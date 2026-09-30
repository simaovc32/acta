"""Body: weight, calories burned and the soreness body map."""

import datetime
import json
import sqlite3
import statistics
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from acta.api.days import check_date
from acta.api.deps import open_db, open_db_rw
from acta.config import TZ
from acta.engine import fitness
from acta.tracking import body

router = APIRouter()


class WeightEntry(BaseModel):
    weight_kg:   Optional[float] = None
    bodyfat_pct: Optional[float] = None
    waist_cm:    Optional[float] = None   # navel-level, feeds the VO2max estimate
    date:        Optional[str] = None   # "YYYY-MM-DD"; defaults to today
    note:        Optional[str] = None


def _fitness_recompute_bg():
    try:
        n = fitness.recompute()
        print(f"[Acta] fitness recomputed after a waist change ({n} days)")
    except Exception as e:                                   # noqa: BLE001
        print(f"[Acta] fitness recompute after waist save failed: {e}")


@router.post("/api/log/weight")
def log_weight(entry: WeightEntry, background: BackgroundTasks):
    """Record a body weight and/or waist. Backdating is allowed so a missed day
    can be filled.

    Weight is a time series, not a constant: calorie estimates for a past date
    use the weight in effect on that date, and the later calibration loop reads
    the same series. Waist is a slow trait (feeds the VO2max estimate) — a new
    value triggers a full fitness_estimate recompute since it carries backward.
    """
    ts = datetime.datetime.now(TZ)
    if entry.date:
        try:
            d = datetime.date.fromisoformat(entry.date)
        except ValueError:
            raise HTTPException(400, "date must be YYYY-MM-DD") from None
        if d > datetime.date.today():
            raise HTTPException(400, "cannot log a weight in the future")
        ts = datetime.datetime(d.year, d.month, d.day, 8, 0, tzinfo=TZ)

    con = open_db_rw()
    try:
        body.ensure(con)
        try:
            stamp = body.log_weight(con, entry.weight_kg, ts,
                                    entry.bodyfat_pct, entry.note,
                                    waist_cm=entry.waist_cm)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        con.commit()
        if entry.waist_cm is not None:
            background.add_task(_fitness_recompute_bg)
        return {"status": "ok", "ts": stamp,
                "weight_kg": entry.weight_kg, "waist_cm": entry.waist_cm,
                "series": body.weight_series(con, 90)}
    finally:
        con.close()


@router.get("/api/body/weight")
def body_weight(days: int = Query(90, ge=1, le=730)):
    con = open_db_rw()
    try:
        body.ensure(con)
        series = body.weight_series(con, days)
        latest = series[-1] if series else None
        # 7-day trend: single-day weights are noisy (water/glycogen), so the
        # rolling mean is the number worth comparing.
        recent = [r["weight_kg"] for r in series[-7:]]
        prev   = [r["weight_kg"] for r in series[-14:-7]]
        trend  = None
        if len(recent) >= 3 and len(prev) >= 3:
            trend = round(statistics.mean(recent) - statistics.mean(prev), 2)
        today = datetime.date.today().isoformat()
        return {"latest": latest, "series": series,
                "avg_7d": round(statistics.mean(recent), 2) if recent else None,
                "trend_kg_per_week": trend,
                "waist_cm": body.waist_at(con, today),
                "waist_measured_on": body.waist_measured_on(con)}
    finally:
        con.close()


@router.get("/api/body/config")
def body_config():
    con = open_db_rw()
    try:
        body.ensure(con)
        cfg = body.get_config(con)
        if cfg is None:
            return {"configured": False}
        today = datetime.datetime.now(TZ).date().isoformat()
        target = body.protein_target(con, today)
        try:
            bmr, used = body.bmr_for_date(con, today)
        except RuntimeError as e:
            return {"configured": True, **cfg, "bmr_kcal_day": None,
                    "protein_target": target, "detail": str(e)}
        return {"configured": True, **cfg, "protein_target": target,
                "bmr_kcal_day": round(bmr), "inputs": used}
    finally:
        con.close()


class ProteinTargetIn(BaseModel):
    g_per_kg: float


@router.post("/api/body/protein-target")
def set_protein_target(entry: ProteinTargetIn):
    """Set the protein target ratio. The gram figure re-derives from bodyweight,
    so this never needs revisiting when the weight changes."""
    con = open_db_rw()
    try:
        body.ensure(con)
        try:
            body.set_protein_g_per_kg(con, entry.g_per_kg)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        con.commit()
        return {"status": "ok",
                "protein_target": body.protein_target(
                    con, datetime.datetime.now(TZ).date().isoformat())}
    finally:
        con.close()


CAL_LOW_COVERAGE = 70.0     # % of elapsed minutes with a valid HR reading


def _cal_row(r) -> dict:
    return {
        "date": r["date"], "total": r["total_kcal"],
        "rest": r["rest_kcal"], "active": r["active_kcal"],
        "coverage_pct": r["coverage_pct"], "elapsed_min": r["elapsed_min"],
        "complete": r["elapsed_min"] == 1440,
    }


@router.get("/api/calories/today")
def calories_today(date: Optional[str] = None):
    """Energy out for today (with a projection), or the settled total for `date`.

    The projection comes from how much of a day's total had typically landed by
    this hour over the last 14 complete days — a flat rate-extrapolation would
    over-read in the evening, when the remaining hours are mostly sleep. A day
    that is already complete needs no projection, so it does not get one.
    """
    con = open_db()
    try:
        if date:
            check_date(date)
            today = date
        else:
            today = datetime.datetime.now(TZ).date().isoformat()
        row = con.execute(
            "SELECT * FROM daily_energy WHERE date = ?", (today,)).fetchone()
        if row is None:
            return {"available": False,
                    "detail": f"no energy row for {today}"}

        hist = con.execute(
            "SELECT total_kcal, hourly_json FROM daily_energy "
            "WHERE elapsed_min = 1440 AND date < ? ORDER BY date DESC LIMIT 14",
            (today,)).fetchall()

        hours_done = len(json.loads(row["hourly_json"]))
        fracs = []
        for h in hist:
            cum = json.loads(h["hourly_json"])
            if len(cum) >= hours_done and h["total_kcal"]:
                fracs.append(cum[hours_done - 1] / h["total_kcal"])
        projected = None
        # Below ~8% of the day elapsed the ratio is too unstable to divide by.
        # A finished day is its own total — projecting it would just restate it.
        if (row["elapsed_min"] != 1440
                and len(fracs) >= 3 and statistics.median(fracs) > 0.08):
            projected = round(row["total_kcal"] / statistics.median(fracs))

        recent = con.execute(
            "SELECT total_kcal FROM daily_energy "
            "WHERE elapsed_min = 1440 AND date < ? ORDER BY date DESC LIMIT 7",
            (today,)).fetchall()
        avg_7d = round(statistics.mean(r["total_kcal"] for r in recent)) if recent else None

        return {
            "available": True,
            **_cal_row(row),
            "projected": projected,
            "avg_7d": avg_7d,
            "bmr_kcal_day": row["bmr_kcal_day"],
            "hourly": json.loads(row["hourly_json"]),
            "weight_kg": row["weight_kg"],
            "low_coverage": (row["coverage_pct"] or 0) < CAL_LOW_COVERAGE,
            "minutes": {
                "sleep": row["sleep_min"], "sedentary": row["sedentary_min"],
                "active": row["active_min"], "strength": row["strength_min"],
                "imputed": row["imputed_min"],
            },
        }
    finally:
        con.close()


@router.get("/api/calories/history")
def calories_history(days: int = Query(30, ge=2, le=180)):
    con = open_db()
    try:
        cutoff = (datetime.datetime.now(TZ).date()
                  - datetime.timedelta(days=days)).isoformat()
        rows = con.execute(
            "SELECT * FROM daily_energy WHERE date >= ? ORDER BY date",
            (cutoff,)).fetchall()
        series = [_cal_row(r) for r in rows]
        full = [s["total"] for s in series if s["complete"]]
        return {
            "series": series,
            "avg": round(statistics.mean(full)) if full else None,
            "avg_7d": round(statistics.mean(full[-7:])) if full else None,
            "bmr_kcal_day": rows[-1]["bmr_kcal_day"] if rows else None,
        }
    finally:
        con.close()


# Body-map areas. The eight joint slugs predate the map (they were the finish
# screen's pain chips) and are kept byte-identical so recent_pain() and older
# workout_session.pain_flags rows keep resolving.
SORENESS_MUSCLE = {
    "neck":       None,          # no exercise group — surfaces as a caution only
    "traps":      "Back",
    "shoulder":   "Shoulders",
    "chest":      "Chest",
    "lats":       "Back",
    "mid-back":   "Back",
    "lower-back": "Back",
    "biceps":     "Arms",
    "triceps":    "Arms",
    "forearm":    "Arms",
    "elbow":      "Arms",
    "wrist":      "Arms",
    "abs":        "Core",
    "obliques":   "Core",
    "glutes":     "Legs",
    "hip":        "Legs",
    "quads":      "Legs",
    "hamstrings": "Legs",
    "knee":       "Legs",
    "calves":     "Legs",
    "ankle":      "Legs",
}


SORENESS_DECAY_H = 72  # max lookback for an explicit `hours` override / clears


# What the feeling is, separate from how strong (severity 1 mild · 2 moderate · 3 severe):
# ache = muscle soreness, tight = stiff, heavy = tired muscle, sharp = joint / injury pain.
SORENESS_KINDS = ("ache", "tight", "heavy", "sharp")


def soreness_day_start_ms(now: datetime.datetime) -> int:
    """Start of the current soreness day.

    Soreness is a same-day self-report, not a DOMS decay curve: what was flagged
    yesterday must not still be showing today (2026-07-25). The boundary is the
    most recent wake-up rather than local midnight, because a bedtime after 00:00
    means a plain midnight cutoff would wipe an entry logged minutes earlier.
    Falls back to local midnight when the night hasn't synced yet."""
    midnight_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0)
                      .timestamp() * 1000)
    now_ms = int(now.timestamp() * 1000)
    try:
        con = open_db()
        try:
            row = con.execute(
                "SELECT waketime_ts FROM sleep_score WHERE waketime_ts IS NOT NULL "
                "ORDER BY night_of DESC LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        row = None
    wake_ms = row["waketime_ts"] if row and row["waketime_ts"] else None
    # Only trust the wake time if it is today's — a stale one (sync lag) would
    # otherwise widen the window back into previous days.
    if wake_ms and midnight_ms <= wake_ms <= now_ms:
        return wake_ms
    return midnight_ms


class SorenessEntry(BaseModel):
    # {area_slug: severity 1-3 | {"severity": 1-3, "kind": ache|tight|heavy|sharp}}. A bare number
    # means kind 'ache'; severity 0 clears the area.
    areas: dict
    note:  Optional[str] = None


@router.post("/api/log/soreness")
def log_soreness(entry: SorenessEntry):
    if not entry.areas:
        raise HTTPException(400, "at least one area required")
    clean = {}
    for area, val in entry.areas.items():
        if area not in SORENESS_MUSCLE:
            raise HTTPException(400, f"unknown area: {area}")
        kind = "ache"
        if isinstance(val, dict):
            sev = val.get("severity")
            kind = val.get("kind") or "ache"
        else:
            sev = val
        try:
            sev = int(sev)
        except (TypeError, ValueError):
            raise HTTPException(400, f"severity for {area} must be an integer") from None
        if not 0 <= sev <= 3:
            raise HTTPException(400, "severity must be 0-3")
        if kind not in SORENESS_KINDS:
            raise HTTPException(400, f"kind for {area} must be one of {', '.join(SORENESS_KINDS)}")
        clean[area] = (sev, kind)

    now_ms = int(datetime.datetime.now(TZ).timestamp() * 1000)
    note = (entry.note or "").strip() or None
    con = open_db_rw()
    try:
        for area, (sev, kind) in clean.items():
            if sev == 0:
                # Explicit "better now" — drop this area out of the window
                # instead of writing a zero row, so decay stays a simple
                # newest-row-wins lookup. Scoped to today so clearing never
                # deletes previous days' history.
                con.execute(
                    "DELETE FROM muscle_soreness WHERE area=? AND ts >= ?",
                    (area, soreness_day_start_ms(datetime.datetime.now(TZ))),
                )
            else:
                con.execute(
                    "INSERT OR REPLACE INTO muscle_soreness(ts, area, severity, note, kind) "
                    "VALUES(?,?,?,?,?)", (now_ms, area, sev, note, kind),
                )
        con.commit()
    finally:
        con.close()
    logged = {a: {"severity": s, "kind": k} for a, (s, k) in clean.items() if s > 0}
    return {"status": "ok", "logged": logged, "cleared": sorted(a for a, (s, _) in clean.items() if s == 0)}


@router.get("/api/soreness/current")
def soreness_current(hours: Optional[int] = None):
    """Newest severity per area logged today, plus the worst severity per exercise
    muscle group so the plan can flag what not to hammer today.

    Default window is the current day (see soreness_day_start_ms) — soreness does
    not carry over to tomorrow. `hours` forces an explicit rolling window instead,
    for diagnostics."""
    now = datetime.datetime.now(TZ)
    now_ms = int(now.timestamp() * 1000)
    if hours is None:
        cutoff = soreness_day_start_ms(now)
    else:
        if not 1 <= hours <= 336:
            raise HTTPException(400, "hours must be 1-336")
        cutoff = now_ms - hours * 3600 * 1000
    con = open_db_rw()
    try:
        rows = con.execute(
            "SELECT area, severity, ts, note, kind FROM muscle_soreness m WHERE ts >= ? "
            "AND ts = (SELECT MAX(ts) FROM muscle_soreness WHERE area = m.area) "
            "ORDER BY severity DESC, area", (cutoff,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()

    areas, muscles, detail = [], {}, {}
    for r in rows:
        muscle = SORENESS_MUSCLE.get(r["area"])
        kind = r["kind"] or "ache"
        areas.append({
            "area":     r["area"],
            "severity": r["severity"],
            "kind":     kind,
            "muscle":   muscle,
            "age_h":    round((now_ms - r["ts"]) / 3600000, 1),
            "note":     r["note"],
        })
        if muscle:
            muscles[muscle] = max(muscles.get(muscle, 0), r["severity"])
            # The entry the Workout chip shows for the group: strongest, sharp pain winning a tie.
            rank = (r["severity"], kind == "sharp")
            if muscle not in detail or rank > detail[muscle][0]:
                detail[muscle] = (rank, {"severity": r["severity"], "kind": kind})
    return {"areas": areas, "muscles": muscles,
            "muscle_detail": {m: d for m, (_, d) in detail.items()},
            "window_h": round((now_ms - cutoff) / 3600000, 1),
            "since_ms": cutoff}
