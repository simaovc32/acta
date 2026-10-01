"""Things the user tells Acta: mental state, feelings, manual workouts."""

import datetime
import json
import subprocess
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from acta import config
from acta.api.days import device_tz, tz_intervals
from acta.api.deps import INGEST_CMD, INGEST_TIMEOUT_S, last_updated, open_db, open_db_rw
from acta.api.event_log import append_events_atomic, append_workout_event, coffee_dupe
from acta.config import TZ
from acta.insights.ml.mental_predictor import CAFFEINE_WINDOW_MIN
from acta.insights.ml.mental_predictor import run as mental_predict_run

router = APIRouter()


@router.get("/api/mental/history")
def mental_history(days: int = Query(default=30, ge=1, le=90)):
    """Raw mental_state entries for the last N days (word scale stored 1–5).

    Frontend derives today's line and the typical-day-by-hour curves. Each
    entry carries `caffeinated` — true when a coffee was logged within the
    lift window before it — so the frontend can split "typical" into a
    no-coffee and a coffee curve without re-deriving the window itself.
    """
    con = open_db()
    try:
        now = datetime.datetime.now(TZ)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(days=days - 1)
        start_ms = int(start.timestamp() * 1000)
        rows = con.execute(
            "SELECT ts, value, note, night_of FROM user_log "
            "WHERE kind = 'mental_state' AND ts >= ? ORDER BY ts",
            (start_ms,),
        ).fetchall()
        tzi = tz_intervals(con)
    finally:
        con.close()

    try:
        with open(config.EVENTS_PATH) as f:
            events = json.load(f)
    except Exception:
        events = []
    window_ms = CAFFEINE_WINDOW_MIN * 60_000
    coffee_ms = []
    for e in events:
        if e.get("type") != "coffee":
            continue
        try:
            naive = datetime.datetime.strptime(e["datetime"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        # Event times are bare wall clock, so they must be resolved against the
        # zone the device was on that day — the same rule modifiers_active uses —
        # or the caffeine window shifts for coffees logged abroad.
        coffee_ms.append(int(naive.replace(tzinfo=device_tz(
            int(naive.replace(tzinfo=TZ).timestamp() * 1000), tzi)).timestamp() * 1000))

    out = []
    for r in rows:
        d = dict(r)
        d["caffeinated"] = any(0 <= (d["ts"] - c) <= window_ms for c in coffee_ms)
        out.append(d)
    return out


class WorkoutEvent(BaseModel):
    datetime: str        # "YYYY-MM-DD HH:MM"
    duration_min: int
    kind: str = "other"  # cardio | strength | sport | walk | other
    intensity: Optional[str] = None  # leve | moderado | intenso (scales synthetic HR when strap absent)


@router.post("/api/log/workout")
def log_workout(evt: WorkoutEvent):
    # Validate datetime format
    try:
        datetime.datetime.strptime(evt.datetime, "%Y-%m-%d %H:%M")
    except ValueError:
        raise HTTPException(400, "datetime must be YYYY-MM-DD HH:MM") from None
    if evt.duration_min < 1 or evt.duration_min > 480:
        raise HTTPException(400, "duration_min must be 1–480")

    if evt.intensity is not None and evt.intensity not in ("leve", "moderado", "intenso"):
        raise HTTPException(400, "intensity must be leve|moderado|intenso")
    try:
        result = append_workout_event(evt.datetime, evt.duration_min, evt.kind, evt.intensity)
        status = "ok" if result.returncode == 0 else "error"
        detail = result.stdout.strip() or result.stderr.strip() or None
    except subprocess.TimeoutExpired:
        # The event is already durably in events.json — only the recompute timed
        # out, and the scheduled run redoes it. Reporting an error to the assistant here
        # would invite it to log the same workout a second time, which is a worse
        # outcome than a late curve.
        status = "ok"
        detail = (f"logged; recompute exceeded {INGEST_TIMEOUT_S}s "
                  f"and will be redone by the scheduled run")
    con = open_db()
    try:
        lu = last_updated(con)
    finally:
        con.close()
    return {"status": status, "last_updated": lu, "detail": detail}


class FeelingEntry(BaseModel):
    # Only mental_state is served: it is only ever described, never fed into a score.
    mental_state:   Optional[int]  = None   # 1–10, multiple per day
    mental_time:    Optional[str]  = None   # "HH:MM" for mental state timestamp
    note:           Optional[str]  = None
    coffee:         Optional[bool] = None   # True = log a coffee event
    coffee_time:    Optional[str]  = None   # "HH:MM" for coffee timestamp
    alcohol:        Optional[bool] = None   # True = log an alcohol event
    alcohol_time:   Optional[str]  = None   # "HH:MM" for alcohol timestamp
    alcohol_amount: Optional[int]  = None   # number of drinks
    alcohol_type:   Optional[str]  = None   # beer, wine, cider, spirits, cocktail, other
    experiment:     Optional[str]  = None   # A/B tag for the coming night (e.g. "magnesio")


@router.post("/api/log/feeling")
def log_feeling(entry: FeelingEntry):
    if entry.mental_state is not None and not 1 <= entry.mental_state <= 10:
        raise HTTPException(400, "mental_state must be 1–10")
    if (entry.mental_state is None and not entry.note
            and not entry.coffee and not entry.alcohol and not entry.experiment):
        raise HTTPException(400, "at least one field required")

    con = open_db_rw()
    now    = datetime.datetime.now(TZ)
    now_ms = int(now.timestamp() * 1000)
    today  = now.date().isoformat()
    try:
        # mental_state: multiple per day; use supplied time if given
        if entry.mental_state is not None:
            if entry.mental_time:
                try:
                    h, m = map(int, entry.mental_time.split(":"))
                    mental_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    mental_ms = int(mental_dt.timestamp() * 1000)
                except (ValueError, AttributeError):
                    mental_ms = now_ms + 2
            else:
                mental_ms = now_ms + 2
            con.execute(
                "INSERT INTO user_log(ts, kind, value, night_of) VALUES(?,?,?,?)",
                (mental_ms, "mental_state", float(entry.mental_state), today),
            )

        if entry.note and entry.note.strip():
            con.execute(
                "INSERT INTO user_log(ts, kind, value, night_of, note) VALUES(?,?,?,?,?)",
                (now_ms + 3, "note", None, today, entry.note.strip()),
            )

        # experiment tag applies to the COMING night: logged from noon onward →
        # the night ending tomorrow morning; logged in the morning → last night
        # (retroactive, e.g. "ontem tomei magnésio").
        if entry.experiment and entry.experiment.strip():
            exp_night = ((now.date() + datetime.timedelta(days=1)).isoformat()
                         if now.hour >= 12 else today)
            con.execute(
                "INSERT INTO user_log(ts, kind, value, night_of, note) VALUES(?,?,?,?,?)",
                (now_ms + 4, "experiment", None, exp_night,
                 entry.experiment.strip().lower()),
            )
        con.commit()

        new_events = []
        if entry.coffee is True:
            if entry.coffee_time:
                try:
                    h, m = map(int, entry.coffee_time.split(":"))
                    coffee_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                except (ValueError, AttributeError):
                    coffee_dt = now
            else:
                coffee_dt = now
            new_events.append({"datetime": coffee_dt.strftime("%Y-%m-%d %H:%M"), "type": "coffee"})

        if entry.alcohol is True:
            if entry.alcohol_time:
                try:
                    h, m = map(int, entry.alcohol_time.split(":"))
                    alcohol_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
                except (ValueError, AttributeError):
                    alcohol_dt = now
            else:
                alcohol_dt = now
            alcohol_event = {"datetime": alcohol_dt.strftime("%Y-%m-%d %H:%M"), "type": "alcohol"}
            if entry.alcohol_amount is not None:
                alcohol_event["amount"] = entry.alcohol_amount
            if entry.alcohol_type:
                alcohol_event["kind"] = entry.alcohol_type
            new_events.append(alcohol_event)

        # A chat assistant is the live caller of this endpoint, so it is a real
        # concurrent writer and takes the same lock as every other path.
        #
        # Coffee is deduped against what is already logged, because the assistant and a
        # latte logged in Food are two routes to one cup and caffeine_bump() sums
        # every active coffee. Alcohol deliberately is NOT: two drinks twenty
        # minutes apart are a real thing to record, and _alcohol_units() is meant
        # to accumulate them. They are written separately so a deduped coffee can
        # never veto the alcohol event sharing its batch.
        wrote = False
        coffee_evs = [e for e in new_events if e["type"] == "coffee"]
        other_evs  = [e for e in new_events if e["type"] != "coffee"]
        if coffee_evs:
            try:
                c_dt = datetime.datetime.strptime(coffee_evs[0]["datetime"], "%Y-%m-%d %H:%M")
            except ValueError:
                c_dt = None
            wrote = append_events_atomic(
                coffee_evs,
                skip_if=(lambda ev: coffee_dupe(ev, c_dt)) if c_dt else None) or wrote
        if other_evs:
            wrote = append_events_atomic(other_evs) or wrote

        if wrote:
            subprocess.run(
                INGEST_CMD,
                capture_output=True, text=True, timeout=INGEST_TIMEOUT_S,
            )

        return {"status": "ok"}
    finally:
        con.close()


@router.get("/api/mental/predict")
def mental_predict():
    return mental_predict_run()
