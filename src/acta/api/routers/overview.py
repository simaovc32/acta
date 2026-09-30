"""Top-level status: the home screen summary, system info, manual refresh."""

import datetime
import json
import os
import subprocess
from typing import Optional

from fastapi import APIRouter

from acta import config
from acta.api.days import check_date, day_bounds_ms, device_tz, today_bounds_ms, tz_intervals
from acta.api.deps import INGEST_CMD, INGEST_TIMEOUT_S, last_updated, open_db
from acta.config import TZ

router = APIRouter()


@router.get("/api/health-summary")
def health_summary():
    con = open_db()
    try:
        now_ms = int(datetime.datetime.now(TZ).timestamp() * 1000)
        today_start_ms, _ = today_bounds_ms()
        bc = con.execute(
            "SELECT minute_ts, level, why_label FROM biocharge WHERE minute_ts <= ? ORDER BY minute_ts DESC LIMIT 1",
            (now_ms,),
        ).fetchone()
        sl = con.execute(
            "SELECT night_of, score, waketime_ts FROM sleep_score ORDER BY night_of DESC LIMIT 1"
        ).fetchone()
        lu = last_updated(con)
        # Earliest day with any data — the floor for day-by-day navigation, so the
        # back arrow stops where history actually ends rather than where the
        # client's loaded window happens to end.
        ds = con.execute("SELECT MIN(night_of) d FROM sleep_score").fetchone()
        # "Fresh" = there is real data covering the gap between the strap/phone
        # sync and now, rather than the pipeline's last stored row from an
        # earlier day silently being shown as if it were current (2026-09-22:
        # a morning with no overnight export showed yesterday 23:57's biocharge
        # level and yesterday's sleep night as if they were today's).
        biocharge_fresh = bool(bc and bc["minute_ts"] >= today_start_ms)
        # Sleep is a once-nightly event, not a per-minute series, so "today's
        # calendar date" doesn't apply the same way — instead, is the latest
        # scored night's wake-up recent enough to still be "last night" rather
        # than a night from a day or more ago that hasn't been superseded yet.
        sleep_fresh = bool(
            sl and sl["waketime_ts"] is not None
            and now_ms - sl["waketime_ts"] <= 24 * 3600 * 1000
        )
        return {
            "biocharge_level": round(bc["level"], 1) if bc else None,
            "biocharge_why":   bc["why_label"]        if bc else None,
            "biocharge_fresh": biocharge_fresh,
            "sleep_score":     round(sl["score"], 1)  if sl else None,
            "sleep_night_of":  sl["night_of"]          if sl else None,
            "sleep_fresh":     sleep_fresh,
            "last_updated":    lu,
            "data_start":      ds["d"] if ds else None,
        }
    finally:
        con.close()


@router.get("/api/system/info")
def system_info():
    con = open_db()
    try:
        nights = con.execute(
            "SELECT COUNT(*) c, MIN(night_of) mn, MAX(night_of) mx FROM sleep_score"
        ).fetchone()
        run = con.execute(
            "SELECT started_at, status FROM ingest_run ORDER BY id DESC LIMIT 1"
        ).fetchone()
        # Distinct from last_ingest_at: a run's started_at ticks on every
        # noop check, so it says nothing about whether data actually moved.
        bc_ts = con.execute("SELECT MAX(computed_at) FROM biocharge").fetchone()[0]
        sl_ts = con.execute("SELECT MAX(computed_at) FROM sleep_score").fetchone()[0]
        last_change = max(t for t in (bc_ts, sl_ts) if t) if (bc_ts or sl_ts) else None
        db_size_mb = round(os.path.getsize(config.ACTA_DB) / (1024 * 1024), 1)
        return {
            "nights_recorded":    nights["c"],
            "data_start":         nights["mn"],
            "data_end":           nights["mx"],
            "db_size_mb":         db_size_mb,
            "last_ingest_at":     run["started_at"] if run else None,
            "last_ingest_status": run["status"] if run else None,
            "last_data_change":   last_change,
        }
    finally:
        con.close()


@router.get("/api/modifiers/active")
def modifiers_active():
    """Coffee / alcohol events whose drain-effect window is active right now.

    Windows match biocharge.py: coffee 150 min, alcohol 240 min. Used by the
    snapshot 'ACTIVE' cell to explain the current drain slope.
    """
    DURATIONS = {"coffee": 150, "alcohol": 240}
    now = datetime.datetime.now(TZ)
    try:
        with open(config.EVENTS_PATH) as f:
            events = json.load(f)
    except Exception:
        return []

    # Event times are bare wall clock; resolve each against the zone the device
    # was on that day rather than assuming home.
    acon = open_db()
    try:
        tzi = tz_intervals(acon)
    finally:
        acon.close()

    active = []
    for e in events:
        kind = e.get("type")
        dur = DURATIONS.get(kind)
        if not dur:
            continue
        try:
            naive = datetime.datetime.strptime(e["datetime"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        start = naive.replace(tzinfo=device_tz(
            int(naive.replace(tzinfo=TZ).timestamp() * 1000), tzi))
        end = start + datetime.timedelta(minutes=dur)
        if start <= now < end:
            active.append({
                "kind": kind,
                "until": end.strftime("%H:%M"),
                "mins_left": int((end - now).total_seconds() // 60),
            })
    active.sort(key=lambda m: m["mins_left"])
    return active


@router.get("/api/log/today")
def log_today(date: Optional[str] = None):
    # Returns a day's user_log events — no sleep score, no score hints (anti-bias rule).
    con = open_db()
    try:
        if date:
            check_date(date)
            start_ms, end_ms = day_bounds_ms(con, date)
        else:
            start_ms, end_ms = today_bounds_ms()
        rows = con.execute(
            "SELECT ts, kind, value, note FROM user_log "
            "WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start_ms, end_ms),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@router.post("/api/refresh")
def trigger_refresh():
    """The dashboard's refresh button.

    Had a 60s timeout — the shortest of any ingest call site — and did not catch
    TimeoutExpired, so a real (non-noop) run that took longer raised out of the
    handler and the button reported a 500 for work that had actually succeeded.
    Now it shares INGEST_TIMEOUT_S and reports a slow run as a slow run.
    """
    try:
        result = subprocess.run(
            INGEST_CMD,
            capture_output=True, text=True, timeout=INGEST_TIMEOUT_S
        )
        status = "ok" if result.returncode == 0 else "error"
        detail = result.stdout.strip() or result.stderr.strip() or None
    except subprocess.TimeoutExpired:
        # subprocess.run kills the child on timeout, so this run really is over.
        # The 5-minute timer picks it up again; nothing is lost.
        status = "timeout"
        detail = (f"ingest exceeded {INGEST_TIMEOUT_S}s and was stopped — "
                  f"the scheduled run will retry shortly")
    con = open_db()
    try:
        lu = last_updated(con)
    finally:
        con.close()
    return {"status": status, "last_updated": lu, "detail": detail}
