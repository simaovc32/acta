"""Vitals: resting HR, HRV, SpO2, stress, temperature and respiratory rate."""

import bisect
import datetime
import statistics
from typing import Optional

from fastapi import APIRouter, Query

from acta.api.days import check_date, day_bounds_ms, device_date, device_tz, today_bounds_ms, tz_intervals
from acta.api.deps import open_db, open_gb
from acta.config import TZ

router = APIRouter()


@router.get("/api/vitals/snapshot")
def vitals_snapshot():
    try:
        con = open_gb()
    except Exception:
        return {"hr": None, "hr_time": None, "hr_ts": None, "rhr": None, "hrv": None,
                "spo2": None, "battery": None, "battery_ts": None}
    try:
        # Latest continuous HR — TIMESTAMP in seconds in this table
        hr_row = con.execute(
            "SELECT TIMESTAMP, HEART_RATE FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
            "WHERE HEART_RATE > 0 ORDER BY TIMESTAMP DESC LIMIT 1"
        ).fetchone()
        hr_val  = int(hr_row["HEART_RATE"]) if hr_row else None
        hr_time = None
        hr_ts   = None
        if hr_row:
            # TIMESTAMP here is in seconds (activity table quirk)
            hr_ts = int(hr_row["TIMESTAMP"] * 1000)
            dt = datetime.datetime.fromtimestamp(hr_row["TIMESTAMP"], TZ)
            hr_time = dt.strftime("%H:%M")

        # Resting HR — TIMESTAMP in ms
        rhr_row = con.execute(
            "SELECT HEART_RATE FROM HUAMI_HEART_RATE_RESTING_SAMPLE "
            "ORDER BY TIMESTAMP DESC LIMIT 1"
        ).fetchone()

        # SpO2 — TIMESTAMP in ms
        spo2_row = con.execute(
            "SELECT SPO2 FROM HUAMI_SPO2_SAMPLE ORDER BY TIMESTAMP DESC LIMIT 1"
        ).fetchone()

        # HRV — mean of last 20 values (measured during sleep)
        hrv_rows = con.execute(
            "SELECT VALUE FROM GENERIC_HRV_VALUE_SAMPLE ORDER BY TIMESTAMP DESC LIMIT 20"
        ).fetchall()
        hrv = round(sum(r["VALUE"] for r in hrv_rows) / len(hrv_rows)) if hrv_rows else None

        # Strap battery — TIMESTAMP in seconds, level in LEVEL
        batt_row = con.execute(
            "SELECT TIMESTAMP, LEVEL FROM BATTERY_LEVEL ORDER BY TIMESTAMP DESC LIMIT 1"
        ).fetchone()

        today_start_ms, _ = today_bounds_ms()
        return {
            "hr":         hr_val,
            "hr_time":    hr_time,
            "hr_ts":      hr_ts,
            "rhr":        int(rhr_row["HEART_RATE"]) if rhr_row else None,
            "hrv":        hrv,
            "spo2":       int(spo2_row["SPO2"])      if spo2_row else None,
            "battery":    int(batt_row["LEVEL"])         if batt_row else None,
            "battery_ts": int(batt_row["TIMESTAMP"] * 1000) if batt_row else None,
            # HR samples every ~1 min whenever the strap is worn and synced, so
            # "no HR sample yet today" is a reliable signal nothing has synced
            # today at all — same staleness concept as health-summary's
            # biocharge_fresh, kept independent since vitals reads Gadgetbridge
            # directly rather than through ingest's replayed biocharge table.
            "fresh":      bool(hr_ts is not None and hr_ts >= today_start_ms),
        }
    finally:
        con.close()


@router.get("/api/vitals/history")
def vitals_history(days: int = Query(default=14, ge=1, le=60),
                   end: Optional[str] = None):
    """Per-day HRV / RHR / SpO2 for the last N days, each with a rolling baseline.

    HRV and SpO2 have many readings per day (mostly overnight) so we take the
    daily mean; RHR is already one value/day. Baseline = median of the window,
    matching the 21-day-median convention used by the biocharge drain model.

    `end=YYYY-MM-DD` ends the window on that day instead of today, so a past day
    is compared against the baseline *as it stood then* rather than against a
    baseline that includes days the user had not lived yet.
    """
    empty = {"series": [], "baseline": None, "current": None}
    try:
        con = open_gb()
    except Exception:
        return {"hrv":   {**empty, "unit": "ms"},
                "rhr":   {**empty, "unit": "bpm"},
                "spo2":  {**empty, "unit": "%"},
                "resp":  {**empty, "unit": "br/min"},
                "steps": {**empty, "unit": "steps"},
                "temp":  {**empty, "unit": "°C"}}

    # Day buckets follow the device's zone, so readings taken abroad don't land
    # in the wrong day once you're home.
    acon = open_db()
    try:
        tzi = tz_intervals(acon)
        # Sleep-window boundaries (bedtime_ts -> waketime_ts -> night_of), sorted
        # by bedtime, for bucketing nocturnal signals by the night they belong to
        # rather than by calendar date (see night_windows() below).
        night_rows = acon.execute(
            "SELECT night_of, bedtime_ts, waketime_ts FROM sleep_score "
            "WHERE bedtime_ts IS NOT NULL AND waketime_ts IS NOT NULL "
            "ORDER BY bedtime_ts"
        ).fetchall()
        night_windows = [(r["bedtime_ts"], r["waketime_ts"], r["night_of"]) for r in night_rows]
        night_starts  = [w[0] for w in night_windows]
        if end:
            check_date(end)
            end_ms = day_bounds_ms(acon, end)[1]
        else:
            end_ms = None
    finally:
        acon.close()

    def night_of_for(ts_ms: int) -> Optional[str]:
        """Which night's bedtime->waketime window a ms timestamp falls in, if any."""
        i = bisect.bisect_right(night_starts, ts_ms) - 1
        if i >= 0:
            bed, wake, night_of = night_windows[i]
            if bed <= ts_ms <= wake:
                return night_of
        return None

    now = datetime.datetime.now(TZ)
    if end_ms is None:
        cutoff_ms = int((now - datetime.timedelta(days=days)).timestamp() * 1000)
    else:
        cutoff_ms = end_ms - days * 86400 * 1000

    def per_day(table, col, where="", ndigits=0, night_bucketed=False):
        """Bucket ms-timestamped readings into a mean per day.

        `night_bucketed=True` assigns each reading to the night (bedtime->
        waketime, keyed by night_of) it falls in, rather than the calendar day
        it happened to land on. This matters for signals only ever measured
        overnight (HRV, respiratory rate; SpO2 mostly) — a sleep session
        crossing midnight otherwise gets split in two by calendar-day
        bucketing, mixing the tail of one night with the start of the next
        and producing a figure that matches neither night. A reading with no
        matching sleep window (stray daytime spot-check, or a night missing
        sleep data) still falls back to its calendar day so it isn't dropped.
        """
        rnd = (lambda x: round(x, ndigits)) if ndigits else (lambda x: round(x))
        rows = con.execute(
            f"SELECT TIMESTAMP, {col} AS v FROM {table} "
            f"WHERE TIMESTAMP >= ? AND TIMESTAMP < ? AND {col} > 0 {where} ORDER BY TIMESTAMP",
            (cutoff_ms, end_ms if end_ms is not None else 2 ** 62),
        ).fetchall()
        buckets: dict[str, list[float]] = {}
        for r in rows:
            ts = r["TIMESTAMP"]
            d = (night_bucketed and night_of_for(ts)) or device_date(ts, tzi)
            buckets.setdefault(d, []).append(r["v"])
        series = [{"date": d, "value": rnd(statistics.mean(vs))}
                  for d, vs in sorted(buckets.items())]
        vals = [p["value"] for p in series]
        baseline = rnd(statistics.median(vals)) if vals else None
        current  = series[-1]["value"] if series else None
        return {"series": series, "baseline": baseline, "current": current}

    def steps_per_day():
        # STEPS live in the activity table (TIMESTAMP in SECONDS) → daily total.
        # Baseline is the median of COMPLETE days only; today's total is partial.
        cutoff_s = cutoff_ms // 1000
        end_s    = (end_ms // 1000) if end_ms is not None else 2 ** 40
        rows = con.execute(
            "SELECT TIMESTAMP, STEPS FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
            "WHERE TIMESTAMP >= ? AND TIMESTAMP < ? AND STEPS > 0 ORDER BY TIMESTAMP",
            (cutoff_s, end_s),
        ).fetchall()
        buckets: dict[str, int] = {}
        for r in rows:
            d = device_date(r["TIMESTAMP"] * 1000, tzi)   # activity table is SECONDS
            buckets[d] = buckets.get(d, 0) + int(r["STEPS"])
        series = [{"date": d, "value": v} for d, v in sorted(buckets.items())]
        today = device_date(int(now.timestamp() * 1000), tzi)
        past = [v for d, v in buckets.items() if d != today]
        baseline = round(statistics.median(past)) if past else None
        current  = buckets.get(today)
        return {"series": series, "baseline": baseline, "current": current}

    def overnight_temp():
        # Skin temp swings ~8 °C across the day, so a daytime mean is noise. The
        # 01:00–06:00 core-sleep window is stable; a rising trend there flags
        # fever / illness / overtraining (and feeds the physio 'temp drop' idea).
        rows = con.execute(
            "SELECT TIMESTAMP, TEMPERATURE AS v FROM GENERIC_TEMPERATURE_SAMPLE "
            "WHERE TIMESTAMP >= ? AND TEMPERATURE > 20 ORDER BY TIMESTAMP",
            (cutoff_ms,),
        ).fetchall()
        buckets: dict[str, list[float]] = {}
        for r in rows:
            dt = datetime.datetime.fromtimestamp(
                r["TIMESTAMP"] / 1000, device_tz(r["TIMESTAMP"], tzi))
            if 1 <= dt.hour < 6:
                buckets.setdefault(dt.date().isoformat(), []).append(r["v"])
        series = [{"date": d, "value": round(statistics.mean(vs), 1)}
                  for d, vs in sorted(buckets.items())]
        vals = [p["value"] for p in series]
        baseline = round(statistics.median(vals), 1) if vals else None
        current  = series[-1]["value"] if series else None
        return {"series": series, "baseline": baseline, "current": current}

    try:
        # HRV and respiratory rate are measured only overnight; SpO2 mostly so.
        # RHR is a single instantaneous sample at wake (calendar day is already
        # correct) and steps is an inherently whole-day total — neither is
        # night-bucketed.
        hrv   = per_day("GENERIC_HRV_VALUE_SAMPLE", "VALUE", night_bucketed=True)
        rhr   = per_day("HUAMI_HEART_RATE_RESTING_SAMPLE", "HEART_RATE")
        spo2  = per_day("HUAMI_SPO2_SAMPLE", "SPO2", night_bucketed=True)
        resp  = per_day("HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE", "RATE", ndigits=1, night_bucketed=True)
        temp  = overnight_temp()
        steps = steps_per_day()
    finally:
        con.close()

    return {"hrv":   {**hrv,   "unit": "ms"},
            "rhr":   {**rhr,   "unit": "bpm"},
            "spo2":  {**spo2,  "unit": "%"},
            "resp":  {**resp,  "unit": "br/min"},
            "steps": {**steps, "unit": "steps"},
            "temp":  {**temp,  "unit": "°C"}}


@router.get("/api/vitals/intraday")
def vitals_intraday(date: Optional[str] = None):
    """Per-minute HR and ~5-min stress for today, or for `date`.

    HR lives in HUAMI_EXTENDED_ACTIVITY_SAMPLE (TIMESTAMP in **seconds**); stress
    in HUAMI_STRESS_SAMPLE (**ms**). Both timestamps are returned in ms for the JS.
    Gadgetbridge keeps the raw samples for the whole history, so a past day is
    served at the same per-minute resolution as today.
    """
    out = {"hr": [], "stress": [], "rhr": None}
    if date:
        check_date(date)
    try:
        con = open_gb()
    except Exception:
        return out

    if date:
        acon = open_db()
        try:
            start_ms, end_ms = day_bounds_ms(acon, date)
        finally:
            acon.close()
    else:
        now = datetime.datetime.now(TZ)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ms = int(day_start.timestamp() * 1000)
        end_ms   = start_ms + 86400 * 1000
    ds_s,  de_s  = start_ms // 1000, end_ms // 1000   # activity table = seconds
    ds_ms, de_ms = start_ms, end_ms                    # stress table  = ms
    try:
        hr = con.execute(
            "SELECT TIMESTAMP, HEART_RATE FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
            "WHERE TIMESTAMP >= ? AND TIMESTAMP < ? "
            "AND HEART_RATE BETWEEN 1 AND 250 ORDER BY TIMESTAMP",
            (ds_s, de_s),
        ).fetchall()
        out["hr"] = [{"ts": r["TIMESTAMP"] * 1000, "v": int(r["HEART_RATE"])} for r in hr]

        st = con.execute(
            "SELECT TIMESTAMP, STRESS FROM HUAMI_STRESS_SAMPLE "
            "WHERE TIMESTAMP >= ? AND TIMESTAMP < ? AND STRESS > 0 ORDER BY TIMESTAMP",
            (ds_ms, de_ms),
        ).fetchall()
        out["stress"] = [{"ts": r["TIMESTAMP"], "v": int(r["STRESS"])} for r in st]

        # For a past day take that day's own resting HR, not the newest reading.
        if date:
            rhr = con.execute(
                "SELECT HEART_RATE FROM HUAMI_HEART_RATE_RESTING_SAMPLE "
                "WHERE TIMESTAMP >= ? AND TIMESTAMP < ? ORDER BY TIMESTAMP DESC LIMIT 1",
                (ds_ms, de_ms),
            ).fetchone()
        else:
            rhr = con.execute(
                "SELECT HEART_RATE FROM HUAMI_HEART_RATE_RESTING_SAMPLE "
                "ORDER BY TIMESTAMP DESC LIMIT 1"
            ).fetchone()
        out["rhr"] = int(rhr["HEART_RATE"]) if rhr else None
    finally:
        con.close()
    return out
