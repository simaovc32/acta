"""fitness — estimated VO2max + percentile, from resting/max HR, body size, load.

Acta cannot MEASURE VO2max: the Helio strap has no GPS and no on-device workout
start, so a maximal test is structurally impossible. This estimates it two
independent, externally-published, lab-CPET-validated ways and reconciles them:

  A  Uth heart-rate ratio      15.3 * HRmax / RHR            passive, stable,
     (Uth & Sorensen 2004)                                   no activity term
  B  Nes / HUNT non-exercise   age, RHR, activity, waist     activity-aware
     (Nes et al. 2011, HUNT, 4637 treadmill tests)           (NTNU calculator)

The spread between A and B is kept and shown. A reads cardiovascular capacity;
B reads recent training load. For an ex-athlete under a light training block
they diverge, and the gap is the information ("your heart says X, your training
says Y"), not an error to hide.

Nothing is fitted in-house — no lab-VO2max ground truth exists to fit to, and
fitting to anything else is the mistake the sleep-weight tuner made twice.

Leaf metric: feeds nothing (not biocharge, not readiness, not the sleep score).
Display only, on the biocharge tab. Percentile is FRIEND-registry (Kaminsky
2015). No "fitness age" label — it floors for a fit 20-year-old and swings more
with the formula than the person (decided 2026-09-04).

Incremental + idempotent, same shape as calories.update() / night_physio.update():
fills any day from the first scored night to today not already in
fitness_estimate, and refreshes the tail. Wired non-fatal into ingest.py.

  python -m acta.engine.fitness            # update (backfills on first run)
  python -m acta.engine.fitness --show     # recent estimates
  python -m acta.engine.fitness --today    # full breakdown for today (incl. diagnostics)
"""
import argparse
import datetime
import json
import sqlite3  # noqa: E402
import statistics

from acta import config
from acta.tracking import body  # noqa: E402

TZ = config.TZ
SCHEMA = """
CREATE TABLE IF NOT EXISTS fitness_estimate (
  date            TEXT PRIMARY KEY,   -- YYYY-MM-DD
  vo2max_uth      REAL,               -- estimator A
  vo2max_nes      REAL,               -- estimator B
  vo2max_central  REAL,               -- reconciled point estimate
  vo2max_lo       REAL,               -- band low  = min(A, B)
  vo2max_hi       REAL,               -- band high = max(A, B)
  percentile      REAL,               -- FRIEND, for age + sex
  pa_index        REAL,               -- derived HUNT activity index (0..45)
  pa_read_as      TEXT,               -- human string ("~3 sessions/week")
  hrmax           INTEGER,            -- input snapshot (provenance)
  rhr_med         REAL,
  waist_cm        REAL,               -- value used (measured or estimated)
  waist_estimated INTEGER,            -- 1 = height*0.5 fallback, no real measure
  hrr_1min        REAL,               -- 1-min heart-rate recovery, bpm (companion metric)
  hrr_2min        REAL,               -- cumulative drop by 2 min
  hrr_n           INTEGER,            -- sessions the HRR median is built from
  confidence      TEXT,               -- high | moderate | low | stale
  why_spread      TEXT,               -- populated only when A and B diverge
  computed_at     TEXT
)
"""

REFRESH_TAIL_DAYS = 3   # RHR median + PA window both keep moving for a few days

# ── Heart-rate recovery (HRR) — a companion metric, NOT a VO2max estimator ─────
# 1-min HRR = how many bpm your HR drops in the 60s after a hard effort stops.
# Measures parasympathetic (vagal) reactivation, a distinct dimension from
# aerobic capacity and an independent mortality predictor (Cole 1999).
#
# NOT fed into the VO2max range. The one published field equation (StepTest4all,
# 22 + 0.3*HRR + 12) is calibrated on a sub-maximal STEP TEST; run against
# real football/running sessions it returns ~44 (≈20 pts low), because
# HRR after a maximal field effort is not comparable to step-test HRR. Observed
# over 5 sessions 2026-09-04 and demoted to a standalone readout.
HRR_WINDOW_SESSIONS = 6
HRR_KINDS = ("run", "cardio", "football", "sport")
HRR_MIN_REF_HR = 130      # the effort must have been real
HRR_MIN, HRR_MAX = 10, 70 # outside this the work-end boundary was wrong (still going / stopped early)


def _hr_at_minute(gb, day_iso: str, minute: int):
    d = datetime.date.fromisoformat(day_iso)
    mid = int(datetime.datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())
    t0 = mid + minute * 60
    r = gb.execute(
        "SELECT HEART_RATE FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND HEART_RATE BETWEEN 1 AND 250 "
        "ORDER BY ABS(TIMESTAMP - ?) LIMIT 1", (t0 - 75, t0 + 75, t0)).fetchone()
    return r[0] if r else None


def _hr_window_max(gb, day_iso: str, m0: int, m1: int):
    d = datetime.date.fromisoformat(day_iso)
    mid = int(datetime.datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())
    r = gb.execute(
        "SELECT MAX(HEART_RATE) FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND HEART_RATE BETWEEN 1 AND 250",
        (mid + m0 * 60, mid + m1 * 60)).fetchone()
    return r[0] if r else None


def _hrr(con, gb, date_iso: str) -> tuple[float | None, float | None, int]:
    """(hrr_1min, hrr_2min, n) — median cumulative HR drop at 1 and 2 minutes
    after effort-end, over the last HRR_WINDOW_SESSIONS confirmed cardio sessions.

    1 min ≈ the parasympathetic (vagal) rebound — the clean autonomic marker.
    2 min still parasympathetic-dominated, adds a little. 3-5 min ('metabolic
    recovery') is deliberately NOT measured: from minute-resolution strap HR with
    uncontrolled cool-downs (walk vs sit, heat, food) it is more noise than signal.
    """
    kinds = ",".join("?" * len(HRR_KINDS))
    rows = con.execute(
        f"SELECT date, start_min, duration_min, work_min FROM pai_detection "
        f"WHERE status = 'confirmed' AND lower(COALESCE(kind,'')) IN ({kinds}) "
        f"AND date <= ? ORDER BY date DESC LIMIT ?",
        (*HRR_KINDS, date_iso, HRR_WINDOW_SESSIONS * 2),
    ).fetchall()
    v1, v2 = [], []
    for day, start, dur, work in rows:
        end = start + (work or dur or 0)
        ref = _hr_window_max(gb, day, end - 2, end)
        r1 = _hr_at_minute(gb, day, end + 1)
        if not (ref and r1 and ref >= HRR_MIN_REF_HR):
            continue
        d1 = ref - r1
        if not (HRR_MIN <= d1 <= HRR_MAX):
            continue        # the work-end boundary was wrong (still going / stopped early)
        v1.append(d1)
        r2 = _hr_at_minute(gb, day, end + 2)
        if r2 and r2 <= r1 + 5:          # HR should still be falling (allow minor noise)
            v2.append(min(HRR_MAX + 15, ref - r2))
        if len(v1) >= HRR_WINDOW_SESSIONS:
            break
    if not v1:
        return None, None, 0
    m2 = round(statistics.median(v2), 1) if v2 else None
    return round(statistics.median(v1), 1), m2, len(v1)


def hrr_rating(hrr: float | None) -> str | None:
    """Field-measured 1-min HRR after a hard effort. Not the gentle-cooldown
    Cole protocol — bands set for max-ish field sessions with light recovery."""
    if hrr is None:
        return None
    if hrr < 15:
        return "sluggish"
    if hrr < 25:
        return "okay"
    if hrr < 36:
        return "good"
    return "athletic"

# ── HRmax ─────────────────────────────────────────────────────────────────────
HRMAX_MIN_VALID = 150       # samples below this are rest-day noise, not a real max
HRMAX_WINDOW_DAYS = 730     # trailing 24 months — lets it drift down ~0.7 bpm/yr
HRMAX_FLOOR_ABS = 160       # nobody's true max is below this; guards a bad table


def _age_predicted_hrmax(age: float) -> float:
    """HUNT (Nes 2013): 211 - 0.64*age. Tanaka 208 - 0.7*age is within ~2 bpm."""
    return 211.0 - 0.64 * age


def _hrmax(gb, date_iso: str, age: float) -> tuple[int, bool]:
    """(hrmax, measured?) — trailing-24mo ceiling, clamped to the age-pred range.

    HUAMI_HEART_RATE_MAX_SAMPLE logs a "max" every day, rest days included, so it
    holds junk (73, 133 ...). Take the max of readings >= 150 in the window; clamp
    to age_pred +/- a tolerance so one bad reading can't run away with the day.
    """
    end = datetime.datetime.fromisoformat(date_iso + "T23:59:59").timestamp() * 1000
    start = end - HRMAX_WINDOW_DAYS * 86400 * 1000
    row = gb.execute(
        "SELECT MAX(HEART_RATE) FROM HUAMI_HEART_RATE_MAX_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND HEART_RATE >= ?",
        (start, end, HRMAX_MIN_VALID),
    ).fetchone()
    pred = _age_predicted_hrmax(age)
    lo, hi = max(HRMAX_FLOOR_ABS, pred - 10), pred + 12
    if row and row[0]:
        return int(round(min(hi, max(lo, row[0])))), True
    return int(round(pred)), False


# ── RHR ───────────────────────────────────────────────────────────────────────
RHR_WINDOW_NIGHTS = 28
RHR_MIN, RHR_MAX = 30, 90


def _rhr_median(con, date_iso: str) -> float | None:
    """Median resting_hr over the last 28 nights up to `date_iso`, excluding
    timezone-transition nights (a 23h/25h day skews everything) and out-of-range
    sentinels. Median is already robust to the odd illness spike (one 61 in 28)."""
    rows = con.execute(
        "SELECT p.resting_hr FROM night_physio p "
        "LEFT JOIN sleep_score s ON s.night_of = p.night_of "
        "WHERE p.night_of <= ? AND p.resting_hr IS NOT NULL "
        "  AND p.resting_hr BETWEEN ? AND ? "
        "  AND COALESCE(s.tz_transition, 0) = 0 "
        "ORDER BY p.night_of DESC LIMIT ?",
        (date_iso, RHR_MIN, RHR_MAX, RHR_WINDOW_NIGHTS),
    ).fetchall()
    vals = [r[0] for r in rows]
    return statistics.median(vals) if len(vals) >= 7 else None


def _rhr_age_days(con, date_iso: str) -> int | None:
    """How stale the newest usable RHR night is, in days before `date_iso`."""
    row = con.execute(
        "SELECT MAX(night_of) FROM night_physio "
        "WHERE night_of <= ? AND resting_hr IS NOT NULL", (date_iso,),
    ).fetchone()
    if not row or not row[0]:
        return None
    d0 = datetime.date.fromisoformat(row[0])
    return (datetime.date.fromisoformat(date_iso) - d0).days


# ── Activity (PA) index ───────────────────────────────────────────────────────
# HUNT leisure-time activity index (0..45), derived from sensor data rather than
# a self-report question. Vigorous-equivalent weekly minutes = moderate + 2*high
# HR-zone minutes (Huami PAI), 4-week mean, plus a fraction of logged strength
# minutes and confirmed unlogged-session work minutes.
PA_WINDOW_WEEKS = 4
STRENGTH_VIGEQ_FRAC = 0.5   # a strength minute keeps HR only moderate

# Piecewise-linear vig-eq min/week -> HUNT PA index (0..45). Anchored to ACSM:
# ~75 min/wk vigorous = "meets guidelines" ~= PA 7.5; ~225 = "active" ~= 22.5.
# Linear (not a step function) so the estimate doesn't whipsaw as a 4-week sum
# crosses a band edge.
_PA_ANCHORS = [(0, 0.0), (75, 7.5), (225, 22.5), (400, 45.0)]


def _vigeq_to_pa(v: float) -> float:
    if v <= 0:
        return 0.0
    for (x0, y0), (x1, y1) in zip(_PA_ANCHORS, _PA_ANCHORS[1:]):
        if v <= x1:
            return y0 + (y1 - y0) * (v - x0) / (x1 - x0)
    return 45.0


def _pa_phrase(v: float) -> str:
    if v < 40:
        return "barely any"
    if v < 110:
        return "~2 sessions/week"
    if v < 220:
        return "~3-4 sessions/week"
    if v < 380:
        return "~5 hard sessions/week"
    return "training most days"


def _pa_index(con, gb, date_iso: str) -> tuple[float, str, float]:
    """(pa_index, read_as, vigeq_min_per_week)."""
    end_d = datetime.date.fromisoformat(date_iso)
    start_d = end_d - datetime.timedelta(weeks=PA_WINDOW_WEEKS)
    start_ms = int(datetime.datetime.combine(start_d, datetime.time()).timestamp() * 1000)
    end_ms = int(datetime.datetime.combine(
        end_d + datetime.timedelta(days=1), datetime.time()).timestamp() * 1000)

    # Huami PAI zone minutes, per day ceiling (the table is cumulative within a day)
    day_zone: dict[str, tuple[int, int]] = {}
    for ts, mod, hi in gb.execute(
        "SELECT TIMESTAMP, TIME_MODERATE, TIME_HIGH FROM HUAMI_PAI_SAMPLE "
        "WHERE TIMESTAMP >= ? AND TIMESTAMP < ?", (start_ms, end_ms),
    ):
        d = datetime.datetime.fromtimestamp(ts / 1000, TZ).date().isoformat()
        m, h = day_zone.get(d, (0, 0))
        day_zone[d] = (max(m, mod or 0), max(h, hi or 0))
    n_weeks = max(1.0, PA_WINDOW_WEEKS)
    vigeq = sum(m + 2 * h for m, h in day_zone.values()) / n_weeks

    # Logged strength minutes (Acta already computes these): lifting keeps HR only
    # moderate, so PAI zone-minutes under-count it. 4-week mean per week.
    row = con.execute(
        "SELECT AVG(strength_min) FROM daily_energy WHERE date >= ? AND date <= ?",
        (start_d.isoformat(), date_iso),
    ).fetchone()
    strength_wk = (row[0] or 0) * 7.0 * STRENGTH_VIGEQ_FRAC if row else 0.0

    total = vigeq + strength_wk

    # Confirmed unlogged sessions caught from HR are the "connection between the
    # detection queue and this algo" that was asked for. Their minutes are ALREADY
    # in the PAI zone data (a confirmed session generated those zone minutes), so
    # they are not added again — instead >=2 confirmed hard sessions in the
    # window put a floor under the estimate: you cannot read as sedentary.
    n_conf = con.execute(
        "SELECT COUNT(*) FROM pai_detection WHERE status = 'confirmed' "
        "AND date >= ? AND date <= ? AND COALESCE(work_min, duration_min) >= 20",
        (start_d.isoformat(), date_iso),
    ).fetchone()[0]
    if n_conf >= 2:
        total = max(total, 150.0)   # >= "meets guidelines"

    return _vigeq_to_pa(total), _pa_phrase(total), total


# ── the estimators ────────────────────────────────────────────────────────────

def uth(hrmax: float, rhr: float) -> float:
    """Uth & Sorensen 2004 heart-rate-ratio method. Validated on well-trained
    men (r~0.87 vs lab); best for the fit, weaker for the unfit."""
    return 15.3 * hrmax / rhr


def nes(age: float, sex: str, waist_cm: float, rhr: float, pa: float) -> float:
    """Nes et al. 2011 HUNT non-exercise model (waist form). R^2 0.61 (men) /
    0.56 (women), SEE ~5.7 ml/kg/min. This is the engine behind the NTNU
    fitness-age calculator."""
    if sex == "female":
        return 74.736 - 0.247 * age + 0.198 * pa - 0.259 * waist_cm - 0.114 * rhr
    return 100.27 - 0.296 * age + 0.226 * pa - 0.369 * waist_cm - 0.155 * rhr


def jackson_bmi(age: float, sex: str, bmi: float, pa_index: float) -> float:
    """Jackson et al. 1990 BMI non-exercise model — diagnostics only, not stored.
    Maps the HUNT PA index onto its 0-7 PA-R scale, weights activity hard."""
    par = min(7.0, pa_index / 6.4)          # 45 -> ~7, 7.5 -> ~1.2
    g = 1.0 if sex != "female" else 0.0
    return 56.363 + 1.921 * par - 0.381 * age - 0.754 * bmi + 10.987 * g


# ── FRIEND percentile ─────────────────────────────────────────────────────────
# FRIEND registry treadmill norms, MEN, ml/kg/min, by age decade (Kaminsky 2015
# / de Souza e Silva 2018). Values interpolate within a decade. Women's are
# ~8 ml/kg/min lower across the board — a rough shift: the demo person is male and
# the exact female table is not reproduced here.
FRIEND_M = {
    25: {10: 41.7, 25: 46.0, 50: 51.4, 75: 58.4, 90: 66.3, 95: 71.5},
    35: {10: 37.4, 25: 41.0, 50: 45.9, 75: 52.0, 90: 59.3, 95: 63.7},
    45: {10: 33.8, 25: 37.3, 50: 41.4, 75: 47.0, 90: 53.6, 95: 57.4},
    55: {10: 29.7, 25: 33.0, 50: 36.7, 75: 41.8, 90: 47.9, 95: 51.4},
    65: {10: 25.8, 25: 28.7, 50: 32.2, 75: 36.9, 90: 42.5, 95: 45.8},
    75: {10: 21.8, 25: 24.5, 50: 27.6, 75: 32.0, 90: 37.0, 95: 40.0},
}


def _percentile(vo2max: float, age: float, sex: str) -> float:
    decades = sorted(FRIEND_M)
    a = min(decades, key=lambda d: abs(d - age))
    table = dict(FRIEND_M[a])
    if sex == "female":
        table = {k: v - 8.0 for k, v in table.items()}
    pts = sorted(table.items(), key=lambda kv: kv[1])       # (pctile, vo2) by vo2
    if vo2max <= pts[0][1]:
        return float(pts[0][0])
    if vo2max >= pts[-1][1]:
        return float(pts[-1][0])
    for (p0, v0), (p1, v1) in zip(pts, pts[1:]):
        if v0 <= vo2max <= v1:
            return round(p0 + (p1 - p0) * (vo2max - v0) / (v1 - v0), 1)
    return 50.0


# ── reconcile ─────────────────────────────────────────────────────────────────
W_UTH, W_NES = 0.55, 0.45   # fixed for v1; PA-confidence weighting is future work


def _reconcile(a: float | None, b: float | None, rhr_age: int | None):
    if a is None and b is None:
        return None, None, None, "low", None
    if a is None or b is None:
        v = a if a is not None else b
        return round(v, 1), round(v, 1), round(v, 1), "low", None
    lo, hi = min(a, b), max(a, b)
    central = W_UTH * a + W_NES * b
    gap = hi - lo
    conf = "high" if gap <= 5 else "moderate" if gap <= 12 else "low"
    if rhr_age is not None and rhr_age > 5:
        conf = "stale"
    why = None
    if gap > 5:
        hr_v, tr_v = (a, b) if a >= b else (b, a)
        why = (f"resting HR points to ~{round(hr_v)}, "
               f"recent training load points to ~{round(tr_v)}")
    return round(central, 1), round(lo, 1), round(hi, 1), conf, why


# ── one day ───────────────────────────────────────────────────────────────────

def estimate(con, gb, date_iso: str) -> dict | None:
    cfg = body.get_config(con)
    if cfg is None:
        return None
    b = datetime.date.fromisoformat(cfg["birth_date"])
    d = datetime.date.fromisoformat(date_iso)
    age = (d - b).days / 365.25
    sex = cfg["sex"]

    rhr = _rhr_median(con, date_iso)
    if rhr is None:
        return None   # nothing to estimate from yet
    rhr_age = _rhr_age_days(con, date_iso)
    hrmax, hrmax_measured = _hrmax(gb, date_iso, age)
    pa_index, pa_read_as, vigeq = _pa_index(con, gb, date_iso)

    weight, _bf = body.weight_at(con, date_iso)
    height_cm = cfg["height_cm"]
    bmi = weight / (height_cm / 100.0) ** 2 if weight else None

    waist, waist_est = body.waist_at(con, date_iso), False
    if waist is None:
        waist, waist_est = height_cm * 0.50, True   # WHtR 0.5 neutral prior

    v_uth = uth(hrmax, rhr)
    v_nes = nes(age, sex, waist, rhr, pa_index) if waist else None
    v_jack = jackson_bmi(age, sex, bmi, pa_index) if bmi else None   # diagnostic

    central, lo, hi, conf, why = _reconcile(v_uth, v_nes, rhr_age)
    pct = _percentile(central, age, sex) if central is not None else None

    hrr1, hrr2, hrr_n = _hrr(con, gb, date_iso)   # companion metric, not in the range

    return {
        "date": date_iso,
        "vo2max_uth": round(v_uth, 1) if v_uth is not None else None,
        "vo2max_nes": round(v_nes, 1) if v_nes is not None else None,
        "vo2max_central": central,
        "vo2max_lo": lo, "vo2max_hi": hi,
        "percentile": pct,
        "pa_index": round(pa_index, 1),
        "pa_read_as": pa_read_as,
        "hrmax": hrmax,
        "rhr_med": round(rhr, 1),
        "waist_cm": round(waist, 1) if waist else None,
        "waist_estimated": 1 if waist_est else 0,
        "hrr_1min": hrr1,
        "hrr_2min": hrr2,
        "hrr_n": hrr_n,
        "confidence": conf,
        "why_spread": why,
        # not persisted, returned for --today / the API's expanded view
        "_diag": {"vo2max_jackson": round(v_jack, 1) if v_jack else None,
                  "vigeq_min_per_week": round(vigeq), "bmi": round(bmi, 1) if bmi else None,
                  "age": round(age, 1), "hrmax_measured": hrmax_measured,
                  "rhr_age_days": rhr_age},
    }


# ── persistence ───────────────────────────────────────────────────────────────

def ensure(con) -> None:
    con.execute(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(fitness_estimate)")}
    for col, decl in (("hrr_1min", "REAL"), ("hrr_2min", "REAL"), ("hrr_n", "INTEGER")):
        if col not in have:
            con.execute(f"ALTER TABLE fitness_estimate ADD COLUMN {col} {decl}")


_COLS = ["date", "vo2max_uth", "vo2max_nes", "vo2max_central", "vo2max_lo",
         "vo2max_hi", "percentile", "pa_index", "pa_read_as", "hrmax", "rhr_med",
         "waist_cm", "waist_estimated", "hrr_1min", "hrr_2min", "hrr_n",
         "confidence", "why_spread", "computed_at"]


def _upsert(con, r: dict) -> None:
    now = datetime.datetime.now(TZ).isoformat(timespec="seconds")
    vals = [r.get(c) for c in _COLS[:-1]] + [now]
    qs = ",".join("?" * len(_COLS))
    upd = ",".join(f"{c}=excluded.{c}" for c in _COLS if c != "date")
    con.execute(
        f"INSERT INTO fitness_estimate ({','.join(_COLS)}) VALUES ({qs}) "
        f"ON CONFLICT(date) DO UPDATE SET {upd}", vals)


def update(verbose: bool = False) -> int:
    """Fill missing days and refresh the tail. Never raises on a single bad day."""
    con = sqlite3.connect(config.ACTA_DB, timeout=30)
    gb = sqlite3.connect(f"file:{config.GADGETBRIDGE_DB}?mode=ro&immutable=1", uri=True)
    try:
        ensure(con)
        body.ensure(con)
        if body.get_config(con) is None:
            if verbose:
                print("  fitness: body_config empty — skipping")
            return 0
        row = con.execute("SELECT MIN(night_of) FROM sleep_score").fetchone()
        if not row or not row[0]:
            return 0
        first = datetime.date.fromisoformat(row[0])
        today = datetime.datetime.now(TZ).date()
        have = {r[0] for r in con.execute("SELECT date FROM fitness_estimate")}
        tail = {(today - datetime.timedelta(days=i)).isoformat()
                for i in range(REFRESH_TAIL_DAYS)}
        written = 0
        d = first
        while d <= today:
            iso = d.isoformat()
            if iso in have and iso not in tail:
                d += datetime.timedelta(days=1)
                continue
            try:
                r = estimate(con, gb, iso)
                if r is not None:
                    _upsert(con, r)
                    written += 1
            except Exception as e:                       # noqa: BLE001
                if verbose:
                    print(f"  fitness: {iso} skipped ({e})")
            d += datetime.timedelta(days=1)
        con.commit()
        if verbose:
            print(f"  fitness: {written} days written")
        return written
    finally:
        con.close()
        gb.close()


def recompute() -> int:
    """Clear and rebuild the whole fitness_estimate table. Used when an input that
    affects all of history changes — a waist measurement carries forward AND
    backward (like body.weight_at), so 3 tail days is not enough. Deterministic,
    ~1 s for ~130 days."""
    con = sqlite3.connect(config.ACTA_DB, timeout=30)
    try:
        ensure(con)
        con.execute("DELETE FROM fitness_estimate")
        con.commit()
    finally:
        con.close()
    return update()


def trend(days: int = 90) -> dict:
    """{series:[{date,central,lo,hi}], latest:{...}, delta_90d, ...} for the API."""
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        rows = con.execute(
            "SELECT * FROM fitness_estimate WHERE date >= ? ORDER BY date", (cutoff,),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return {"series": [], "latest": None, "delta_90d": None}
    series = [{"date": r["date"], "central": r["vo2max_central"],
              "lo": r["vo2max_lo"], "hi": r["vo2max_hi"]} for r in rows
             if r["vo2max_central"] is not None]
    latest = dict(rows[-1])
    delta = None
    if len(series) >= 2:
        delta = round(series[-1]["central"] - series[0]["central"], 1)
    return {"series": series, "latest": latest, "delta_90d": delta,
            "window_days": days}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--today", action="store_true")
    ap.add_argument("--date")
    args = ap.parse_args()

    if args.today or args.date:
        con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
        gb = sqlite3.connect(f"file:{config.GADGETBRIDGE_DB}?mode=ro&immutable=1", uri=True)
        d = args.date or datetime.datetime.now(TZ).date().isoformat()
        r = estimate(con, gb, d)
        print(json.dumps(r, indent=2))
        con.close()
        gb.close()
        return
    if args.show:
        con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
        for row in con.execute(
            "SELECT date, vo2max_lo, vo2max_central, vo2max_hi, percentile, "
            "confidence FROM fitness_estimate ORDER BY date DESC LIMIT 30"):
            print(f"  {row[0]}  {row[1]:.0f}-{row[3]:.0f}  central {row[2]:.1f}  "
                  f"p{row[4]:.0f}  [{row[5]}]")
        con.close()
        return
    n = update(verbose=True)
    print(f"fitness: {n} days written")


if __name__ == "__main__":
    _main()
