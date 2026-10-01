"""Sleep: nightly scores, hypnograms, overnight heart rate, bedtime advice."""

import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from acta.api.days import check_date
from acta.api.deps import last_updated, open_db, open_gb
from acta.engine import sleep_score as SS
from acta.insights.ml.bedtime_recommender import run as bedtime_run

router = APIRouter()


@router.get("/api/sleep/latest")
def sleep_latest(date: Optional[str] = None):
    """Latest scored night, or the night of `date` when given.

    Returns 404 for a date with no scored night — the caller renders an empty
    state rather than silently showing a different night's numbers.
    """
    con = open_db()
    try:
        if date:
            check_date(date)
            row = con.execute(
                "SELECT * FROM sleep_score WHERE night_of = ?", (date,)
            ).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM sleep_score ORDER BY night_of DESC LIMIT 1"
            ).fetchone()
        if not row:
            raise HTTPException(404, "no sleep data")
        lu = last_updated(con)
        d  = dict(row)
        d["last_updated"] = lu
        # Round floats for cleanliness
        for k in ("score", "c_efficiency", "c_regularity", "c_duration", "c_stage_balance", "c_physio"):
            if d.get(k) is not None:
                d[k] = round(d[k], 2)
        return d
    finally:
        con.close()


@router.get("/api/sleep/recent")
def sleep_recent(n: int = Query(default=14, ge=1, le=90)):
    con = open_db()
    try:
        rows = con.execute(
            "SELECT night_of, score, c_efficiency, c_regularity, c_duration, "
            "c_stage_balance, c_physio, asleep_min, deep_min, light_min, rem_min, "
            "awake_min, bedtime_ts, waketime_ts, tz_offset_min "
            "FROM sleep_score ORDER BY night_of DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [
            {**dict(r), "score": round(r["score"], 1)}
            for r in rows
        ]
    finally:
        con.close()


@router.get("/api/sleep/stages")
def sleep_stages(date: Optional[str] = None):
    """Hypnogram segments for the latest sleep session, or for `date`'s night.

    Gadgetbridge keeps the full raw session history, so any past night can be
    rendered at real resolution rather than from a stored summary.
    """
    if date:
        check_date(date)
    con = open_db()
    try:
        row = con.execute(
            "SELECT night_of FROM sleep_score WHERE night_of = ?", (date,)
        ).fetchone() if date else con.execute(
            "SELECT night_of FROM sleep_score ORDER BY night_of DESC LIMIT 1"
        ).fetchone()
        if not row:
            return []
        target_night = row["night_of"]
    finally:
        con.close()

    try:
        gb = open_gb()
    except Exception:
        return []

    try:
        rows = gb.execute(
            "SELECT DATA FROM HUAMI_SLEEP_SESSION_SAMPLE ORDER BY TIMESTAMP"
        ).fetchall()
        stage_map = {4: "light", 5: "deep", 7: "awake", 8: "rem"}
        for r in rows:
            blob = r["DATA"]
            if not blob:
                continue
            segs = SS.parse_hypnogram(blob)
            if not segs:
                continue
            base = SS.session_base_date(blob)
            waketime = base + datetime.timedelta(minutes=segs[-1][1])
            if str(waketime.date()) != target_night:
                continue
            t0 = segs[0][0]
            result = []
            for s, e, code in segs:
                s_ms = int((base + datetime.timedelta(minutes=s)).timestamp() * 1000)
                e_ms = int((base + datetime.timedelta(minutes=e)).timestamp() * 1000)
                result.append({
                    "stage":     stage_map.get(code, "awake"),
                    "start_min": s - t0,
                    "end_min":   e - t0,
                    "start_ts":  s_ms,
                    "end_ts":    e_ms,
                })
            # Sleep the hypnogram never recorded (see ingest.extend_sleep_from_activity),
            # so the chart ends at the same wake time the stats report. Drawn as light
            # sleep (Gadgetbridge's convention) as a DISPLAY choice only: the activity
            # stream has no stage information, light_min is untouched, and stage
            # percentages come from the hypnogram alone.
            con2 = open_db()
            try:
                ext = con2.execute(
                    "SELECT ext_sleep_min, ext_gap_min, waketime_ts FROM sleep_score "
                    "WHERE night_of = ?", (target_night,)).fetchone()
            finally:
                con2.close()
            if ext and (ext["ext_sleep_min"] or 0) > 0:
                hyp_end_ms = result[-1]["end_ts"]
                gap_ms = (ext["ext_gap_min"] or 0) * 60_000
                base_min = result[-1]["end_min"]
                if gap_ms:
                    result.append({
                        "stage": "awake",
                        "start_min": base_min,
                        "end_min": base_min + (ext["ext_gap_min"] or 0),
                        "start_ts": hyp_end_ms,
                        "end_ts": hyp_end_ms + gap_ms,
                    })
                result.append({
                    "stage": "light",
                    "recovered": True,
                    "start_min": base_min + (ext["ext_gap_min"] or 0),
                    "end_min": base_min + (ext["ext_gap_min"] or 0) + ext["ext_sleep_min"],
                    "start_ts": hyp_end_ms + gap_ms,
                    "end_ts": ext["waketime_ts"],
                })
            return result
        return []
    finally:
        gb.close()


@router.get("/api/sleep/hr-history")
def sleep_hr_history(n: int = Query(default=7, ge=1, le=14)):
    """Return min/max sleep HR per night for the last N nights."""
    con = open_db()
    try:
        rows = con.execute(
            "SELECT night_of, bedtime_ts, waketime_ts FROM sleep_score ORDER BY night_of DESC LIMIT ?",
            (n,),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return []

    try:
        gb = open_gb()
    except Exception:
        return [{"night_of": r["night_of"], "hr_min": None, "hr_max": None} for r in rows]

    try:
        result = []
        for r in rows:
            # HUAMI_EXTENDED_ACTIVITY_SAMPLE uses seconds, not ms
            bed_s  = r["bedtime_ts"]  // 1000
            wake_s = r["waketime_ts"] // 1000
            hr_rows = gb.execute(
                "SELECT HEART_RATE FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
                "WHERE TIMESTAMP BETWEEN ? AND ? AND HEART_RATE > 0 AND HEART_RATE < 200",
                (bed_s, wake_s),
            ).fetchall()
            if hr_rows:
                hrs = [hr["HEART_RATE"] for hr in hr_rows]
                result.append({"night_of": r["night_of"], "hr_min": int(min(hrs)), "hr_max": int(max(hrs))})
            else:
                result.append({"night_of": r["night_of"], "hr_min": None, "hr_max": None})
        return result
    finally:
        gb.close()


@router.get("/api/recommend/bedtime")
def recommend_bedtime():
    return bedtime_run()
