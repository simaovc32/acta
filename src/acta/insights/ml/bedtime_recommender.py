"""
ml/bedtime_recommender.py

Recommends tonight's optimal bedtime to maximise sleep score.
Uses OLS linear regression on historical nights. Pure stdlib — no numpy.

Features per night:
  intercept, bio_at_bed, bed_hour_virt, alcohol_flag, bed_dev

Target: sleep score (0–100)

Feature history (2026-07-02): backtested every candidate feature via leave-one-out
cross-validation over 65 nights. Added `bed_dev` (bedtime deviation from the trailing
median) — the strongest single predictor (r=-0.47), because schedule regularity is 20%
of the actual sleep score and is fully knowable at bedtime. Removed `workout_flag`
(r=0.00, pure noise) and `drain_2h` (marginal; models without it scored better).
Momentum features (prev_score / avg3) were tested and rejected — they hurt (sleep
scores are not autocorrelated). Result: LOO MAE 7.6 -> 7.4, R² 0.08 -> 0.13.
`bed_dev` also gives the recommendation curve a real sweet spot at the habitual
bedtime instead of the old monotonic "earlier is always better".
"""

import datetime
import json
import sqlite3
import statistics

from acta import config

# Shared OLS math (works both as `ml.bedtime_recommender` import and standalone)
from acta.insights.ml.ols import _dot, _ols

TZ          = config.TZ
MIN_NIGHTS  = 10
REG_WINDOW  = 14   # nights of trailing history for the regularity baseline


# ── Helpers ───────────────────────────────────────────────────────────────────

def _virt_hour(dt: datetime.datetime) -> float:
    """Returns virtual hour where midnight = 24.0 to avoid discontinuity."""
    h = dt.hour + dt.minute / 60
    return h + 24 if h < 6 else h


def _bed_deviation(bed_hr: float, history: list) -> float:
    """Absolute deviation (hours) of a bedtime from the trailing median bedtime.

    Captures schedule regularity — a large driver of the actual sleep score and,
    unlike hours-slept or stages, fully knowable at bedtime. `history` is the list
    of prior nights' virtual bed hours in chronological order; only the last
    REG_WINDOW are used. Zero deviation at the habitual bedtime, growing either
    side, so the recommendation curve peaks at the usual time.
    """
    window = history[-REG_WINDOW:]
    if not window:
        return 0.0
    return abs(bed_hr - statistics.median(window))


def _load_events() -> list:
    try:
        with open(config.EVENTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def _bio_at(con, ms: int) -> float | None:
    row = con.execute(
        "SELECT level FROM biocharge WHERE minute_ts <= ? ORDER BY minute_ts DESC LIMIT 1",
        (ms,),
    ).fetchone()
    return row[0] if row else None


def _alcohol_within_6h(events: list, bed_dt: datetime.datetime) -> int:
    for evt in events:
        if evt.get("type") != "alcohol":
            continue
        try:
            evt_dt = datetime.datetime.strptime(evt["datetime"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except (ValueError, KeyError):
            continue
        diff_h = (bed_dt - evt_dt).total_seconds() / 3600
        if 0 <= diff_h <= 6:
            return 1
    return 0


# ── Training ──────────────────────────────────────────────────────────────────

def _build_dataset(con, events: list):
    nights = con.execute(
        "SELECT night_of, score, bedtime_ts FROM sleep_score "
        "WHERE bedtime_ts IS NOT NULL ORDER BY night_of",
    ).fetchall()

    X, y = [], []
    bed_hours_hist = []   # virtual bed hours of all prior nights, chronological
    for n in nights:
        bed_ms  = n["bedtime_ts"]
        score   = n["score"]
        bed_dt  = datetime.datetime.fromtimestamp(bed_ms / 1000, TZ)
        bed_hr  = _virt_hour(bed_dt)

        bio_bed = _bio_at(con, bed_ms)
        if bio_bed is not None:
            features = [
                1.0,
                bio_bed,
                bed_hr,
                _alcohol_within_6h(events, bed_dt),
                _bed_deviation(bed_hr, bed_hours_hist),
            ]
            X.append(features)
            y.append(score)

        # Record every slept night in the regularity baseline, even those without
        # biocharge data — the median schedule should reflect all nights.
        bed_hours_hist.append(bed_hr)

    return X, y


def train(con, events: list):
    X, y = _build_dataset(con, events)
    if len(X) < MIN_NIGHTS:
        return None, len(X)
    return _ols(X, y), len(X)


# ── Prediction ────────────────────────────────────────────────────────────────

def _candidate_ms(slot: float, ref: datetime.datetime) -> int:
    h = int(slot) % 24
    m = int((slot % 1) * 60)
    dt = ref.replace(hour=h, minute=m, second=0, microsecond=0)
    if h < 6:
        dt += datetime.timedelta(days=1)
    return int(dt.timestamp() * 1000)


def recommend(w: list, n_nights: int, con, events: list) -> dict:
    now    = datetime.datetime.now(TZ)
    now_ms = int(now.timestamp() * 1000)

    # Drain rate from last 30 waking minutes
    pts = con.execute(
        "SELECT minute_ts, level FROM biocharge "
        "WHERE minute_ts BETWEEN ? AND ? "
        "AND (why_label != 'sleep' OR why_label IS NULL) "
        "ORDER BY minute_ts",
        (now_ms - 30 * 60_000, now_ms),
    ).fetchall()

    if len(pts) >= 5:
        span_min = (pts[-1][0] - pts[0][0]) / 60_000
        drain_per_min = (pts[0][1] - pts[-1][1]) / max(1.0, span_min)
        drain_per_min = max(0.005, min(0.3, drain_per_min))
    else:
        drain_per_min = 0.015

    current_bio = _bio_at(con, now_ms) or 50.0

    # Trailing bed-hour history for the regularity baseline (most recent nights)
    hist_rows = con.execute(
        "SELECT bedtime_ts FROM sleep_score WHERE bedtime_ts IS NOT NULL "
        "ORDER BY night_of DESC LIMIT ?",
        (REG_WINDOW,),
    ).fetchall()
    bed_hist = [
        _virt_hour(datetime.datetime.fromtimestamp(r[0] / 1000, TZ))
        for r in reversed(hist_rows)
    ]

    # Candidates: every 15 min from 21:00 to 02:30 (virtual 21.0–26.5)
    candidates = []
    slot = 21.0
    while slot <= 26.5:
        cand_ms      = _candidate_ms(slot, now)
        mins_until   = (cand_ms - now_ms) / 60_000
        bio_at_bed   = max(5.0, current_bio - drain_per_min * mins_until)

        cand_dt = datetime.datetime.fromtimestamp(cand_ms / 1000, TZ)
        alc     = _alcohol_within_6h(events, cand_dt)
        bed_dev = _bed_deviation(slot, bed_hist)

        features = [1.0, bio_at_bed, slot, alc, bed_dev]
        score    = max(0.0, min(100.0, _dot(w, features)))

        h = int(slot) % 24
        m = int((slot % 1) * 60)
        candidates.append({
            "time":            f"{h:02d}:{m:02d}",
            "predicted_score": int(round(score)),
            "bio_at_bed":      round(bio_at_bed, 1),
            "ms":              cand_ms,
        })
        slot += 0.25

    # Only future candidates (or up to 30 min in past)
    future = [c for c in candidates if c["ms"] >= now_ms - 30 * 60_000]
    if not future:
        future = candidates[-4:]

    best = max(future, key=lambda c: c["predicted_score"])

    # Strip internal ms field from output
    clean = [{"time": c["time"], "predicted_score": c["predicted_score"],
              "bio_at_bed": c["bio_at_bed"]} for c in candidates]

    return {
        "recommended_time": best["time"],
        "predicted_score":  int(round(best["predicted_score"])),
        "bio_at_best":      best["bio_at_bed"],
        "model_nights":     n_nights,
        "candidates":       clean,
        "note":             None,
    }


# ── Public entry point ────────────────────────────────────────────────────────

def run() -> dict:
    events = _load_events()
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        w, n = train(con, events)
        if w is None:
            return {
                "recommended_time": None,
                "predicted_score":  None,
                "bio_at_best":      None,
                "model_nights":     n,
                "candidates":       [],
                "note":             f"Need {MIN_NIGHTS} nights of data, have {n}",
            }
        return recommend(w, n, con, events)
    finally:
        con.close()
