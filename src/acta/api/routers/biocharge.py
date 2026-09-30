"""BioCharge: the per-minute energy curve, naps and the day's workouts."""

import datetime
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from acta import config
from acta.api.days import check_date, day_bounds_ms, device_tz, tz_intervals
from acta.api.deps import last_updated, open_db, open_gb
from acta.config import TZ
from acta.engine import sleep_score as SS
from acta.tracking import activities

router = APIRouter()


@router.get("/api/biocharge/latest")
def biocharge_latest():
    con = open_db()
    try:
        now_ms = int(datetime.datetime.now(TZ).timestamp() * 1000)
        row = con.execute(
            "SELECT minute_ts, level, why_label, computed_at FROM biocharge "
            "WHERE minute_ts <= ? ORDER BY minute_ts DESC LIMIT 1",
            (now_ms,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "no biocharge data")
        lu = last_updated(con)
        return {
            "level":       round(row["level"], 1),
            "why_label":   row["why_label"],
            "minute_ts":   row["minute_ts"],
            "last_updated": lu,
        }
    finally:
        con.close()


def hr_covered_minutes(start_ms, end_ms):
    """Set of epoch-minutes in [start,end] that have a real HR sample.

    HUAMI_EXTENDED_ACTIVITY_SAMPLE.TIMESTAMP is in **seconds** (unlike most
    HUAMI_* tables) and uses 255/0 as "no reading" sentinels — both filtered
    here, so an unworn strap reads as absent rather than as a valid number.
    Returns an empty set if Gadgetbridge is unreachable, which degrades to
    "assume covered" (no grey) rather than painting the whole chart grey.
    """
    try:
        gb = open_gb()
    except Exception:
        return set()
    try:
        rows = gb.execute(
            "SELECT TIMESTAMP FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
            "WHERE TIMESTAMP >= ? AND TIMESTAMP <= ? AND HEART_RATE BETWEEN 1 AND 250",
            (start_ms // 1000, end_ms // 1000 + 60),
        ).fetchall()
        return {r[0] // 60 for r in rows}
    except Exception:
        return set()
    finally:
        gb.close()


@router.get("/api/biocharge")
def biocharge_series(hours: int = Query(default=48, ge=1, le=360),
                     date: Optional[str] = None):
    """Biocharge series — either a rolling window (`hours`) or one day (`date`).

    `date=YYYY-MM-DD` returns exactly that device-local day, which is what the
    day view pages through; history depth is then bounded by the data, not by
    how wide a trailing window the client happened to load.
    """
    con = open_db()
    try:
        if date:
            check_date(date)
            start_ms, end_ms = day_bounds_ms(con, date)
            rows = con.execute(
                "SELECT minute_ts, level, why_label FROM biocharge "
                "WHERE minute_ts >= ? AND minute_ts < ? ORDER BY minute_ts",
                (start_ms, end_ms),
            ).fetchall()
        else:
            cutoff_ms = int(
                (datetime.datetime.now(TZ) - datetime.timedelta(hours=hours)).timestamp() * 1000
            )
            rows = con.execute(
                "SELECT minute_ts, level, why_label FROM biocharge WHERE minute_ts >= ? ORDER BY minute_ts",
                (cutoff_ms,),
            ).fetchall()
        # Downsample to keep payload manageable: 1-in-5 for >720 pts, 1-in-15 for >7200.
        # A single day lands on step=5 — the same resolution the live day view uses.
        step = 15 if len(rows) > 7200 else (5 if len(rows) > 720 else 1)

        # `no_hr` marks stretches the strap wasn't recording, so the chart can grey
        # them out instead of drawing a confident line over modelled-only minutes.
        # Biocharge still has a value there (drain falls back to the resting rate),
        # but it is an estimate, not a measurement, and should not look the same.
        covered = hr_covered_minutes(rows[0]["minute_ts"], rows[-1]["minute_ts"]) if rows else set()
        out = []
        for r in rows[::step]:
            m0 = r["minute_ts"] // 60000
            # A downsampled point stands for the `step` minutes it covers, so it
            # only counts as a gap when none of them had a reading.
            has = any((m0 + k) in covered for k in range(step))
            pt = {"minute_ts": r["minute_ts"], "level": round(r["level"], 2),
                  "why_label": r["why_label"]}
            # Sleep is staged from the strap's own hypnogram, so a sleep-labelled
            # minute is by definition worn — never grey those. A nap or a
            # sleep_ext minute is only detected from a worn strap's RAW_KIND
            # stream, so never grey those either.
            if not has and r["why_label"] not in ("sleep", "nap", "sleep_ext"):
                pt["no_hr"] = True
            out.append(pt)
        return out
    finally:
        con.close()


@router.get("/api/activities")
def activities_day(date: Optional[str] = None):
    """Logged workouts overlapping `date` (default: today), for the biocharge chart.

    Read-only view of the workout events that already feed biocharge (events.json): start and end
    (epoch ms and wall clock), minutes, and an icon key + name saying what it was (gym, run, bike,
    football, coaching, ...). Overlapping entries are merged (a guided session and a detection of the
    same effort are one workout). A missing or unreadable events.json is an empty list: this only
    decorates a chart and must never blank it.
    """
    con = open_db()
    try:
        d = date or datetime.datetime.now(TZ).date().isoformat()
        check_date(d)
        start_ms, end_ms = day_bounds_ms(con, d)
        tzi = tz_intervals(con)
    finally:
        con.close()
    try:
        with open(config.EVENTS_PATH) as f:
            events = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(events, list):
        return []

    def resolve(dt_str: str) -> int:
        naive = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        zone = device_tz(int(naive.replace(tzinfo=TZ).timestamp() * 1000), tzi)
        return int(naive.replace(tzinfo=zone).timestamp() * 1000)

    return activities.day_activities(events, start_ms, end_ms, resolve)


@router.get("/api/naps")
def naps(date: Optional[str] = None):
    """Strap-detected daytime naps for `date` (default: today).

    Derived from the biocharge minutes ingest labelled 'nap'. Each nap reports
    whether a real hypnogram overlapped its window: the strap has never yet
    written one for a nap, so stage_source is normally "unknown" and `stages`
    is null — the stages are not shown rather than guessed. `delta` is the
    biocharge the nap put back.
    """
    con = open_db()
    try:
        d = date or datetime.datetime.now(TZ).date().isoformat()
        check_date(d)
        start_ms, end_ms = day_bounds_ms(con, d)
        rows = con.execute(
            "SELECT minute_ts, level, why_label FROM biocharge "
            "WHERE minute_ts >= ? AND minute_ts < ? ORDER BY minute_ts",
            (start_ms, end_ms),
        ).fetchall()
    finally:
        con.close()

    runs, cur, prev_level = [], None, None
    for r in rows:
        if r["why_label"] == "nap":
            if cur is None:
                cur = {"start_ts": r["minute_ts"],
                       "start_level": prev_level if prev_level is not None else r["level"]}
            cur["end_ts"] = r["minute_ts"]
            cur["end_level"] = r["level"]
        elif cur is not None:
            runs.append(cur)
            cur = None
        prev_level = r["level"]
    if cur is not None:
        runs.append(cur)
    if not runs:
        return []

    try:
        gb = open_gb()
        sessions = [(SS.parse_hypnogram(x["DATA"]), x["DATA"]) for x in gb.execute(
            "SELECT DATA FROM HUAMI_SLEEP_SESSION_SAMPLE ORDER BY TIMESTAMP").fetchall()
            if x["DATA"]]
        gb.close()
    except Exception:
        sessions = []
    stage_name = {4: "light", 5: "deep", 7: "awake", 8: "rem"}

    out = []
    for run in runs:
        s_ms, e_ms = run["start_ts"], run["end_ts"] + 60_000
        stages = None
        for segs, blob in sessions:
            if not segs:
                continue
            base = SS.session_base_date(blob)
            b0 = int((base + datetime.timedelta(minutes=segs[0][0])).timestamp() * 1000)
            b1 = int((base + datetime.timedelta(minutes=segs[-1][1])).timestamp() * 1000)
            if b1 <= s_ms or b0 >= e_ms:
                continue
            stages = []
            for sg, eg, code in segs:
                gs = int((base + datetime.timedelta(minutes=sg)).timestamp() * 1000)
                ge = int((base + datetime.timedelta(minutes=eg)).timestamp() * 1000)
                if ge <= s_ms or gs >= e_ms:
                    continue
                stages.append({"stage": stage_name.get(code, "awake"),
                               "start_ts": max(gs, s_ms), "end_ts": min(ge, e_ms)})
            break
        out.append({
            "start_ts": s_ms,
            "end_ts": e_ms,
            "minutes": round((e_ms - s_ms) / 60_000),
            "delta": round(run["end_level"] - run["start_level"], 1),
            "stage_source": "hypnogram" if stages else "unknown",
            "stages": stages,
        })
    return out
