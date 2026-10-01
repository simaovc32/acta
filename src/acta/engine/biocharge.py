"""
BioCharge actions -- @1 stage-weighted recharge + @2 drain + @3 modifiers + @4 labels.
Batch-recompute wrapper (@5): auto-detects date range, runs all days in sequence,
writes biocharge_output.json. Re-running with the same DB produces the same file.

Stage mapping: corrected from working sleep_score.py code
  5=deep  4=light  8=REM  7=awake   (do NOT re-derive from any spec text)
This mapping is how it works for GadgetBridge. If you use another device, it might be different
"""

import datetime
import json
import statistics

from acta import config
from acta.engine.sleep_score import TZ

COLD_START = 40.0   # midnight level for the very first day (no prior carry-forward)

FLOOR, CAP  = 5, 100
AWAKE_CODE  = 7   # awake stage code (see mapping above)

# -- @1 recharge coefficients --------------------------------------------------
STAGE_WEIGHTS  = {5: 2.0, 8: 1.5, 4: 1.0, 7: 0.0}   # deep, REM, light, awake
SEED_MIN       = 15.0   # score=0   -> morning target
SEED_MAX       = 95.0   # score=100 -> morning target

# -- Naps (strap-detected daytime sleep) --------------------------------------
# The strap marks a nap with RAW_KIND=NAP_KIND but never writes a hypnogram for
# it, so each nap minute recharges at NAP_RECHARGE_RATE (the median per-minute
# night recharge, i.e. an average sleep minute). If a hypnogram does overlap,
# that rate is redistributed by real stage weight via NAP_STAGE_REF_WEIGHT.
NAP_KIND              = 120     # RAW_KIND marker for strap-detected sleep
NAP_RECHARGE_RATE     = 0.145   # pts/min, flat, when stages are unknown
NAP_STAGE_REF_WEIGHT  = 1.31    # night weight-sum / asleep-min (measured)
NAP_MIN_BLOCK_MIN     = 20      # shorter than this is lying still, not a nap
NAP_MAX_GAP_MIN       = 5       # bridge a turn-over inside the block
NAP_MIN_START_LAG_MIN = 150     # complement of EXT_MAX_START_LAG_MIN: later than this is a nap
NAP_MAX_MINUTES       = 120     # counted-minute cap per nap
NAP_MAX_RECHARGE      = 18.0    # pts cap per nap (backstop for all-day sleep)
NAP_LATEST_END_MIN    = 22 * 60 # a block ending after 22:00 is bedtime, not a nap
# Forward-only, same posture as HR_ZONE_TOP_START / night_physio.RHR_FACTOR_START.
NAP_DETECT_START      = "2026-08-29"

# -- Extend-sleep credit (return-to-sleep after the hypnogram ends) ------------
# The strap never restarts a hypnogram if you fall back asleep. detect_ext_sleep()
# recovers that from the same NAP_KIND signal, in the window just before naps
# start, so the two never overlap. Recharges at NAP_RECHARGE_RATE (unstaged).
# These mirror ingest.py's EXT_* constants -- keep both in sync by hand.
EXT_MIN_BLOCK_MIN     = 15      # shorter than this is lying still, not sleep
EXT_MAX_GAP_MIN       = 5       # bridge a turn-over inside the block
EXT_MAX_START_LAG_MIN = 150     # exact complement of NAP_MIN_START_LAG_MIN
EXT_LOOKAHEAD_MIN     = 300     # how far past the hypnogram's own wake to look
# Forward-only: nights before this get no extend-sleep recharge.
EXT_CREDIT_START      = "2026-09-05"

# -- @2 drain coefficients -----------------------------------------------------
BASE_DRAIN       = 0.010   # always applied while awake (pts/min)
STRESS_DRAIN_MAX = 0.050   # max additional drain from elevated stress
STRESS_SPREAD    = 20
BASELINE_WINDOW  = 21

# HR zone drain (pts/min added on top of BASE_DRAIN).
# Zones are defined by bpm above rhr_bl; thresholds are upper bounds of each zone.
#   Zone 0  Rest      < +10  above RHR  →  barely above resting
#   Zone 1  Light     +10–28            →  walking, bar, casual
#   Zone 2  Moderate  +28–53            →  brisk walk, dancing
#   Zone 3  Aerobic   +53–78            →  jogging, cycling
#   Zone 4  Hard      +78–113           →  running, sustained sport
#   Zone 5  Max       > +113            →  football, hockey, sprints
HR_ZONE_THRESHOLDS = [10, 28, 53, 78, 113]          # upper bound of zones 0-4
HR_ZONE_DRAINS     = [0.00, 0.01, 0.03, 0.07, 0.11, 0.17]  # zone 0-5
# Above the Zone-5 floor the drain keeps climbing at the Zone 4->5 slope,
# capped at HR_MAX so a spurious reading can't run away with the day.
HR_ZONE_TOP_SLOPE  = (HR_ZONE_DRAINS[5] - HR_ZONE_DRAINS[4]) / (HR_ZONE_THRESHOLDS[4] - HR_ZONE_THRESHOLDS[3])
HR_MAX             = 195      # measured from my body
# Forward-only: days before this keep a flat Zone-5 drain, so history isn't re-shaped.
HR_ZONE_TOP_START  = "2026-08-20"

# Ambient gate: when intensity is low and no workout is logged the zone drain
# is dampened — covers HR elevated by alcohol, excitement, etc. but not exercise.
AMBIENT_GATE_INTENSITY = 20    # RAW_INTENSITY below this → ambient
AMBIENT_GATE_FACTOR    = 0.35  # fraction of zone drain kept when ambient

# -- @3 modifiers: coffee and alcohol ------------------------------------------
COFFEE_DURATION       = 150    # minutes of drain suppression
COFFEE_SUPPRESS_MAX   = 0.60   # fraction suppressed when coffee is within COFFEE_FULL_WIN of wake
COFFEE_SUPPRESS_MIN   = 0.20   # fraction suppressed for a late-day coffee
COFFEE_FULL_WIN       = 180    # minutes from wake: full strength
COFFEE_DECAY_WIN      = 480    # minutes from wake: minimum strength reached
ALCOHOL_DURATION      = 240    # minutes of elevated drain
ALCOHOL_PRESLEEP_WIN  = 360    # minutes before bedtime: alcohol counts as "pre-sleep"

# Standard-drink units per serving (1 unit = 10g pure alcohol, the EU reference).
# Calibrated to real pours: volume(ml) x ABV x 0.789 (ethanol density) / 10.
#   wine  125ml @ 12%  -> 11.8g -> 1.2   (small glass)
#   beer  200ml @ 5%   ->  7.9g -> 0.8
#   cider 200ml @ 4.5% ->  7.1g -> 0.7
# Wine tops the list despite the smallest volume — it's ~2.5x stronger per ml.
# Unknown/legacy events with no "kind" fall back to 1.0 unit/drink.
ALCOHOL_UNITS_PER_DRINK   = {"wine": 1.2, "beer": 0.8, "cider": 0.7}
ALCOHOL_DEFAULT_UNIT      = 1.0

# Evening drain: alcohol raises HR / burns biocharge faster during the window.
# Scales with total standard-drink units (amount x units/drink), not just y/n.
ALCOHOL_DRAIN_PER_UNIT       = 0.30   # extra drain multiplier per unit
ALCOHOL_DRAIN_FACTOR_MAX     = 2.2    # cap so a heavy night doesn't blow up drain
# Alcohol's effect on sleep is applied in sleep_score.score_alcohol_penalty.

# -- Manual activity synthetic HR (strap not worn) -----------------------------
# Any logged workout minute with no real HR data gets HR = RHR baseline +
# offset * intensity, so strap-less activities (swimming, beach, gym) drain
# realistically instead of at resting rate. Real strap data always wins.
ACTIVITY_HR_OFFSET = {        # bpm above resting-HR baseline at "moderado"
    "cardio":   50,           # swim, run, bike
    "sport":    45,           # football, padel… (intervals, averaged)
    "strength": 30,           # gym (sets + rests averaged)
    "walk":     15,           # beach walk, light hike
    "other":    25,
}
INTENSITY_SCALE = {"leve": 0.7, "moderado": 1.0, "intenso": 1.3}
SYNTHETIC_INTENSITY = 80      # above AMBIENT_GATE_INTENSITY; ensures exertion label

# -- @4 segment labels ---------------------------------------------------------
SEG_MIN_DRAIN         = 3.0   # pts: minimum awake-span drain to surface a label
SEG_EXERTION_I        = 55    # RAW_INTENSITY threshold for "exertion" tag
SEG_EXERTION_HR_ABOVE = 78    # bpm above rhr_bl for "exertion" (≈ Zone 4+)
SEG_ACTIVE_I          = 25    # intensity threshold for "active" tag
SEG_ACTIVE_HR_ABOVE   = 28    # bpm above rhr_bl for "active" (≈ Zone 2+)
SEG_WASO_GAP          = 10    # awake gaps shorter than this (min) skipped inside sleep


def minute_activity_score(intens, hr, rhr_bl):
    """0 = baseline, 1 = active, 2 = exertion — for ONE minute.

    Single definition of the thresholds, shared by `_active_subspans` (which
    groups minutes into spans) and `ingest.build_minute_labels` (which colours
    each minute on the chart), so the two can never disagree.
    """
    if hr is not None:
        # HR decides exertion whenever it exists: movement is not effort.
        hr_above = hr - rhr_bl
        if hr_above > SEG_EXERTION_HR_ABOVE:
            return 2
        if intens > SEG_ACTIVE_I or hr_above > SEG_ACTIVE_HR_ABOVE:
            return 1
        return 0
    # No HR reading this minute (strap off//sentinel, or synthetic-activity gaps):
    # movement intensity is the only signal left, so it stands in for effort.
    if intens > SEG_EXERTION_I:
        return 2
    if intens > SEG_ACTIVE_I:
        return 1
    return 0


# -- Helpers -------------------------------------------------------------------

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

# ── Per-date device timezone ──────────────────────────────────────────────────
# A day starts at the midnight the *device* was on, not at Lisbon midnight.
# ingest registers the offsets (from sleep_score.tz_offset_min) before computing;
# with none registered every helper falls back to the home zone.

_TZ_OFFSETS = {}     # datetime.date -> UTC offset in minutes
_TZ_CACHE   = {}

def set_tz_offsets(mapping):
    _TZ_OFFSETS.clear()
    _TZ_OFFSETS.update(mapping)
    _TZ_CACHE.clear()

def day_tz(date):
    """Timezone in effect on `date`, carrying the last known offset forward."""
    if not _TZ_OFFSETS:
        return TZ
    if date in _TZ_CACHE:
        return _TZ_CACHE[date]
    off = _TZ_OFFSETS.get(date)
    if off is None:
        # A night missed abroad must not snap the day back to the home zone.
        prior = [d for d in _TZ_OFFSETS if d <= date]
        off = _TZ_OFFSETS[max(prior)] if prior else None
    tz = TZ if off is None else datetime.timezone(datetime.timedelta(minutes=off))
    _TZ_CACHE[date] = tz
    return tz

def day_bounds_s(date):
    t0 = int(midnight_dt(date).timestamp())
    return t0, t0 + 86400

def minute_index(ts_s, midnight_s):
    return (ts_s - midnight_s) // 60

def midnight_dt(date):
    return datetime.datetime(date.year, date.month, date.day, tzinfo=day_tz(date))


# -- @3 modifiers --------------------------------------------------------------

def load_events():
    """Load all logged events as list of (aware_datetime, type_str, extra_dict)."""
    try:
        with open(config.EVENTS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    out = []
    for entry in raw:
        dt = datetime.datetime.strptime(entry["datetime"], "%Y-%m-%d %H:%M")
        # The stored string is bare wall clock, so resolve it against the zone the
        # device was on that day — otherwise anything logged abroad lands an hour
        # or more from when it actually happened.
        dt = dt.replace(tzinfo=day_tz(dt.date()))
        extra = {k: v for k, v in entry.items() if k not in ("datetime", "type")}
        out.append((dt, entry["type"], extra))
    return out


def _coffee_suppress(ev_min, wake_min):
    """Fraction of drain suppressed by a coffee, based on minutes since wake."""
    mins_since_wake = ev_min - wake_min
    if mins_since_wake <= COFFEE_FULL_WIN:
        return COFFEE_SUPPRESS_MAX
    if mins_since_wake >= COFFEE_DECAY_WIN:
        return COFFEE_SUPPRESS_MIN
    t = (mins_since_wake - COFFEE_FULL_WIN) / (COFFEE_DECAY_WIN - COFFEE_FULL_WIN)
    return COFFEE_SUPPRESS_MAX - t * (COFFEE_SUPPRESS_MAX - COFFEE_SUPPRESS_MIN)


def _alcohol_units(ev_extra):
    """Total standard-drink units for a logged alcohol event (amount x units/drink).

    units_per_drink, when present, is the real figure computed from the drink's
    ABV at log time (food logging) and wins over the kind->units lookup, which
    only covers the three seeded pours.
    """
    amount = ev_extra.get("amount") or 1
    upd    = ev_extra.get("units_per_drink")
    if upd is not None:
        try:
            return amount * float(upd)
        except (TypeError, ValueError):
            pass
    per_drink = ALCOHOL_UNITS_PER_DRINK.get(ev_extra.get("kind"), ALCOHOL_DEFAULT_UNIT)
    return amount * per_drink


def _alcohol_drain_factor(units):
    return min(1.0 + ALCOHOL_DRAIN_PER_UNIT * units, ALCOHOL_DRAIN_FACTOR_MAX)


def build_drain_scale(date, events, wake_min):
    """
    Per-minute drain multiplier array for one day.
    Coffee: multiplies drain by (1 - suppress) for COFFEE_DURATION minutes;
    overlapping coffees compound (each masks fatigue independently).
    Alcohol: the standard-drink units of every drink active in a minute are
    SUMMED, then one _alcohol_drain_factor is applied. Overlapping drinks raise
    the same physiological load — they must not multiply drain N-fold (six
    2-unit glasses would hit factor^6 ~ 20x instead of the intended 2.2x cap).
    This matches how alcohol_presleep_units already sums units for the score.
    """
    scale = [1.0] * 1440
    mid = midnight_dt(date)
    alc_units = [0.0] * 1440
    for ev_dt, ev_type, ev_extra in events:
        ev_min = int((ev_dt - mid).total_seconds() / 60)
        if not (0 <= ev_min < 1440):
            continue
        if ev_type == "coffee":
            suppress = _coffee_suppress(ev_min, wake_min)
            for m in range(ev_min, min(1440, ev_min + COFFEE_DURATION)):
                scale[m] *= (1.0 - suppress)
        elif ev_type == "alcohol":
            u = _alcohol_units(ev_extra)
            for m in range(ev_min, min(1440, ev_min + ALCOHOL_DURATION)):
                alc_units[m] += u
    for m in range(1440):
        if alc_units[m]:
            scale[m] *= _alcohol_drain_factor(alc_units[m])
    return scale


def build_workout_mask(date, events):
    """Per-minute bool: True during a logged workout window (ambient gate disabled)."""
    mask = [False] * 1440
    mid = midnight_dt(date)
    for ev_dt, ev_type, ev_extra in events:
        if ev_type != "workout":
            continue
        ev_min   = int((ev_dt - mid).total_seconds() / 60)
        duration = ev_extra.get("duration_min", 60)
        for m in range(max(0, ev_min), min(1440, ev_min + duration)):
            mask[m] = True
    return mask


def alcohol_presleep_units(events, bedtime, waketime):
    """
    Total standard-drink units logged within ALCOHOL_PRESLEEP_WIN minutes before
    bedtime or during sleep. Used to penalise that night's SLEEP SCORE
    (see sleep_score.score_alcohol_penalty).
    """
    window_start = bedtime - datetime.timedelta(minutes=ALCOHOL_PRESLEEP_WIN)
    units = 0.0
    for ev_dt, ev_type, ev_extra in events:
        if ev_type == "alcohol" and window_start <= ev_dt <= waketime:
            units += _alcohol_units(ev_extra)
    return units


# -- @1 recharge ---------------------------------------------------------------

def score_to_seed(sleep_score):
    """Sleep score (0-100) -> desired level at wake (SEED_MIN-SEED_MAX)."""
    return clamp(SEED_MIN + (SEED_MAX - SEED_MIN) / 100.0 * sleep_score, SEED_MIN, SEED_MAX)


def build_stage_map(segs, base, target_date):
    """Per-minute stage code list for target_date; None = no sensor record."""
    offset = int((base - midnight_dt(target_date)).total_seconds() / 60)
    stage_map = [None] * 1440
    for s, e, code in segs:
        for m in range(max(0, s + offset), min(1440, e + offset)):
            stage_map[m] = code
    return stage_map


def compute_recharge_rates(stage_map, level_at_bedtime, morning_target):
    """
    Per-minute recharge rates summing to (morning_target - level_at_bedtime),
    weighted by stage: deep > REM > light > 0 (awake).
    """
    total_recharge = max(0.0, morning_target - level_at_bedtime)
    weight_sum = sum(
        STAGE_WEIGHTS.get(stage_map[m], 0)
        for m in range(1440)
        if stage_map[m] is not None and stage_map[m] != AWAKE_CODE
    )
    rates = [0.0] * 1440
    if weight_sum > 0 and total_recharge > 0:
        base_rate = total_recharge / weight_sum
        for m in range(1440):
            code = stage_map[m]
            if code is not None and code != AWAKE_CODE:
                rates[m] = base_rate * STAGE_WEIGHTS.get(code, 0)
    return rates


# -- Naps --------------------------------------------------------------------

def detect_naps(con, date, hypno_wake_min, latest_end_min):
    """Strap-detected daytime sleep in the awake window of `date`.

    Returns a list of (start_min, end_min) inclusive, in `date`'s local-minute
    grid. A block is minutes of RAW_KIND=NAP_KIND with no steps, gaps up to
    NAP_MAX_GAP_MIN bridged, at least NAP_MIN_BLOCK_MIN long, starting at least
    NAP_MIN_START_LAG_MIN after the recorded wake (everything earlier belongs to
    ingest.extend_sleep_from_activity) and ending before latest_end_min. Each
    block's counted length is capped at NAP_MAX_MINUTES.
    """
    t0, t1 = day_bounds_s(date)
    rows = con.execute(
        "SELECT TIMESTAMP, RAW_KIND, STEPS FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? ORDER BY TIMESTAMP",
        (t0, t1),
    ).fetchall()
    sleepy = {}
    for ts, kind, steps in rows:
        m = minute_index(ts, t0)
        if 0 <= m < 1440:
            sleepy[m] = (kind == NAP_KIND and not (steps or 0))

    floor_min = hypno_wake_min + NAP_MIN_START_LAG_MIN
    blocks, cur = [], None
    for m in range(1440):
        if sleepy.get(m):
            cur = [m, m] if cur is None else [cur[0], m]
        elif cur is not None:
            # bridge a short gap only if sleep resumes within it
            if any(sleepy.get(k) for k in range(m + 1, min(1440, m + NAP_MAX_GAP_MIN + 1))):
                continue
            blocks.append(tuple(cur))
            cur = None
    if cur is not None:
        blocks.append(tuple(cur))

    naps = []
    for s, e in blocks:
        if (e - s + 1) < NAP_MIN_BLOCK_MIN:
            continue
        if s < floor_min:
            continue
        if e >= latest_end_min:
            continue
        naps.append((s, min(e, s + NAP_MAX_MINUTES - 1)))
    return naps


def detect_ext_sleep(con, date, hypno_wake_min):
    """Return-to-sleep after the hypnogram ends, recovered from the raw activity
    stream -- see the EXT_* constants above and ingest.extend_sleep_from_activity,
    which computes the same blocks for sleep_score. Unlike a nap, this can be more
    than one block on a restless night (each return to sleep after another
    awakening), so every qualifying block is returned, not just the first.

    Returns a list of (start_min, end_min) inclusive, in `date`'s local-minute
    grid.
    """
    t0, t1 = day_bounds_s(date)
    rows = con.execute(
        "SELECT TIMESTAMP, RAW_KIND, STEPS FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? ORDER BY TIMESTAMP",
        (t0, t1),
    ).fetchall()
    sleepy = {}
    for ts, kind, steps in rows:
        m = minute_index(ts, t0)
        if 0 <= m < 1440:
            sleepy[m] = (kind == NAP_KIND and not (steps or 0))

    ceiling_min = min(1439, hypno_wake_min + EXT_LOOKAHEAD_MIN)
    blocks, cur = [], None
    for m in range(hypno_wake_min + 1, ceiling_min + 1):
        if sleepy.get(m):
            cur = [m, m] if cur is None else [cur[0], m]
        elif cur is not None:
            if any(sleepy.get(k) for k in range(m + 1, min(1440, m + EXT_MAX_GAP_MIN + 1))):
                continue
            blocks.append(tuple(cur))
            cur = None
    if cur is not None:
        blocks.append(tuple(cur))

    out = []
    for s, e in blocks:
        if (s - hypno_wake_min) > EXT_MAX_START_LAG_MIN:
            break          # too far after waking to belong to this night
        if (e - s + 1) < EXT_MIN_BLOCK_MIN:
            continue
        out.append((s, e))
    return out


def nap_stage_codes(all_sessions, date, s_min, e_min):
    """Real per-minute stage codes for a nap window, from any overlapping
    hypnogram. Empty dict when the strap wrote none -- the normal case, in which
    the nap's stages are reported as unknown rather than guessed."""
    codes = {}
    for segs, base in all_sessions:
        if abs((base.date() - date).days) > 1:
            continue
        smap = build_stage_map(segs, base, date)
        for m in range(s_min, e_min + 1):
            c = smap[m]
            if c is not None:
                codes[m] = c
    return codes


# -- Baselines -----------------------------------------------------------------

def resting_hr_baseline(con, date):
    cutoff_ms = int(midnight_dt(date).timestamp()) * 1000
    rows = con.execute(
        "SELECT HEART_RATE FROM HUAMI_HEART_RATE_RESTING_SAMPLE "
        "WHERE TIMESTAMP < ? AND HEART_RATE > 0 ORDER BY TIMESTAMP DESC LIMIT ?",
        (cutoff_ms, BASELINE_WINDOW),
    ).fetchall()
    vals = [r[0] for r in rows]
    return statistics.median(vals) if vals else 50


def stress_baseline(con, date):
    t1_ms = int(midnight_dt(date).timestamp()) * 1000
    t0_ms = t1_ms - BASELINE_WINDOW * 86400_000
    rows = con.execute(
        "SELECT STRESS FROM HUAMI_STRESS_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND STRESS IS NOT NULL",
        (t0_ms, t1_ms),
    ).fetchall()
    vals = [r[0] for r in rows]
    return statistics.median(vals) if vals else 33


# -- Signal loaders ------------------------------------------------------------

def load_activity(con, date):
    t0, t1 = day_bounds_s(date)
    rows = con.execute(
        "SELECT TIMESTAMP, RAW_INTENSITY, HEART_RATE, STEPS "
        "FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? ORDER BY TIMESTAMP",
        (t0, t1),
    ).fetchall()
    out = {}
    for ts, intensity, hr, steps in rows:
        m = minute_index(ts, t0)
        if 0 <= m < 1440:
            out[m] = (intensity or 0, hr if hr not in (0, 255) else None, steps or 0)
    return out


def inject_manual_activity(activity, date, events, rhr_bl):
    """
    For any logged workout with no real HR data, fill in synthetic HR by
    activity kind + intensity (see ACTIVITY_HR_OFFSET / INTENSITY_SCALE),
    with a 5-min ramp in/out. Minutes with real strap data are kept as-is.
    "hockey" maps to sport.
    """
    mid = midnight_dt(date)
    act = dict(activity)
    for ev_dt, ev_type, ev_extra in events:
        if ev_type != "workout":
            continue
        ev_min   = int((ev_dt - mid).total_seconds() / 60)
        duration = ev_extra.get("duration_min", 60)
        kind     = ev_extra.get("kind")
        kind     = "sport" if kind == "hockey" else kind
        offset   = ACTIVITY_HR_OFFSET.get(kind, ACTIVITY_HR_OFFSET["other"])
        scale    = INTENSITY_SCALE.get(ev_extra.get("intensity"), 1.0)
        for i in range(duration):
            m = ev_min + i
            if not (0 <= m < 1440):
                continue
            if act.get(m, (0, None, 0))[1] is not None:
                continue  # real strap data wins
            ramp = 0.6 if (i < 5 or i >= duration - 5) else 1.0
            act[m] = (SYNTHETIC_INTENSITY, rhr_bl + offset * scale * ramp, 0)
    return act


def load_stress(con, date):
    t0_ms = int(midnight_dt(date).timestamp()) * 1000
    t1_ms = t0_ms + 86400_000
    rows = con.execute(
        "SELECT TIMESTAMP, STRESS FROM HUAMI_STRESS_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND STRESS IS NOT NULL ORDER BY TIMESTAMP",
        (t0_ms, t1_ms),
    ).fetchall()
    events = {}
    for ts_ms, stress in rows:
        m = (ts_ms // 1000 - t0_ms // 1000) // 60
        if 0 <= m < 1440:
            events[m] = stress
    filled, current = {}, None
    for m in range(1440):
        if m in events:
            current = events[m]
        filled[m] = current
    return filled


# -- @2 drain ------------------------------------------------------------------

def compute_drain(intensity, hr, stress, rhr_bl, stress_bl, confirmed_workout=False,
                  top_slope=0.0):
    """top_slope: pts/min per bpm above the Zone-5 floor. 0.0 reproduces the
    original flat top zone exactly, which is what days before HR_ZONE_TOP_START
    are computed with — the gate lives in compute_day, not here."""
    hr_val   = hr if hr is not None else rhr_bl
    hr_above = max(0.0, hr_val - rhr_bl)

    # Zone-based drain from HR.
    if   hr_above < HR_ZONE_THRESHOLDS[0]: zone_drain = HR_ZONE_DRAINS[0]
    elif hr_above < HR_ZONE_THRESHOLDS[1]: zone_drain = HR_ZONE_DRAINS[1]
    elif hr_above < HR_ZONE_THRESHOLDS[2]: zone_drain = HR_ZONE_DRAINS[2]
    elif hr_above < HR_ZONE_THRESHOLDS[3]: zone_drain = HR_ZONE_DRAINS[3]
    elif hr_above < HR_ZONE_THRESHOLDS[4]: zone_drain = HR_ZONE_DRAINS[4]
    else:
        over       = min(hr_above, max(0.0, HR_MAX - rhr_bl)) - HR_ZONE_THRESHOLDS[4]
        zone_drain = HR_ZONE_DRAINS[5] + max(0.0, over) * top_slope

    # Ambient gate: low movement + no logged workout → dampen zone drain.
    # Catches HR elevated by alcohol, excitement, or social settings.
    if not confirmed_workout and (intensity or 0) < AMBIENT_GATE_INTENSITY:
        zone_drain *= AMBIENT_GATE_FACTOR

    s = stress if stress is not None else stress_bl
    stress_dev = min(1.0, max(0.0, (s - stress_bl) / STRESS_SPREAD))
    return BASE_DRAIN + zone_drain + stress_dev * STRESS_DRAIN_MAX


# -- Core engine ---------------------------------------------------------------

def run_engine(stage_map, recharge_rates, activity, stress_map, rhr_bl, stress_bl,
               start_level, drain_scale=None, workout_mask=None, top_slope=0.0,
               nap_recharge=None, ext_recharge=None):
    if drain_scale is None:
        drain_scale = [1.0] * 1440
    if workout_mask is None:
        workout_mask = [False] * 1440
    if nap_recharge is None:
        nap_recharge = [0.0] * 1440
    if ext_recharge is None:
        ext_recharge = [0.0] * 1440
    levels, drain_log = [], []
    level = start_level
    for t in range(1440):
        code     = stage_map[t]
        is_sleep = code is not None and code != AWAKE_CODE
        if is_sleep:
            level = clamp(level + recharge_rates[t], FLOOR, CAP)
            drain_log.append(0.0)
        elif nap_recharge[t] > 0.0:
            # A detected nap: recharges like sleep, never drains.
            level = clamp(level + nap_recharge[t], FLOOR, CAP)
            drain_log.append(0.0)
        elif ext_recharge[t] > 0.0:
            # Recovered return-to-sleep after the hypnogram ends: recharges
            # like sleep, never drains -- same treatment as a nap, unstaged.
            level = clamp(level + ext_recharge[t], FLOOR, CAP)
            drain_log.append(0.0)
        else:
            intens, hr, _ = activity.get(t, (0, None, 0))
            stress = stress_map.get(t)
            d = compute_drain(intens, hr, stress, rhr_bl, stress_bl,
                              confirmed_workout=workout_mask[t],
                              top_slope=top_slope) * drain_scale[t]
            level = clamp(level - d, FLOOR, CAP)
            drain_log.append(d)
        levels.append(level)
    return levels, drain_log


# -- Per-day compute -----------------------------------------------------------

def compute_day(con, date, sleep_score, start_level, all_sessions, events=None, next_score=None,
                recharge_factor=1.0, next_recharge_factor=1.0):
    """
    Run one full day. Returns a result dict or None if no session found.
    start_level = BioCharge at midnight of this date (carry-forward from prior day).
    events = list of (aware_datetime, type_str) from load_events().
    recharge_factor scales the morning wake target by overnight recovery quality
    (RHR vs baseline — computed by the caller, 1.0 = neutral). next_recharge_factor
    is the same for the next night's pre-midnight recharge overlay.
    """
    if events is None:
        events = []
    session_segs = session_base = bedtime = waketime = None
    for segs, base in all_sessions:
        wt = base + datetime.timedelta(minutes=segs[-1][1])
        if wt.date() == date:
            session_segs = segs
            session_base = base
            bedtime  = base + datetime.timedelta(minutes=segs[0][0])
            waketime = wt
            break

    if session_segs is None:
        print(f"  No sleep session waking on {date}")
        return None

    stage_map = build_stage_map(session_segs, session_base, date)
    is_sleep  = [code is not None and code != AWAKE_CODE for code in stage_map]

    rhr_bl    = resting_hr_baseline(con, date)
    stress_bl = stress_baseline(con, date)
    activity  = load_activity(con, date)
    activity  = inject_manual_activity(activity, date, events if events else [], rhr_bl)
    stress_mp = load_stress(con, date)

    # Phase 1: simulate drain from midnight to bedtime to find level when sleep starts.
    # If bedtime is before midnight (common — fell asleep the prior evening), bedtime_min=0
    # and level_at_bedtime = start_level unchanged.
    workout_mask = build_workout_mask(date, events if events else [])

    bedtime_min = max(0, int((bedtime - midnight_dt(date)).total_seconds() / 60))
    level_at_bedtime = start_level
    for t in range(min(bedtime_min, 1440)):
        if is_sleep[t]:
            break
        intens, hr, _ = activity.get(t, (0, None, 0))
        stress = stress_mp.get(t)
        d = compute_drain(intens, hr, stress, rhr_bl, stress_bl,
                          confirmed_workout=workout_mask[t])
        level_at_bedtime = clamp(level_at_bedtime - d, FLOOR, CAP)

    # recharge_factor scales the target by physiological recovery quality
    # (elevated RHR → factor < 1 → wake level below what duration alone implies).
    morning_target = clamp(score_to_seed(sleep_score) * recharge_factor, SEED_MIN, SEED_MAX)

    wake_min = int((waketime - midnight_dt(date)).total_seconds() / 60)
    drain_scale    = build_drain_scale(date, events, wake_min)
    recharge_rates = compute_recharge_rates(stage_map, level_at_bedtime, morning_target)

    # Overlay pre-midnight sleep from the next night's session. Done AFTER
    # recharge_rates so the primary session's recharge distribution is unaffected.
    # If next_score is provided, we also apply proper recharge to those minutes
    # (proportional to the full night's stage weights) after run_engine.
    next_date = date + datetime.timedelta(days=1)
    _pre_bed_n_min = _pre_segs_n = _pre_base_n = None
    for segs_n, base_n in all_sessions:
        bed_n = base_n + datetime.timedelta(minutes=segs_n[0][0])
        wt_n  = base_n + datetime.timedelta(minutes=segs_n[-1][1])
        if wt_n.date() == next_date and bed_n.date() == date:
            _pre_bed_n_min = int((bed_n - midnight_dt(date)).total_seconds() / 60)
            _pre_segs_n, _pre_base_n = segs_n, base_n
            next_map = build_stage_map(segs_n, base_n, date)
            for t in range(1440):
                if next_map[t] is not None:
                    stage_map[t] = next_map[t]
            break

    is_sleep = [code is not None and code != AWAKE_CODE for code in stage_map]

    # Extend-sleep credit (see EXT_* constants): unstaged, so kept out of stage_map.
    ext_minutes  = set()
    ext_recharge = [0.0] * 1440
    ext_out      = []
    if str(date) >= EXT_CREDIT_START:
        for s_min, e_min in detect_ext_sleep(con, date, wake_min):
            for m in range(s_min, e_min + 1):
                ext_recharge[m] = NAP_RECHARGE_RATE
                ext_minutes.add(m)
            ext_out.append(dict(
                start_min=s_min, end_min=e_min, minutes=e_min - s_min + 1,
                recharge=round((e_min - s_min + 1) * NAP_RECHARGE_RATE, 2)))

    # Naps (see NAP_* constants): unstaged, so kept out of stage_map with their
    # own recharge track and why_label.
    nap_minutes  = set()
    nap_recharge = [0.0] * 1440
    naps_out     = []
    if str(date) >= NAP_DETECT_START:
        nap_ceiling = NAP_LATEST_END_MIN
        for segs_n, base_n in all_sessions:
            if (base_n + datetime.timedelta(minutes=segs_n[-1][1])).date() != next_date:
                continue
            bn = int((base_n + datetime.timedelta(minutes=segs_n[0][0])
                      - midnight_dt(date)).total_seconds() / 60)
            if 0 < bn < nap_ceiling:
                nap_ceiling = bn
            break
        for s_min, e_min in detect_naps(con, date, wake_min, nap_ceiling):
            codes = nap_stage_codes(all_sessions, date, s_min, e_min)
            acc = 0.0
            for m in range(s_min, e_min + 1):
                if codes:
                    c = codes.get(m)
                    if c is None or c == AWAKE_CODE:
                        continue
                    rate = NAP_RECHARGE_RATE * STAGE_WEIGHTS.get(c, 1.0) / NAP_STAGE_REF_WEIGHT
                else:
                    rate = NAP_RECHARGE_RATE
                if acc + rate > NAP_MAX_RECHARGE:
                    rate = max(0.0, NAP_MAX_RECHARGE - acc)
                if rate <= 0.0:
                    continue
                nap_recharge[m] = rate
                nap_minutes.add(m)
                acc += rate
            naps_out.append(dict(
                start_min=s_min, end_min=e_min, minutes=e_min - s_min + 1,
                recharge=round(acc, 2),
                stage_source="hypnogram" if codes else "unknown"))

    # Forward-only via HR_ZONE_TOP_START.
    top_slope = HR_ZONE_TOP_SLOPE if str(date) >= HR_ZONE_TOP_START else 0.0

    levels, drain_log = run_engine(
        stage_map, recharge_rates, activity, stress_mp, rhr_bl, stress_bl,
        start_level, drain_scale, workout_mask, top_slope=top_slope,
        nap_recharge=nap_recharge, ext_recharge=ext_recharge
    )

    # Post-process: apply recharge to pre-midnight sleep minutes.
    # Recharge is distributed proportionally across the FULL night (pre + post midnight)
    # so the rate is consistent with what the next day's (D+1) engine will pick up from.
    if _pre_bed_n_min is not None and next_score is not None:
        def _sw(code):
            return STAGE_WEIGHTS.get(code, 0.0) if (code is not None and code != AWAKE_CODE) else 0.0
        next_map_d1 = build_stage_map(_pre_segs_n, _pre_base_n, next_date)
        pre_weight  = sum(_sw(stage_map[t]) for t in range(_pre_bed_n_min, 1440))
        post_weight = sum(_sw(next_map_d1[t]) for t in range(1440))
        total_weight = pre_weight + post_weight
        if total_weight > 0:
            next_target = clamp(score_to_seed(next_score) * next_recharge_factor,
                                SEED_MIN, SEED_MAX)
            total_recharge = max(0.0, next_target - levels[_pre_bed_n_min])
            base_rate = total_recharge / total_weight
            lv = levels[_pre_bed_n_min]
            for t in range(_pre_bed_n_min, 1440):
                code = stage_map[t]
                if code is not None and code != AWAKE_CODE:
                    lv = clamp(lv + base_rate * _sw(code), FLOOR, CAP)
                    levels[t] = lv
                else:
                    lv = levels[t]  # carry drain/flat from run_engine during WASO

    return dict(
        date=date, levels=levels, drain_log=drain_log,
        stage_map=stage_map, is_sleep=is_sleep,
        bedtime=bedtime, waketime=waketime,
        morning_target=morning_target, level_at_bedtime=level_at_bedtime,
        rhr_bl=rhr_bl, stress_bl=stress_bl,
        sleep_score=sleep_score, start_level=start_level,
        activity=activity, events=events, drain_scale=drain_scale,
        workout_mask=workout_mask, alcohol_penalty=0.0,
        nap_minutes=nap_minutes, naps=naps_out,
        ext_minutes=ext_minutes, ext_sleep=ext_out,
    )


# -- @4 segment labels ---------------------------------------------------------

def _active_subspans(start, end, activity, drain_log, rhr_bl, merge_gap=5):
    """
    Within an awake run [start, end), find contiguous high-activity sub-spans.
    Returns list of (span_start, span_end, tag, drain).
    """
    # Score each minute: 0=baseline, 1=active, 2=exertion
    scores = [minute_activity_score(*activity.get(m, (0, None, 0))[:2], rhr_bl)
              for m in range(start, end)]

    # Find contiguous active blocks, merging gaps <= merge_gap minutes
    spans = []
    i = 0
    n = len(scores)
    while i < n:
        if scores[i] >= 1:
            j = i + 1
            while j < n:
                if scores[j] >= 1:
                    j += 1
                elif j + merge_gap <= n and any(s >= 1 for s in scores[j:j + merge_gap]):
                    j += 1   # bridge small gap
                else:
                    break
            spans.append((start + i, start + j))
            i = j
        else:
            i += 1

    # Filter by duration and drain, classify
    result = []
    for s, e in spans:
        if (e - s) < 10:
            continue
        drain = sum(drain_log[s:e])
        if drain < SEG_MIN_DRAIN:
            continue
        peak = max(scores[s - start:e - start])
        tag  = "exertion" if peak == 2 else "active"
        result.append((s, e, tag, drain))
    return result




# -- @5 batch-recompute helpers ------------------------------------------------



def find_compute_dates(all_sessions, today):
    """All wake dates with sleep sessions, plus today, sorted chronologically."""
    dates = set()
    for segs, base in all_sessions:
        wt = base + datetime.timedelta(minutes=segs[-1][1])
        dates.add(wt.date())
    dates.add(today)
    return sorted(dates)
