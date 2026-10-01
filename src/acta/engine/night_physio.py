"""night_physio — persist per-night RHR / sleep-HR / HRV into acta.db.

sleep_score.py already reads these from Gadgetbridge for the c_physio points,
but discards the raw values. This module stores the longitudinal series so
trends (illness / overtraining early-warning) become possible.

Sources (same tables + filters sleep_score.py uses):
  HUAMI_HEART_RATE_RESTING_SAMPLE  — device-computed RHR, ~1 sample at wake
  HUAMI_EXTENDED_ACTIVITY_SAMPLE   — per-minute HR (min sleeping HR)
  GENERIC_HRV_VALUE_SAMPLE         — HRV ms samples
TIMESTAMP UNITS DIFFER PER config.GADGETBRIDGE_DB TABLE:
  HUAMI_EXTENDED_ACTIVITY_SAMPLE   → epoch SECONDS
  HUAMI_HEART_RATE_RESTING_SAMPLE  → epoch MILLISECONDS
  GENERIC_HRV_VALUE_SAMPLE         → epoch MILLISECONDS
(acta *_ts are always milliseconds.)

Incremental + idempotent: fills nights present in sleep_score but missing in
night_physio. Called from the ingest pipeline; safe to run any time:
    python -m acta.engine.night_physio            # update (backfills on first run)
    python -m acta.engine.night_physio --show     # print the series
"""
import copy
import datetime
import json
import sqlite3
import sys

from acta import config

TZ = config.TZ
# intensity label but no per-set RPE. Maps to a session-RPE (0-10) so cardio and
# strength share one internal-load scale (session-RPE load = RPE × minutes).
INTENSITY_RPE = {"leve": 3.0, "moderado": 5.0, "intenso": 8.0}
# Fallback "typical session" load used only until there are ≥3 RPE'd sessions to
# build a personal baseline from. Set near the user's actual short/dense sessions
# (~100-110 sRPE) rather than a generic 5×45=225, so the cold-start window isn't
# under-sensitive. Superseded by _srpe_baseline() the moment history exists.
SRPE_REF = 120.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS night_physio (
  night_of     TEXT PRIMARY KEY,   -- YYYY-MM-DD, same key as sleep_score
  resting_hr   INTEGER,            -- device RHR at wake (bpm)
  min_sleep_hr INTEGER,            -- lowest per-minute HR while asleep (bpm)
  hr_dip       INTEGER,            -- resting_hr - min_sleep_hr
  hrv_mean     REAL,               -- mean HRV during sleep window (ms)
  computed_at  TEXT
)
"""


def _night_values(gb: sqlite3.Connection, bed_ms: int, wake_ms: int):
    resting = gb.execute(
        "SELECT HEART_RATE FROM HUAMI_HEART_RATE_RESTING_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND HEART_RATE > 0 "
        "ORDER BY TIMESTAMP LIMIT 1",
        (bed_ms, wake_ms + 4 * 3600 * 1000),  # ms table; RHR sample lands at/after wake
    ).fetchone()
    resting_hr = resting[0] if resting else None

    row = gb.execute(
        "SELECT MIN(HEART_RATE) FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND HEART_RATE > 30 AND HEART_RATE != 255",
        (bed_ms // 1000, wake_ms // 1000),  # seconds table
    ).fetchone()
    min_sleep_hr = row[0] if row else None

    row = gb.execute(
        "SELECT AVG(VALUE) FROM GENERIC_HRV_VALUE_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND VALUE BETWEEN 10 AND 300",
        (bed_ms, wake_ms),  # ms table
    ).fetchone()
    hrv_mean = round(row[0], 1) if row and row[0] is not None else None

    hr_dip = (resting_hr - min_sleep_hr) if (resting_hr and min_sleep_hr) else None
    return resting_hr, min_sleep_hr, hr_dip, hrv_mean


# Tail nights are refreshed even when a row exists: ingest re-scores the last
# RESCORE_TAIL_NIGHTS nights as fuller data arrives, and these values follow
# sleep_score's bedtime/waketime window. INSERT OR REPLACE makes re-running free.
REFRESH_TAIL_NIGHTS = 2


def update() -> int:
    """Fill missing nights, and refresh the last REFRESH_TAIL_NIGHTS regardless.
    Returns number of nights written."""
    acta = sqlite3.connect(config.ACTA_DB, timeout=30)
    acta.execute(SCHEMA)
    todo = acta.execute(
        "SELECT s.night_of, s.bedtime_ts, s.waketime_ts FROM sleep_score s "
        "LEFT JOIN night_physio p ON p.night_of = s.night_of "
        "WHERE s.bedtime_ts IS NOT NULL AND s.waketime_ts IS NOT NULL "
        "  AND (p.night_of IS NULL OR s.night_of >= ("
        "        SELECT MIN(night_of) FROM ("
        "          SELECT night_of FROM sleep_score ORDER BY night_of DESC LIMIT ?)))"
        " ORDER BY s.night_of",
        (REFRESH_TAIL_NIGHTS,),
    ).fetchall()
    if not todo:
        acta.close()
        return 0

    gb = sqlite3.connect(f"file:{config.GADGETBRIDGE_DB}?mode=ro", uri=True)
    now_iso = datetime.datetime.now().isoformat(timespec="seconds")
    written = 0
    for night_of, bed_ms, wake_ms in todo:
        vals = _night_values(gb, bed_ms, wake_ms)
        if all(v is None for v in vals):
            continue  # no config.GADGETBRIDGE_DB data for that window (pruned); on a tail refresh this
                      # also means the existing row is left alone rather than blanked
        acta.execute(
            "INSERT OR REPLACE INTO night_physio VALUES (?,?,?,?,?,?)",
            (night_of, *vals, now_iso),
        )
        written += 1
    acta.commit()
    acta.close()
    gb.close()
    return written


def rhr_series(days: int = 33) -> list[tuple[str, int]]:
    """(night_of, resting_hr) for the last `days` nights, oldest first."""
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT night_of, resting_hr FROM night_physio "
        "WHERE resting_hr IS NOT NULL ORDER BY night_of DESC LIMIT ?",
        (days,),
    ).fetchall()
    con.close()
    return rows[::-1]


def rhr_trend(elevated_bpm: int = 5, run_nights: int = 3):
    """Returns (last_rhr, baseline, warning:str|None).

    baseline = median of the series excluding the most recent `run_nights`;
    warning fires when ALL of the last `run_nights` are >= baseline + elevated_bpm.
    """
    import statistics
    series = rhr_series()
    if len(series) < run_nights + 7:  # need some history for a meaningful baseline
        return (series[-1][1] if series else None), None, None
    recent = [hr for _, hr in series[-run_nights:]]
    baseline = statistics.median(hr for _, hr in series[:-run_nights])
    warning = None
    if all(hr >= baseline + elevated_bpm for hr in recent):
        warning = (f"❤️ Resting HR elevated for {run_nights} nights ({', '.join(map(str, recent))} bpm "
                   f"vs baseline {baseline:.0f}): possible illness or accumulated fatigue. "
                   f"Consider easing off training.")
    return recent[-1], baseline, warning


RHR_FACTOR_START = "2026-07-20"  # forward-only gate — history is never re-shaped


def recharge_factor_info(night_of):
    """(factor, rhr, baseline) for the night waking on `night_of`.
    factor is clamped to [0.85, 1.10]; returns (1.0, rhr, baseline) when the
    date is gated or history is thin, (1.0, None, None) when no data."""
    import statistics
    try:
        con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
        row = con.execute(
            "SELECT resting_hr FROM night_physio WHERE night_of=? AND resting_hr IS NOT NULL",
            (night_of,)).fetchone()
        hist = [r[0] for r in con.execute(
            "SELECT resting_hr FROM night_physio "
            "WHERE night_of < ? AND resting_hr IS NOT NULL "
            "ORDER BY night_of DESC LIMIT 30", (night_of,))]
        con.close()
    except sqlite3.Error:
        return 1.0, None, None
    if not row or len(hist) < 7:
        return 1.0, (row[0] if row else None), None
    baseline = statistics.median(hist)
    if night_of < RHR_FACTOR_START:
        return 1.0, row[0], baseline
    return max(0.85, min(1.10, baseline / row[0])), row[0], baseline


def recharge_factor(night_of):
    return recharge_factor_info(night_of)[0]


def _last_vs_baseline(rows, run_nights=3, min_hist=7):
    """rows: [(night_of, value)] oldest-first → (last3, baseline) or None."""
    import statistics
    if len(rows) < run_nights + min_hist:
        return None
    recent = [v for _, v in rows[-run_nights:]]
    baseline = statistics.median(v for _, v in rows[:-run_nights])
    return recent, baseline


# Acute-deterioration detector (fast 2-night nosedive), complementing the slower
# 3-night sustained rule in recovery_alarm(), which misses a sudden cliff after a
# good night. Tuned to be earlier, not noisier: the window must be monotonically
# worsening, so a single random bad night can't trip it.
ACUTE_RUN      = 2      # nights that must all be past-threshold AND worsening
ACUTE_HRV_SOFT = 0.85   # every night in the window <= baseline × this
ACUTE_HRV_HARD = 0.80   # latest night <= baseline × this
ACUTE_RHR_SOFT = 3      # every night in the window >= baseline + this (bpm)
ACUTE_RHR_HARD = 5      # latest night >= baseline + this (bpm)


def _acute_move(rows, direction, soft, hard, run=ACUTE_RUN, min_hist=7):
    """Fast-deterioration check over the last `run` nights.

    rows: [(night_of, value)] oldest-first. direction 'up' (RHR: higher is worse)
    or 'down' (HRV: lower is worse). Fires only when ALL of:
      - every night in the window is past the `soft` threshold,
      - the latest night is past the `hard` threshold,
      - the window is monotonically worsening (each night at least as bad as the
        one before) — so a single isolated bad night cannot trip it.
    baseline = median of every night before the window.
    Returns (fired: bool, window_values: list, baseline: float) or None if the
    history is too thin to judge."""
    import statistics
    if len(rows) < run + min_hist:
        return None
    recent = [v for _, v in rows[-run:]]
    baseline = statistics.median(v for _, v in rows[:-run])
    if direction == "down":
        past_soft = all(v <= baseline * soft for v in recent)
        past_hard = recent[-1] <= baseline * hard
        worsening = all(recent[i] <= recent[i - 1] for i in range(1, len(recent)))
    else:  # 'up'
        past_soft = all(v >= baseline + soft for v in recent)
        past_hard = recent[-1] >= baseline + hard
        worsening = all(recent[i] >= recent[i - 1] for i in range(1, len(recent)))
    return (past_soft and past_hard and worsening), recent, baseline


def recovery_alarm(run_nights=3):
    """Illness / overtraining detector — two complementary patterns.

    SUSTAINED (slow, specific). Each signal fires when ALL of the last
    `run_nights` nights are beyond threshold vs baseline:
      RHR   >= baseline + 5 bpm
      HRV   <= baseline * 0.90
      sleep <= baseline - 5 points
    2 of 3 → "soft"; 3 of 3 → "hard" (+ illness_fingerprint snapshot, taken by ingest).

    ACUTE (fast, earlier). Catches a 2-night nosedive the sustained rule misses
    (see _acute_move): RHR spiking or HRV crashing over 2 worsening nights → a
    "soft" nudge. Both acute signals at once → a stronger "soft" wording. This is
    the early heads-up that fires before 3 bad nights have accumulated.

    Returns (level, message, signals): level None | "soft" | "hard".
    `signals` carries every check for transparency: rhr/hrv/sleep (sustained) and
    rhr_acute/hrv_acute (acute).
    """
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    rhr_rows = con.execute(
        "SELECT night_of, resting_hr FROM night_physio WHERE resting_hr IS NOT NULL "
        "ORDER BY night_of DESC LIMIT 33").fetchall()[::-1]
    hrv_rows = con.execute(
        "SELECT night_of, hrv_mean FROM night_physio WHERE hrv_mean IS NOT NULL "
        "ORDER BY night_of DESC LIMIT 33").fetchall()[::-1]
    sleep_rows = con.execute(
        "SELECT night_of, score FROM sleep_score "
        "ORDER BY night_of DESC LIMIT 33").fetchall()[::-1]
    con.close()

    # ── sustained (3-night) ──
    signals = {}
    r = _last_vs_baseline(rhr_rows, run_nights)
    if r:
        recent, base = r
        signals["rhr"] = all(v >= base + 5 for v in recent)
    h = _last_vs_baseline(hrv_rows, run_nights)
    if h:
        recent, base = h
        signals["hrv"] = all(v <= base * 0.90 for v in recent)
    s = _last_vs_baseline(sleep_rows, run_nights)
    if s:
        recent, base = s
        signals["sleep"] = all(v <= base - 5 for v in recent)

    fired = [k for k in ("rhr", "hrv", "sleep") if signals.get(k)]
    labels = {"rhr": "resting HR up", "hrv": "HRV down", "sleep": "sleep getting worse"}
    desc = ", ".join(labels[k] for k in fired)

    # ── acute (2-night nosedive) ──
    acute_msgs = []
    ra = _acute_move(rhr_rows, "up", ACUTE_RHR_SOFT, ACUTE_RHR_HARD)
    if ra:
        signals["rhr_acute"] = ra[0]
        if ra[0]:
            traj = "→".join(str(int(v)) for _, v in rhr_rows[-3:])
            acute_msgs.append(f"resting HR rising ({traj} bpm, baseline {ra[2]:.0f})")
    ha = _acute_move(hrv_rows, "down", ACUTE_HRV_SOFT, ACUTE_HRV_HARD)
    if ha:
        signals["hrv_acute"] = ha[0]
        if ha[0]:
            traj = "→".join(str(int(v)) for _, v in hrv_rows[-3:])
            acute_msgs.append(f"HRV falling ({traj} ms, baseline {ha[2]:.0f})")
    both_acute = signals.get("rhr_acute") and signals.get("hrv_acute")

    # ── decide (most severe pattern wins) ──
    if len(fired) >= 3:
        return ("hard",
                f"🤒 STRONG recovery alarm: {desc}, {run_nights} nights "
                f"in a row on all 3 signals. The typical pattern of illness coming on or "
                f"serious accumulated fatigue. Ease off training and sleep more.", signals)
    if len(fired) == 2:
        extra = f" Falling fast: {'; '.join(acute_msgs)}." if acute_msgs else ""
        return ("soft",
                f"⚠️ Weak recovery signal: {desc} ({run_nights} nights).{extra} "
                f"Not an alarm yet, but listen to your body today.", signals)
    if acute_msgs:
        if both_acute:
            return ("soft",
                    f"📉 STRONG fast drop in recovery: {'; '.join(acute_msgs)}. "
                    f"Two autonomic signals falling together over 2 nights: "
                    f"possibly illness starting, dehydration or fatigue. Take it "
                    f"easy today, drink water and sleep more; if it is worse tomorrow, "
                    f"ease off training.", signals)
        return ("soft",
                f"📉 Fast drop in recovery: {'; '.join(acute_msgs)}. "
                f"A marker falling quickly. Listen to your body today; if it is worse "
                f"tomorrow, ease off training.", signals)
    return (None, None, signals)


def _load_events():
    import json
    try:
        with open(config.EVENTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _cardio_srpe_for_date(day_iso, events=None):
    """Session-RPE load from NON-strength workouts logged in events.json for a
    date. Strength is counted from workout_session (it carries a real avg_rpe),
    so kind='strength' is skipped here to avoid double-counting the mirror event
    that append_workout_event writes for every guided session."""
    events = _load_events() if events is None else events
    total = 0.0
    for e in events:
        if e.get("type") != "workout":
            continue
        if (e.get("kind") or "").lower() == "strength":
            continue
        if not str(e.get("datetime", "")).startswith(day_iso):
            continue
        dur = e.get("duration_min") or 0
        rpe = INTENSITY_RPE.get((e.get("intensity") or "").lower(), 5.0)
        total += rpe * dur
    return total


def srpe_load_for_date(con, day_iso):
    """Total session-RPE load (RPE × minutes) for a calendar day: strength from
    workout_session (avg_rpe × minutes) + cardio from events.json. Returns
    (srpe, strength_missing_rpe) — the flag is True when a strength session had
    no RPE, so the caller knows the strength part is undercounted."""
    srpe = 0.0
    strength_missing_rpe = False
    try:
        rows = con.execute(
            "SELECT avg_rpe, duration_sec FROM workout_session WHERE date_iso LIKE ?",
            (f"{day_iso}%",)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for rpe, dur_sec in rows:
        if rpe is None:
            strength_missing_rpe = True
            continue
        srpe += rpe * (dur_sec or 0) / 60.0
    srpe += _cardio_srpe_for_date(day_iso)
    return srpe, strength_missing_rpe


def _srpe_baseline(con):
    """Typical single-session sRPE (median of past strength sessions that have
    an RPE). None until there are ≥3, so readiness falls back to SRPE_REF."""
    import statistics
    vals = []
    try:
        for rpe, dur in con.execute(
            "SELECT avg_rpe, duration_sec FROM workout_session WHERE avg_rpe IS NOT NULL"):
            v = rpe * (dur or 0) / 60.0
            if v > 0:
                vals.append(v)
    except sqlite3.OperationalError:
        return None
    return statistics.median(vals) if len(vals) >= 3 else None


def recent_pain(days=None, now=None):
    """[(date_iso, [areas])] of what is flagged as sore/painful *today*.

    Reads the body map (`muscle_soreness`), which is also where the workout
    finish-screen pain chips are mirrored — one source, one reset rule. Soreness
    is a same-day self-report and does not carry over: if it still hurts
    tomorrow, it gets logged again.

    Surfaced by the readiness card and morning digest as a transparent rule —
    never fed into the score. `days` is accepted and ignored for call
    compatibility."""
    now = now or datetime.datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    # Same day boundary as /api/soreness/current: the most recent wake-up, or
    # local midnight when the night has not synced.
    midnight_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0)
                      .timestamp() * 1000)
    now_ms = int(now.timestamp() * 1000)
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT waketime_ts FROM sleep_score WHERE waketime_ts IS NOT NULL "
            "ORDER BY night_of DESC LIMIT 1").fetchone()
        wake_ms = row[0] if row and row[0] else None
        cutoff = wake_ms if (wake_ms and midnight_ms <= wake_ms <= now_ms) else midnight_ms
        rows = con.execute(
            "SELECT area FROM muscle_soreness m WHERE ts >= ? AND ts = "
            "(SELECT MAX(ts) FROM muscle_soreness WHERE area = m.area) "
            "ORDER BY severity DESC, area", (cutoff,)).fetchall()
    except sqlite3.OperationalError:
        rows = []            # pre-migration DB without the table
    finally:
        con.close()
    areas = [r[0] for r in rows]
    return [(now.date().isoformat(), areas)] if areas else []


def readiness(now=None):
    """0-100 readiness for today, or (None, None) without today's data.

    Weights: sleep score 40% · RHR deviation 25% · HRV deviation 20% ·
    yesterday's training load 15%. Load uses session-RPE (RPE × minutes) when
    available — counting cardio, which volume alone missed — and falls back to
    the volume term otherwise. Heuristic weights, revisit once
    training_response.py shows sRPE actually predicts recovery.
    Returns (score:int|None, parts:dict).
    """
    import datetime as dt
    import statistics
    now = now or dt.datetime.now(config.TZ)
    today = now.date().isoformat()
    yesterday = (now.date() - dt.timedelta(days=1)).isoformat()

    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    sleep = con.execute(
        "SELECT score FROM sleep_score WHERE night_of=?", (today,)
    ).fetchone()
    phys = con.execute(
        "SELECT resting_hr, hrv_mean FROM night_physio WHERE night_of=?", (today,)
    ).fetchone()
    if not sleep or not phys or phys[0] is None:
        con.close()
        return None, {}

    rhr_hist = [r[0] for r in con.execute(
        "SELECT resting_hr FROM night_physio WHERE night_of < ? "
        "AND resting_hr IS NOT NULL ORDER BY night_of DESC LIMIT 30", (today,))]
    hrv_hist = [r[0] for r in con.execute(
        "SELECT hrv_mean FROM night_physio WHERE night_of < ? "
        "AND hrv_mean IS NOT NULL ORDER BY night_of DESC LIMIT 30", (today,))]
    vol_row = con.execute(
        "SELECT volume FROM workout_session WHERE date_iso LIKE ?",
        (f"{yesterday}%",)).fetchone()
    vol_hist = [r[0] for r in con.execute(
        "SELECT volume FROM workout_session WHERE volume IS NOT NULL")]
    srpe, strength_no_rpe = srpe_load_for_date(con, yesterday)
    srpe_base = _srpe_baseline(con)
    con.close()

    parts = {"sleep": round(sleep[0])}

    # RHR: each bpm below baseline is +5 around a 50 midpoint
    if len(rhr_hist) >= 7:
        rhr_c = max(0.0, min(100.0, 50 + (statistics.median(rhr_hist) - phys[0]) * 5))
    else:
        rhr_c = 50.0
    parts["rhr"] = round(rhr_c)

    # HRV: percent deviation from baseline, 10% below → -25
    if phys[1] is not None and len(hrv_hist) >= 7:
        base = statistics.median(hrv_hist)
        hrv_c = max(0.0, min(100.0, 50 + (phys[1] / base - 1) * 250))
    else:
        hrv_c = 50.0
    parts["hrv"] = round(hrv_c)

    # Yesterday's load: fresh = 100, harder day = lower. Prefer session-RPE
    # (RPE x minutes), which counts cardio and intensity; fall back to the volume
    # term for a strength session with no RPE and no cardio.
    if srpe > 0:
        ref = srpe_base or SRPE_REF
        load_c = max(20.0, min(100.0, 100 - 60 * (srpe / ref)))
        parts["load_src"] = "srpe"
        # An unrated strength session contributes zero to srpe, so the day is
        # undercounted (e.g. hard unrated lifting + a short walk would look
        # nearly rested). Compute the volume estimate too and keep whichever
        # reports more fatigue.
        if strength_no_rpe and vol_row and vol_row[0]:
            med_vol = statistics.median(vol_hist) if vol_hist else vol_row[0]
            vol_c = max(20.0, min(100.0, 100 - 60 * (vol_row[0] / med_vol)))
            if vol_c < load_c:
                load_c = vol_c
                parts["load_src"] = "srpe+volume"
    elif vol_row and vol_row[0]:
        med_vol = statistics.median(vol_hist) if vol_hist else vol_row[0]
        load_c = max(20.0, min(100.0, 100 - 60 * (vol_row[0] / med_vol)))
        parts["load_src"] = "volume"
    else:
        load_c = 100.0
        parts["load_src"] = "rested"
    parts["load"] = round(load_c)

    # Pain/niggle flags from recent workouts — a transparent caution, shown on
    # the card and in the digest. NOT part of the score.
    parts["pain"] = recent_pain(days=4, now=now)

    score = round(0.40 * sleep[0] + 0.25 * rhr_c + 0.20 * hrv_c + 0.15 * load_c)
    return score, parts


DAY_KEYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Trim tiers mirror the readiness bands: green/amber (>=55) is left as-is, since
# the 55-74 band can still be a good day. Red (<55) is split into three cut sizes,
# purely from the score (no per-type history yet).
TRIM_TIERS = [(55, 0), (40, 20), (25, 40), (0, 60)]


def training_trim_pct(score):
    """Map a readiness score to a training-load trim: 0/20/40/60%."""
    if score is None:
        return None
    for threshold, pct in TRIM_TIERS:
        if score >= threshold:
            return pct
    return 60


def _trim_exercise_sets(sets, frac):
    """Drop the tail of a strength exercise's not-yet-done sets, keeping >=1."""
    undone = [i for i, s in enumerate(sets) if not s.get("done")]
    if not undone:
        return sets
    n_drop = min(round(len(sets) * frac), len(undone) - 1)
    if n_drop <= 0:
        return sets
    drop = set(undone[-n_drop:])
    return [s for i, s in enumerate(sets) if i not in drop]


def apply_training_trim(day_plan, pct):
    """Readiness-driven cut applied to a COPY of a workout_plan day entry.

    Cardio (mode=="time"/cardio day): scales each not-yet-done set's
    durationSec, rounded to the nearest minute. Strength: drops the tail of
    each exercise's not-yet-done sets (at least 1 kept). Never mutates the
    input - the source also carries done/actualSec execution history.
    Returns (trimmed_day, cardio_before_sec, cardio_after_sec); the before/
    after seconds are 0 for a strength day.
    """
    trimmed = copy.deepcopy(day_plan)
    if not pct:
        return trimmed, 0, 0
    frac = pct / 100
    variants = trimmed.get("variants") or []
    pick = trimmed.get("pick") or 0
    if pick < 0 or pick >= len(variants):
        pick = 0
    if not variants:
        return trimmed, 0, 0
    is_cardio = bool(trimmed.get("cardio"))
    before = after = 0
    for ex in variants[pick].get("exercises", []):
        sets = ex.get("sets") or []
        if is_cardio or ex.get("mode") == "time":
            for s in sets:
                sec = s.get("durationSec") or 0
                before += sec
                if s.get("done") or not sec:
                    after += sec
                    continue
                new_sec = max(30, round(sec * (1 - frac) / 60) * 60)
                s["durationSec"] = new_sec
                after += new_sec
        else:
            ex["sets"] = _trim_exercise_sets(sets, frac)
    return trimmed, before, after


def training_trim(now=None):
    """Today's readiness-driven training-load suggestion.

    Reads workout_plan read-only and returns a trimmed COPY of today's
    entry - never persists anything back to the plan table. None when
    readiness isn't known yet, today is a rest day, or the tier is 0
    (nothing to cut).
    """
    now = now or datetime.datetime.now(TZ)
    score, _parts = readiness(now=now)
    pct = training_trim_pct(score)
    if not pct:
        return None
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    row = con.execute("SELECT plan_json FROM workout_plan WHERE id=1").fetchone()
    con.close()
    if not row:
        return None
    plan = json.loads(row[0])
    key = DAY_KEYS[now.weekday()]
    day = plan.get(key)
    if not day or not day.get("variants") or day.get("type") == "Rest":
        return None
    variants = day.get("variants") or []
    pick = day.get("pick") or 0
    if pick < 0 or pick >= len(variants):
        pick = 0
    all_sets = [s for ex in variants[pick].get("exercises", [])
                for s in (ex.get("sets") or [])]
    if all_sets and all(s.get("done") for s in all_sets):
        return None  # already logged in full - nothing left to suggest
    trimmed, before_sec, after_sec = apply_training_trim(day, pct)
    if day.get("cardio") and before_sec:
        summary = (f"{trimmed.get('type')}: {round(after_sec / 60)}min "
                   f"instead of {round(before_sec / 60)}min")
    else:
        summary = f"{trimmed.get('type')}: cut ~{pct}% of today's sets"
    return {"score": score, "pct": pct, "day_key": key,
            "type": trimmed.get("type"), "plan": trimmed, "summary": summary}


if __name__ == "__main__":
    if "--show" in sys.argv:
        for night, hr in rhr_series(days=90):
            print(night, hr)
        last, base, warn = rhr_trend()
        print(f"\nlast={last} baseline={base} warning={warn or 'none'}")
    else:
        n = update()
        print(f"night_physio: {n} nights written")
