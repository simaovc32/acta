"""
ml/mental_predictor.py

Predicts mental flow (cognitive clarity, 1–5) across today.

Hybrid model:
  predicted(t) = BASELINE(t) + CAFFEINE_BUMP(t)

BASELINE — OLS linear regression (pure stdlib) on logged mental_state entries
that were NOT under caffeine. Features per entry:
  intercept, hours_since_wake, hours_since_wake², sleep_score (centered)
Target: mental_state (1–5).

Entries logged while caffeine was active are held out of baseline training, so
caffeine_bump() doesn't count the coffee lift a second time.

CAFFEINE_BUMP — a separate additive module. Each logged coffee adds a decaying
lift that peaks ~40 min after intake and fades over ~3h. A cortisol-trough
bonus applies when the coffee is 2–3h post-wake. Magnitudes are hardcoded
until LEARN_MIN_ENTRIES caffeinated entries exist, at which point they are
measured from residuals against the clean baseline instead.
"""

import bisect
import datetime
import json
import sqlite3

from acta import config
from acta.insights.ml.ols import _dot, _ols, _r_squared

TZ          = config.TZ

MIN_ENTRIES = 20          # below this, no prediction (avoids overfit noise)
SCORE_CENTER = 70.0       # sleep_score centering constant for conditioning
FLOOR, CAP  = 1.0, 5.0    # mental_state scale bounds

# Caffeine module
CAFFEINE_WINDOW_MIN = 180
CAFFEINE_PEAK_MIN   = 40
CAFFEINE_BUMP_NORMAL = 1.0    # lift outside the cortisol-trough window
CAFFEINE_BUMP_OPTIMAL = 2.0   # lift when taken 2–3h post-wake
LEARN_MIN_ENTRIES = 20        # caffeinated entries needed before measuring the lift
LEARN_MIN_SHAPE   = 0.05      # ignore entries at the tail where shape≈0 (noise amplifier)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_events() -> list:
    try:
        with open(config.EVENTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def _tz_intervals(con) -> list:
    """(start_ms, utc_offset_min) per night, from sleep_score.

    A local copy of acta.api.days.tz_intervals() — the API imports THIS module,
    so importing it back would be circular. Same source of truth (sleep_score.tz_offset_min) and
    same carry-forward rule; if the two ever need to diverge, that is a bug.
    """
    try:
        rows = con.execute(
            "SELECT night_of, tz_offset_min FROM sleep_score "
            "WHERE tz_offset_min IS NOT NULL ORDER BY night_of").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        d = datetime.date.fromisoformat(r["night_of"])
        tz = datetime.timezone(datetime.timedelta(minutes=r["tz_offset_min"]))
        out.append((int(datetime.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000),
                    r["tz_offset_min"]))
    out.sort()
    return out


def _device_tz(ts_ms: int, intervals: list):
    """Zone the device was on at ts_ms; falls back to home before any offset."""
    if not intervals:
        return TZ
    i = bisect.bisect_right(intervals, (ts_ms, 10 ** 9)) - 1
    return TZ if i < 0 else datetime.timezone(datetime.timedelta(minutes=intervals[i][1]))


def _coffee_ms(events: list, intervals: list = None) -> list:
    """Return coffee-event timestamps in ms.

    Resolved against the zone the device was on that day, not the home zone:
    the stored string is bare wall clock, so a coffee logged abroad would
    otherwise shift the caffeine window.
    """
    intervals = intervals or []
    out = []
    for evt in events:
        if evt.get("type") != "coffee":
            continue
        try:
            naive = datetime.datetime.strptime(evt["datetime"], "%Y-%m-%d %H:%M")
        except (ValueError, KeyError):
            continue
        home_ms = int(naive.replace(tzinfo=TZ).timestamp() * 1000)
        out.append(int(naive.replace(tzinfo=_device_tz(home_ms, intervals)).timestamp() * 1000))
    return out


def _sleep_for_day(con, day: str):
    """Returns (score, waketime_ts) for the night whose wake-day is `day`, or None."""
    row = con.execute(
        "SELECT score, waketime_ts FROM sleep_score WHERE night_of = ?", (day,)
    ).fetchone()
    if not row or row["waketime_ts"] is None:
        return None
    return row["score"], row["waketime_ts"]


def _caffeine_shape(dt_min: float) -> float:
    """Lift envelope at dt_min after intake: ramp to peak, then decay to zero."""
    if dt_min < 0 or dt_min > CAFFEINE_WINDOW_MIN:
        return 0.0
    if dt_min <= CAFFEINE_PEAK_MIN:
        return dt_min / CAFFEINE_PEAK_MIN
    return max(0.0, 1 - (dt_min - CAFFEINE_PEAK_MIN) /
               (CAFFEINE_WINDOW_MIN - CAFFEINE_PEAK_MIN))


def caffeine_bump(t_ms: int, coffee_ms: list, waketime_ts: int, learned_coef=None) -> float:
    """Additive caffeine lift at time t_ms from all coffees active then."""
    bump = 0.0
    for c_ms in coffee_ms:
        shape = _caffeine_shape((t_ms - c_ms) / 60_000)
        if shape == 0.0:
            continue
        if learned_coef is not None:
            mag = learned_coef
        else:
            hrs_post_wake = (c_ms - waketime_ts) / 3_600_000 if waketime_ts else 0
            mag = CAFFEINE_BUMP_OPTIMAL if 2.0 <= hrs_post_wake <= 3.0 else CAFFEINE_BUMP_NORMAL
        bump += mag * shape
    return bump


# ── Training ──────────────────────────────────────────────────────────────────

def _build_dataset(con, coffee_ms: list):
    """Split mental_state entries into clean (baseline training) and caffeinated.

    Values outside FLOOR..CAP are legacy entries from a 1–10 scale and are
    dropped from both sets.
    """
    rows = con.execute(
        "SELECT ts, value FROM user_log WHERE kind = 'mental_state' ORDER BY ts"
    ).fetchall()
    clean, caffeinated = [], []
    for r in rows:
        if not FLOOR <= r["value"] <= CAP:
            continue
        dt = datetime.datetime.fromtimestamp(r["ts"] / 1000, TZ)
        sl = _sleep_for_day(con, dt.date().isoformat())
        if sl is None:
            continue
        score, wake_ts = sl
        hsw = max(0.0, (r["ts"] - wake_ts) / 3_600_000)
        feats = [1.0, hsw, hsw * hsw, score - SCORE_CENTER]

        active = [c for c in coffee_ms if 0 <= (r["ts"] - c) <= CAFFEINE_WINDOW_MIN * 60_000]
        if active:
            caffeinated.append((feats, r["value"], max(active), wake_ts, r["ts"]))
        else:
            clean.append((feats, r["value"]))
    return clean, caffeinated


def _learn_coef(w, caffeinated: list):
    """Measure the caffeine lift from residuals against the clean baseline.

    Returns None until LEARN_MIN_ENTRIES caffeinated entries exist, so the
    hardcoded magnitudes stay in force while the sample is too small to trust.
    """
    if len(caffeinated) < LEARN_MIN_ENTRIES:
        return None
    coefs = []
    for feats, value, c_ms, _wake_ts, ts in caffeinated:
        shape = _caffeine_shape((ts - c_ms) / 60_000)
        if shape < LEARN_MIN_SHAPE:
            continue
        coefs.append((value - _dot(w, feats)) / shape)
    if not coefs:
        return None
    return sum(coefs) / len(coefs)


def train(con, coffee_ms: list):
    clean, caffeinated = _build_dataset(con, coffee_ms)
    if len(clean) < MIN_ENTRIES:
        return None, len(clean), None, None
    X = [f for f, _ in clean]
    y = [v for _, v in clean]
    try:
        w = _ols(X, y)
    except ValueError:
        return None, len(clean), None, None
    return w, len(clean), _r_squared(X, y, w), _learn_coef(w, caffeinated)


# ── Prediction ────────────────────────────────────────────────────────────────

def predict_today(con, events: list) -> dict:
    coffee_ms = _coffee_ms(events, _tz_intervals(con))
    w, n, r2, learned = train(con, coffee_ms)
    if w is None:
        return {"curve": [], "model_entries": n, "r2": None, "caffeine_coef": None,
                "note": f"Need {MIN_ENTRIES} caffeine-free mental-state entries (have {n})"}

    now   = datetime.datetime.now(TZ)
    today = now.date().isoformat()
    sl = _sleep_for_day(con, today)
    if sl is None:
        return {"curve": [], "model_entries": n, "r2": round(r2, 2),
                "caffeine_coef": learned,
                "note": "No sleep score for today yet — can't anchor wake time"}
    score, wake_ts = sl

    # Sweep from wake to 23:00 (local) in 15-min steps
    wake_dt = datetime.datetime.fromtimestamp(wake_ts / 1000, TZ)
    end_dt  = now.replace(hour=23, minute=0, second=0, microsecond=0)
    slot = wake_dt.replace(second=0, microsecond=0)
    curve = []
    while slot <= end_dt:
        t_ms = int(slot.timestamp() * 1000)
        hsw  = max(0.0, (t_ms - wake_ts) / 3_600_000)
        base = _dot(w, [1.0, hsw, hsw * hsw, score - SCORE_CENTER])
        val  = base + caffeine_bump(t_ms, coffee_ms, wake_ts, learned)
        val  = max(FLOOR, min(CAP, val))
        curve.append({
            "time": slot.strftime("%H:%M"),
            "hour": round(slot.hour + slot.minute / 60, 2),
            "predicted": round(val, 2),
        })
        slot += datetime.timedelta(minutes=15)

    return {"curve": curve, "model_entries": n, "r2": round(r2, 2),
            "caffeine_coef": round(learned, 2) if learned is not None else None,
            "note": None}


# ── Public entry point ────────────────────────────────────────────────────────

def run() -> dict:
    events = _load_events()
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return predict_today(con, events)
    finally:
        con.close()


if __name__ == "__main__":
    import pprint
    pprint.pprint(run())
