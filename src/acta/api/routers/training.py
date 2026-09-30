"""Training load: detected workouts (PAI), readiness, VO2max, activity energy."""

import datetime
import re
import sqlite3
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from acta.api.days import check_date
from acta.api.deps import open_db, trigger_ingest
from acta.api.event_log import write_event_only
from acta.config import TZ
from acta.engine import calories, fitness, night_physio, pai
from acta.tracking import body

router = APIRouter()


class PaiConfirm(BaseModel):
    activity: str                        # picker slug -> pai.canonical_kind()
    intensity: Optional[str] = None      # leve | moderado | intenso
    duration_min: Optional[int] = None   # override the detected duration


def with_energy(rows: list) -> list:
    """Attach the marginal kcal cost to each detected bout.

    Priced by slicing the day's own minute-by-minute energy curve, so a bout's
    number can never disagree with the day total it sits inside. Days are
    computed once and reused -- several bouts often share one. Never fatal: a
    day with no strap coverage simply gets no figure rather than a 500.
    """
    seen = {}
    for r in rows:
        try:
            key = (r["date"], r["start_min"], r["duration_min"], r.get("work_min"))
            if key not in seen:
                seen[key] = calories.activity_cost(
                    r["date"], r["start_min"], r["duration_min"],
                    work_min=r.get("work_min"))
            c = seen[key]
            r["kcal"] = c["net_kcal"]                 # effort only
            r["cooldown_kcal"] = c.get("cooldown_kcal") or 0
            r["kcal_per_min"] = c["kcal_per_min"]
        except Exception:
            r["kcal"] = None
    return rows


@router.get("/api/pai/detections")
def pai_detections():
    """Unconfirmed sessions the strap saw but that were never logged.
    Drives the topbar warning badge; an empty list hides it entirely."""
    rows = pai.pending()
    for r in rows:
        r["start_clock"] = pai.fmt_clock(r["start_min"])
    with_energy(rows)
    return {"detections": rows,
            "count": len(rows),
            "activities": [{"slug": s, "label": label} for s, label, _ in pai.ACTIVITY_LABELS]}


@router.post("/api/pai/detections/{det_id}/confirm")
def pai_confirm(det_id: int, body: PaiConfirm, bg: BackgroundTasks):
    """Turn a detection into a real workout event.

    The ingest recompute append_workout_event triggers is slow, so it runs as a
    BackgroundTask -- same reasoning as POST /api/workout/session, which keeps
    the subprocess off the request path.
    """
    if body.intensity is not None and body.intensity not in ("leve", "moderado", "intenso"):
        raise HTTPException(400, "intensity must be leve|moderado|intenso")
    # Validate the slug rather than letting canonical_kind() fall back to
    # "other" -- that fallback silently prices a session at 25 bpm instead of
    # sport's 45, and would write the unrecognised string into events.json.
    if body.activity not in {s for s, _, _ in pai.ACTIVITY_LABELS}:
        raise HTTPException(400, f"unknown activity '{body.activity}'")
    con = pai.open_acta(write=True)
    con.execute(pai.SCHEMA)
    try:
        row = con.execute(
            "SELECT * FROM pai_detection WHERE id=? AND status='pending'", (det_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "no pending detection with that id")
        dur = body.duration_min or row["duration_min"]
        if dur < 1 or dur > 480:
            raise HTTPException(400, "duration_min must be 1-480")
        kind = pai.canonical_kind(body.activity)
        intensity = body.intensity or row["intensity"]
        dt_str = f"{row['date']} {pai.fmt_clock(row['start_min'])}"

        # Write the event FIRST, synchronously. It is a fast locked append; the
        # slow part is the recompute, which is what belongs in the background.
        # Marking the row confirmed before the event existed meant a failure in
        # the background task (torn read, subprocess timeout) left the detection
        # resolved with nothing written -- silently lost, and scan() would never
        # re-propose it because (date, start_min) was already known.
        write_event_only(dt_str, dur, kind, intensity, "pai", body.activity)

        con.execute(
            "UPDATE pai_detection SET status='confirmed', kind=?, label=?, "
            "intensity=?, duration_min=?, resolved_at=? WHERE id=?",
            (kind, body.activity, intensity, dur,
             datetime.datetime.now(TZ).isoformat(), det_id),
        )
        con.commit()
    finally:
        con.close()
    bg.add_task(trigger_ingest)
    return {"ok": True, "datetime": dt_str, "kind": kind,
            "label": body.activity, "duration_min": dur, "intensity": intensity}


@router.get("/api/pai/history")
def pai_history(limit: int = Query(default=100, ge=1, le=500)):
    """Resolved detections, split by outcome.

    Confirmed sessions otherwise have no surface anywhere: they leave the badge
    and live only in events.json, which nothing renders. Dismissed ones are
    listed separately so a mistaken dismiss can be undone -- before this the
    only way back was SQL.
    """
    con = pai.open_acta()
    try:
        rows = con.execute(
            "SELECT * FROM pai_detection WHERE status IN ('confirmed','dismissed') "
            "ORDER BY date DESC, start_min DESC LIMIT ?", (limit,)
        ).fetchall()
    except sqlite3.OperationalError:
        return {"confirmed": [], "dismissed": []}
    finally:
        con.close()
    out = {"confirmed": [], "dismissed": []}
    labels = {s: label for s, label, _ in pai.ACTIVITY_LABELS}
    for r in rows:
        d = dict(r)
        d["start_clock"] = pai.fmt_clock(d["start_min"])
        d["label_display"] = labels.get(d.get("label") or "", d.get("label") or "—")
        out[d["status"]].append(d)
    with_energy(out["confirmed"])
    with_energy(out["dismissed"])
    return out


@router.post("/api/pai/detections/{det_id}/undismiss")
def pai_undismiss(det_id: int):
    """Put a dismissed detection back in the pending queue.

    Only 'dismissed' is reversible here. Un-confirming would have to retract the
    events.json entry it already wrote and re-run the recompute, which is a
    different and riskier operation -- deliberately not offered.
    """
    con = pai.open_acta(write=True)
    try:
        cur = con.execute(
            "UPDATE pai_detection SET status='pending', resolved_at=NULL "
            "WHERE id=? AND status='dismissed'", (det_id,))
        con.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "no dismissed detection with that id")
    finally:
        con.close()
    return {"ok": True}


@router.get("/api/pai/load")
def pai_load(days: int = Query(default=14, ge=7, le=90)):
    """Rolling cardio load for the context tile.

    PAI_TOTAL is a trailing 7-day sum against Huami's target of 100. It is
    reported here for display only and deliberately feeds no score: it drops
    off a cliff when a big day ages out of the window (2026-08-14: 81.2 ->
    08-15: 10.4) with nothing physiological behind the fall, so a score reading
    it would crater for a purely calendar reason.
    """
    gb = pai.open_gb()
    try:
        days_map = pai.daily(gb)
    finally:
        gb.close()
    if not days_map:
        return {"current": None, "target": 100, "earned_today": None, "series": []}
    ordered = sorted(days_map)
    today = datetime.datetime.now(TZ).date().isoformat()
    series = [{"date": d, "value": round(days_map[d]["PAI_TOTAL"] or 0, 1)}
              for d in ordered[-days:]]
    latest = days_map[ordered[-1]]
    return {
        "current": round(latest["PAI_TOTAL"] or 0, 1),
        "target": 100,
        "earned_today": round(latest["PAI_TODAY"] or 0, 1) if ordered[-1] == today else None,
        "series": series,
    }


@router.post("/api/pai/detections/{det_id}/dismiss")
def pai_dismiss(det_id: int):
    """Not a workout. Stays dismissed -- scan() never re-proposes a resolved row."""
    con = pai.open_acta(write=True)
    con.execute(pai.SCHEMA)
    try:
        cur = con.execute(
            "UPDATE pai_detection SET status='dismissed', resolved_at=? "
            "WHERE id=? AND status='pending'",
            (datetime.datetime.now(TZ).isoformat(), det_id),
        )
        con.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "no pending detection with that id")
    finally:
        con.close()
    return {"ok": True}


@router.get("/api/readiness")
def api_readiness(date: Optional[str] = None):
    """Today's readiness (0-100, 4 weighted components) + RHR trend state.
    score is null until the night has synced (sleep + RHR present).

    With `date`, the score is computed for that day. The RHR-trend and recovery
    blocks are **not** back-dated — both are rolling detectors of *current*
    state, so they are returned empty rather than showing today's alarm next to
    a past day's score.
    """
    if date:
        check_date(date)
        d = datetime.date.fromisoformat(date)
        at = datetime.datetime(d.year, d.month, d.day, 12, 0, tzinfo=TZ)
        score, parts = night_physio.readiness(now=at)
        return {
            "score": score,
            "parts": parts,
            "rhr": {"last": None, "baseline": None, "warning": False},
            "recovery": {"level": None, "message": None},
            "historical": True,
        }
    score, parts = night_physio.readiness()
    last, baseline, warning = night_physio.rhr_trend()
    rec_level, rec_msg, _rec_signals = night_physio.recovery_alarm()
    trim = night_physio.training_trim()
    return {
        "score": score,
        "parts": parts,          # {sleep, rhr, hrv, load} each 0-100
        "rhr": {"last": last, "baseline": baseline, "warning": warning},
        # Full recovery detector (sustained 3-night + acute 2-night nosedive).
        # level: null | "soft" | "hard"; message is the human-readable line.
        "recovery": {"level": rec_level, "message": rec_msg},
        # Readiness-driven training-load cut for today's planned session, or
        # null on a green/amber day. Lightweight text only (pct/summary) -
        # the trimmed plan itself is served by GET /api/workout/plan.
        "trim": ({"pct": trim["pct"], "score": trim["score"],
                   "type": trim["type"], "summary": trim["summary"]}
                  if trim else None),
        "historical": False,
    }


@router.get("/api/fitness")
def api_fitness(date: Optional[str] = None):
    """Estimated VO2max + percentile + 90-day trend, for the biocharge-tab card.

    Two independent estimators (Uth HR-ratio, Nes/HUNT non-exercise) reconciled
    into a range + central point. Leaf metric — feeds nothing. With `date`, the
    row for that day (or the most recent earlier one) is returned; the trend
    window ends there.
    """
    today = datetime.datetime.now(TZ).date().isoformat()
    target = today
    if date:
        check_date(date)
        target = date
    con = open_db()
    try:
        fitness.ensure(con)
        row = con.execute(
            "SELECT * FROM fitness_estimate WHERE date <= ? ORDER BY date DESC LIMIT 1",
            (target,)).fetchone()
        if row is None or row["vo2max_central"] is None:
            return {"available": False}

        cutoff = (datetime.date.fromisoformat(target)
                  - datetime.timedelta(days=90)).isoformat()
        series = [
            {"date": r["date"], "central": r["vo2max_central"],
             "lo": r["vo2max_lo"], "hi": r["vo2max_hi"]}
            for r in con.execute(
                "SELECT date, vo2max_central, vo2max_lo, vo2max_hi "
                "FROM fitness_estimate WHERE date BETWEEN ? AND ? "
                "AND vo2max_central IS NOT NULL ORDER BY date", (cutoff, target))
        ]
        delta = (round(series[-1]["central"] - series[0]["central"], 1)
                 if len(series) >= 2 else None)

        cfg = body.get_config(con) or {}
        weight, _bf = body.weight_at(con, target)
        h_cm = cfg.get("height_cm")
        bmi = round(weight / (h_cm / 100) ** 2, 1) if (weight and h_cm) else None
        b = cfg.get("birth_date")
        age_ref = (datetime.date.fromisoformat(target)
                   - datetime.date.fromisoformat(b)).days // 365 if b else None
        waist_on = body.waist_measured_on(con)

        return {
            "available": True,
            "date": row["date"],
            "historical": row["date"] != today,
            "vo2max": {"central": row["vo2max_central"],
                       "lo": row["vo2max_lo"], "hi": row["vo2max_hi"],
                       "unit": "ml/kg/min"},
            "percentile": row["percentile"],
            "age_ref": age_ref, "sex": cfg.get("sex"),
            "pa_index": row["pa_index"], "pa_read_as": row["pa_read_as"],
            "confidence": row["confidence"], "why_spread": row["why_spread"],
            "trend_90d": series, "delta_90d": delta,
            "estimators": {"uth": row["vo2max_uth"], "nes": row["vo2max_nes"]},
            "hrr": ({"value": row["hrr_1min"], "at2": row["hrr_2min"],
                     "n": row["hrr_n"],
                     "rating": fitness.hrr_rating(row["hrr_1min"])}
                    if row["hrr_1min"] is not None else None),
            "inputs": {"hrmax": row["hrmax"], "rhr_med": row["rhr_med"],
                       "waist_cm": None if row["waist_estimated"] else row["waist_cm"],
                       "bmi": bmi},
            "waist_estimated": bool(row["waist_estimated"]),
            "waist_measured_on": waist_on,
        }
    finally:
        con.close()


@router.get("/api/energy/activity")
def energy_activity(date: str, start_min: int, duration_min: int):
    """Marginal kcal for one bout of activity.

    "Marginal" is the point: the resting cost of the same minutes is subtracted,
    so the number is what the activity added rather than what the body spent
    while it happened. The day's whole minute curve is recomputed and sliced, so
    a bout can never disagree with the day total it sits inside.
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    if duration_min <= 0 or duration_min > 1440 or not (0 <= start_min < 1440):
        raise HTTPException(400, "start_min 0-1439 and duration_min 1-1440")
    return calories.activity_cost(date, start_min, duration_min)


@router.get("/api/energy/day-activities")
def energy_day_activities(date: str):
    """Detected bouts on one day, each with what it cost.

    Lives next to the Energy-out panel because that is where the question gets
    asked -- the panel's "activity" figure is the whole day's movement, which is
    not the same as "what did that run cost me".
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    con = open_db()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, date, start_min, duration_min, work_min, cooldown_min, "
            "peak_hr, status, label FROM pai_detection WHERE date = ? "
            "ORDER BY start_min", (date,))]
    finally:
        con.close()
    for r in rows:
        r["start_clock"] = pai.fmt_clock(r["start_min"])
    with_energy(rows)
    # Cooldown counts toward the activities band. It is still spent because of
    # the session -- stretching after a run is part of the run, not incidental
    # daily movement -- so leaving it in the "movement" band would attribute it
    # to walking around the house. The two are still reported apart per bout;
    # only the day's band total merges them.
    return {"date": date, "activities": rows,
            "total_kcal": sum((r.get("kcal") or 0) + (r.get("cooldown_kcal") or 0)
                              for r in rows),
            "effort_kcal": sum(r.get("kcal") or 0 for r in rows),
            "cooldown_kcal": sum(r.get("cooldown_kcal") or 0 for r in rows)}


@router.get("/api/energy/sessions")
def energy_sessions(limit: int = Query(20, ge=1, le=100)):
    """Every app-tracked workout with what it cost.

    Strength minutes are priced from a fixed compendium MET rather than heart
    rate (HR is a poor energy proxy under load), so these numbers scale with
    duration and are less personalised than an HR-driven bout -- which is worth
    knowing when reading them next to a football match.
    """
    con = open_db()
    try:
        rows = con.execute(
            "SELECT id, date_iso, title, duration_sec, volume, avg_rpe "
            "FROM workout_session ORDER BY date_iso DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        item = {k: r[k] for k in r.keys()}
        try:
            item["energy"] = calories.session_cost(r["date_iso"], r["duration_sec"])
        except Exception as exc:                       # a day with no strap data
            item["energy"] = {"error": f"{type(exc).__name__}: {exc}"[:120]}
        out.append(item)
    return {"sessions": out, "count": len(out)}
