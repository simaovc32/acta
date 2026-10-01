"""
Daily energy expenditure estimate — per-minute, mode-segmented.

Why not one formula over the whole day
--------------------------------------
A daily total is overwhelmingly made of *low-intensity* minutes. Population
HR->EE equations (Keytel et al.) are anchored to absolute heart rate and fitted
on a cohort with higher RHR, so for a fit user the same absolute HR means a much
lower relative effort and those equations over-bill exactly that band. Biocharge
uses HR *reserve* for the same reason.

So each minute is classified and priced by mode instead:

    sleep      -> BMR multiple (sleep is definitionally near-basal)
    strength   -> fixed compendium MET (HR is a poor EE proxy under load)
    active     -> %HR-reserve -> VO2 -> kcal, using *measured* HRmax and the
                  rolling RHR baseline, so it self-adapts as fitness changes
    sedentary  -> BMR multiple + a step-rate increment (absolute, mass-scaled)

and the awake modes take max(sedentary, active) so there is no discontinuity at
the crossover.

Units
-----
Resting terms are anchored to the user's own BMR (so the floor *is* BMR).
Movement terms are absolute VO2, converted at 5 kcal per litre of O2, because
the cost of moving a body scales with its mass, not with its basal rate. The
personal resting VO2 is back-solved from BMR rather than assumed to be the
textbook 3.5 ml/kg/min, which keeps the two scales continuous at rest.

Accuracy
--------
Uncalibrated (k = 1.0) the absolute level is good to roughly +/-10-15%; relative
day-to-day comparison is considerably tighter. `k` in body_config is the single
calibration knob and stays 1.0 until there is measured weight + intake data to
fit it against. This estimate is deliberately NOT fed back into biocharge or
readiness — it is derived from the same HR stream, so that would double-count.

CLI:
    python -m acta.engine.calories                 # today so far
    python -m acta.engine.calories 2026-08-04      # one day, with breakdown
    python -m acta.engine.calories --range 14      # last N days, one line each
"""
import argparse
import collections
import datetime
import json
import sqlite3
import statistics

from acta import config
from acta.engine import biocharge as BC  # noqa: E402
from acta.engine import sleep_score as SS  # noqa: E402
from acta.tracking import body  # noqa: E402

BC.parse_hypnogram = SS.parse_hypnogram
BC.session_base_date = SS.session_base_date
TZ = SS.TZ

# ── model constants ───────────────────────────────────────────────────────────

KCAL_PER_L_O2 = 5.0        # kcal released per litre of O2 consumed (RQ ~0.85)
VO2_TEXTBOOK_MET = 3.5     # ml/kg/min, the compendium's 1-MET definition

SLEEP_BMR_MULT = 0.95      # whole night incl. REM/WASO; 0.85 is deep sleep only
SED_BMR_MULT = 1.25        # awake, still: basal + postural tone + thermic effect

# Walking cost above sitting, per step/min. ~100 spm (moderate walk) lands near
# +7.5 ml/kg/min, i.e. ~3.3 MET total — matches the compendium for level walking.
VO2_PER_STEP_MIN = 0.075
VO2_STEP_CAP = 25.0        # ml/kg/min; beyond this it isn't walking any more

MET_STRENGTH = 5.0         # compendium: resistance training, moderate-vigorous

# Below this fraction of HR reserve the HR signal is dominated by non-metabolic
# drivers (posture, heat, caffeine, stress) and the step/sedentary path is used.
# 0.40 is where the %HRR ~ %VO2-reserve relation is established as linear
# (Swain & Leutholtz 1997; ACSM) — below it the relation bends and HR over-reads.
# Not a free parameter: lowering it bills ordinary sitting HR as exercise and
# inflates the day far past a credible TDEE. Do not lower it to "capture more activity".
HRR_TRUST_MIN = 0.40

# Afterburn. Applied to the excess-above-rest accumulated in genuinely vigorous
# minutes only; a small, well-evidenced term, not a fudge factor.
EPOC_FRACTION = 0.08
EPOC_VO2_MIN = 25.0        # ml/kg/min, roughly 7 MET

# Fallback when a minute has no strap row at all.
IMPUTE_BMR_MULT_DEFAULT = 1.40
IMPUTE_BMR_MULT_RANGE = (1.15, 2.20)

HRMAX_MIN_PLAUSIBLE = 150  # sub-150 rows in HUAMI_HEART_RATE_MAX_SAMPLE are junk
VO2MAX_RANGE = (25.0, 85.0)


# ── helpers ───────────────────────────────────────────────────────────────────

def load_tz_offsets(acta: sqlite3.Connection) -> None:
    """Anchor biocharge's day grid to device-local midnight."""
    rows = acta.execute(
        "SELECT night_of, tz_offset_min FROM sleep_score "
        "WHERE tz_offset_min IS NOT NULL"
    ).fetchall()
    BC.set_tz_offsets({datetime.date.fromisoformat(n): off for n, off in rows})


def kcal_min_from_vo2(vo2_ml_kg_min: float, weight_kg: float) -> float:
    return vo2_ml_kg_min * weight_kg * KCAL_PER_L_O2 / 1000.0


def hr_max_estimate(gb: sqlite3.Connection, date, age: int) -> tuple[float, str]:
    """Highest credible measured max-HR in the trailing year, else age formula."""
    t1 = int(BC.midnight_dt(date).timestamp()) + 86400
    t0 = t1 - 365 * 86400
    rows = gb.execute(
        "SELECT TIMESTAMP, HEART_RATE FROM HUAMI_HEART_RATE_MAX_SAMPLE "
        "ORDER BY TIMESTAMP DESC"
    ).fetchall()
    vals = []
    for ts, hr in rows:
        ts = ts / 1000 if ts > 1e11 else ts       # table has mixed-unit history
        if t0 <= ts <= t1 and hr and hr >= HRMAX_MIN_PLAUSIBLE:
            vals.append(hr)
    if vals:
        return float(max(vals)), "measured"
    return 208.0 - 0.7 * age, "tanaka"            # Tanaka et al. 2001


def vo2max_estimate(hr_max: float, rhr: float, vo2_rest: float) -> float:
    """Uth-Sorensen-Overgaard: VO2max ~ 15.3 * HRmax/RHR. Clamped to plausible."""
    est = 15.3 * hr_max / max(rhr, 30.0)
    lo, hi = VO2MAX_RANGE
    return max(lo, min(hi, max(est, vo2_rest + 5.0)))


def sleep_minutes(acta: sqlite3.Connection, date) -> list[bool]:
    """Per-minute sleep flag, from every session overlapping this device-local day.

    Uses bedtime->waketime rather than the stage map: awake-in-bed differs from
    asleep by a couple of percent of BMR, far below this model's resolution, and
    the window form handles pre-midnight sleep without re-parsing hypnograms.
    """
    t0, t1 = BC.day_bounds_s(date)
    mask = [False] * 1440
    rows = acta.execute(
        "SELECT bedtime_ts, waketime_ts FROM sleep_score "
        "WHERE bedtime_ts IS NOT NULL AND waketime_ts IS NOT NULL "
        "AND waketime_ts >= ? AND bedtime_ts <= ?",
        (t0 * 1000, t1 * 1000),
    ).fetchall()
    for bed_ms, wake_ms in rows:
        s = max(0, int((bed_ms / 1000 - t0) // 60))
        e = min(1440, int((wake_ms / 1000 - t0) // 60) + 1)
        for m in range(s, e):
            mask[m] = True
    return mask


def workout_kinds(date, events) -> list[str | None]:
    """Per-minute logged-workout kind (None outside any logged window)."""
    out: list[str | None] = [None] * 1440
    mid = BC.midnight_dt(date)
    for ev_dt, ev_type, extra in events:
        if ev_type != "workout":
            continue
        start = int((ev_dt - mid).total_seconds() / 60)
        dur = extra.get("duration_min", 60)
        kind = extra.get("kind") or "other"
        for m in range(max(0, start), min(1440, start + dur)):
            out[m] = kind
    return out


# ── core ──────────────────────────────────────────────────────────────────────

def estimate_day(gb: sqlite3.Connection, acta: sqlite3.Connection, date,
                 *, now=None, events=None) -> dict:
    """Energy expenditure for one device-local day.

    For today, only minutes up to `now` are counted (so the result reads as
    "so far today", not a partially-empty full day).
    """
    date_iso = date.isoformat()
    bmr, used = body.bmr_for_date(acta, date_iso)
    weight = used["weight_kg"]
    k = used["k"]
    bmr_min = bmr / 1440.0

    # Personal resting VO2, back-solved from BMR instead of assuming 3.5.
    vo2_rest = bmr_min * 1000.0 / (KCAL_PER_L_O2 * weight)

    rhr_bl = float(BC.resting_hr_baseline(gb, date))
    hr_max, hr_max_src = hr_max_estimate(gb, date, used["age"])
    vo2max = vo2max_estimate(hr_max, rhr_bl, vo2_rest)
    hr_reserve = max(1.0, hr_max - rhr_bl)

    if events is None:
        events = BC.load_events()

    activity = BC.load_activity(gb, date)
    activity = BC.inject_manual_activity(activity, date, events, rhr_bl)
    sleep_mask = sleep_minutes(acta, date)
    wk_kind = workout_kinds(date, events)

    # Elapsed window: whole day in the past, up to `now` for today.
    t0, _ = BC.day_bounds_s(date)
    now = now or datetime.datetime.now(TZ)
    elapsed = 1440
    if now.timestamp() < t0 + 86400:
        elapsed = max(0, min(1440, int((now.timestamp() - t0) // 60) + 1))

    kcal = [0.0] * elapsed
    mode = [""] * elapsed
    hr_ok = 0
    epoc_excess = 0.0

    sed_still = SED_BMR_MULT * bmr_min
    strength_kcal_min = kcal_min_from_vo2(MET_STRENGTH * VO2_TEXTBOOK_MET, weight)

    # Pass 1 — everything with data.
    for m in range(elapsed):
        row = activity.get(m)
        intensity, hr, steps = row if row else (None, None, None)
        if hr is not None:
            hr_ok += 1

        if sleep_mask[m]:
            kcal[m] = SLEEP_BMR_MULT * bmr_min
            mode[m] = "sleep"
            continue

        if wk_kind[m] == "strength":
            kcal[m] = strength_kcal_min
            mode[m] = "strength"
            continue

        if row is None:
            mode[m] = "imputed"
            continue

        # Sedentary floor: still-awake cost plus a step-rate increment.
        vo2_steps = min(VO2_STEP_CAP, (steps or 0) * VO2_PER_STEP_MIN)
        sed = sed_still + kcal_min_from_vo2(vo2_steps, weight)

        # Active path — only when HR is credible AND movement is corroborated.
        act = None
        if hr is not None:
            moving = (intensity or 0) >= BC.AMBIENT_GATE_INTENSITY
            if moving or wk_kind[m] is not None:
                hrr = (hr - rhr_bl) / hr_reserve
                if hrr >= HRR_TRUST_MIN:
                    vo2 = hrr * (vo2max - vo2_rest) + vo2_rest
                    act = kcal_min_from_vo2(vo2, weight)
                    if vo2 >= EPOC_VO2_MIN:
                        epoc_excess += act - bmr_min

        if act is not None and act > sed:
            kcal[m], mode[m] = act, "active"
        else:
            kcal[m], mode[m] = sed, "sedentary"

    # Pass 2 — minutes with no strap row at all, priced from this day's own
    # observed awake average so a partial-coverage day isn't flattened to BMR.
    observed = [kcal[m] for m in range(elapsed) if mode[m] in ("sedentary", "active")]
    if observed:
        mult = statistics.median(observed) / bmr_min
    else:
        mult = IMPUTE_BMR_MULT_DEFAULT
    lo, hi = IMPUTE_BMR_MULT_RANGE
    impute_kcal = max(lo, min(hi, mult)) * bmr_min
    for m in range(elapsed):
        if mode[m] == "imputed":
            kcal[m] = impute_kcal

    total = sum(kcal) + EPOC_FRACTION * epoc_excess
    total *= k
    rest_floor = bmr_min * elapsed * k

    counts = {md: mode.count(md) for md in
              ("sleep", "strength", "active", "sedentary", "imputed")}

    # Cumulative kcal at the end of each elapsed hour. Feeds the intraday chart
    # and lets the API project a full day from a partial one without having to
    # recompute history per request.
    hourly, run = [], 0.0
    for m in range(elapsed):
        run += kcal[m]
        if m % 60 == 59 or m == elapsed - 1:
            hourly.append(round(run * k))

    return {
        "date": date_iso,
        # The per-minute arrays are already built above and were being thrown
        # away. activity_cost() needs them to price a single bout; callers that
        # only want day totals simply ignore them (nothing is persisted).
        "kcal_min": kcal,
        "mode_min": mode,
        "bmr_kcal_min": bmr_min,
        "k_factor": k,
        "total_kcal": round(total),
        "hourly": hourly,
        "rest_kcal": round(rest_floor),
        "active_kcal": round(total - rest_floor),
        "bmr_kcal_day": round(bmr),
        "elapsed_min": elapsed,
        "coverage_pct": round(hr_ok / elapsed * 100, 1) if elapsed else 0.0,
        "epoc_kcal": round(EPOC_FRACTION * epoc_excess * k),
        "minutes": counts,
        "inputs": {
            "weight_kg": weight, "age": used["age"], "k": k,
            "rhr_baseline": round(rhr_bl, 1),
            "hr_max": round(hr_max, 1), "hr_max_src": hr_max_src,
            "vo2max_est": round(vo2max, 1), "vo2_rest": round(vo2_rest, 2),
        },
    }


# ── persistence ───────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_energy (
  date          TEXT PRIMARY KEY,   -- device-local day, YYYY-MM-DD
  total_kcal    INTEGER,
  rest_kcal     INTEGER,            -- BMR floor over the elapsed window
  active_kcal   INTEGER,            -- total - rest
  bmr_kcal_day  INTEGER,
  elapsed_min   INTEGER,            -- < 1440 only for the current day
  coverage_pct  REAL,               -- share of elapsed minutes with valid HR
  epoc_kcal     INTEGER,
  sleep_min     INTEGER,
  sedentary_min INTEGER,
  active_min    INTEGER,
  strength_min  INTEGER,
  imputed_min   INTEGER,
  weight_kg     REAL,               -- weight in effect that day
  k             REAL,               -- calibration factor applied
  hourly_json   TEXT,               -- cumulative kcal at each elapsed hour
  computed_at   TEXT
)
"""

# Days at the tail always get recomputed: evening activity arrives with the
# overnight sync, so "yesterday" is still incomplete at the time it first runs.
# Same reasoning as replay_biocharge's yesterday rule.
REFRESH_TAIL_DAYS = 2


def ensure(con: sqlite3.Connection) -> None:
    con.execute(SCHEMA)


def _upsert(acta, r) -> None:
    m = r["minutes"]
    acta.execute(
        "INSERT INTO daily_energy VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET "
        "total_kcal=excluded.total_kcal, rest_kcal=excluded.rest_kcal, "
        "active_kcal=excluded.active_kcal, bmr_kcal_day=excluded.bmr_kcal_day, "
        "elapsed_min=excluded.elapsed_min, coverage_pct=excluded.coverage_pct, "
        "epoc_kcal=excluded.epoc_kcal, sleep_min=excluded.sleep_min, "
        "sedentary_min=excluded.sedentary_min, active_min=excluded.active_min, "
        "strength_min=excluded.strength_min, imputed_min=excluded.imputed_min, "
        "weight_kg=excluded.weight_kg, k=excluded.k, "
        "hourly_json=excluded.hourly_json, computed_at=excluded.computed_at",
        (r["date"], r["total_kcal"], r["rest_kcal"], r["active_kcal"],
         r["bmr_kcal_day"], r["elapsed_min"], r["coverage_pct"], r["epoc_kcal"],
         m["sleep"], m["sedentary"], m["active"], m["strength"], m["imputed"],
         r["inputs"]["weight_kg"], r["inputs"]["k"],
         json.dumps(r["hourly"]),
         datetime.datetime.now(TZ).isoformat(timespec="seconds")),
    )


def update(verbose: bool = False) -> int:
    """Fill missing days and refresh the tail. Returns days written.

    Never raises on a single bad day — a gap in Gadgetbridge history must not
    stop the rest of the range from computing.
    """
    acta = sqlite3.connect(config.ACTA_DB, timeout=30)
    acta.row_factory = sqlite3.Row
    gb = sqlite3.connect(f"file:{config.GADGETBRIDGE_DB}?mode=ro&immutable=1", uri=True)
    try:
        ensure(acta)
        body.ensure(acta)
        if body.get_config(acta) is None:
            if verbose:
                print("  calories: body_config empty — skipping")
            return 0
        load_tz_offsets(acta)

        row = acta.execute("SELECT MIN(night_of) FROM sleep_score").fetchone()
        if not row or not row[0]:
            return 0
        first = datetime.date.fromisoformat(row[0])
        today = datetime.datetime.now(TZ).date()

        have = {r["date"] for r in acta.execute("SELECT date FROM daily_energy")}
        tail = {(today - datetime.timedelta(days=i)).isoformat()
                for i in range(REFRESH_TAIL_DAYS)}

        events = BC.load_events()
        written = 0
        d = first
        while d <= today:
            iso = d.isoformat()
            if iso in have and iso not in tail:
                d += datetime.timedelta(days=1)
                continue
            try:
                _upsert(acta, estimate_day(gb, acta, d, events=events))
                written += 1
            except Exception as e:                      # noqa: BLE001
                if verbose:
                    print(f"  calories: {iso} skipped ({e})")
            d += datetime.timedelta(days=1)
        acta.commit()
        if verbose:
            print(f"  calories: {written} days written")
        return written
    finally:
        acta.close()
        gb.close()


def activity_cost(date, start_min: int, duration_min: int, *,
                  work_min: int = None, now=None, events=None) -> dict:
    """What one bout of activity actually cost, in kcal.

    Reported **marginal**, not gross: the resting floor for the same minutes is
    subtracted, because those calories would have been spent lying on the sofa.
    That is the number worth knowing; a fitness app usually quotes the gross.

    The day's whole minute-by-minute curve is recomputed and then sliced, rather
    than modelled separately, so a bout's number can never disagree with the day
    total it sits inside. Strength minutes use the fixed compendium MET the rest
    of the module uses (heart rate is a poor energy proxy under load), so a gym
    session is duration-driven and less personalised than a run.
    """
    if isinstance(date, str):
        date = datetime.date.fromisoformat(date)
    acta = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    acta.row_factory = sqlite3.Row
    gb = sqlite3.connect(f"file:{config.GADGETBRIDGE_DB}?mode=ro&immutable=1", uri=True)
    try:
        load_tz_offsets(acta)
        day = estimate_day(gb, acta, date, now=now, events=events)
    finally:
        acta.close()
        gb.close()

    kcal = day["kcal_min"]
    mode = day["mode_min"]
    bmr_min = day["bmr_kcal_min"]
    k = day["k_factor"]

    a = max(0, int(start_min))
    b = min(len(kcal), a + max(0, int(duration_min)))
    if b <= a:
        return {"date": day["date"], "start_min": a, "duration_min": 0,
                "gross_kcal": 0, "rest_kcal": 0, "net_kcal": 0,
                "minutes_counted": 0, "note": "bout falls outside the recorded day"}

    window = kcal[a:b]
    modes = mode[a:b]
    gross = sum(window) * k
    rest = bmr_min * len(window) * k
    counted = collections.Counter(m or "unknown" for m in modes)
    measured = sum(1 for m in modes if m in ("active", "strength", "sedentary"))

    # Effort and cooldown priced apart: HR stays elevated after the effort stops
    # while the energy cost has already fallen. No discount factor is invented;
    # `net_kcal` is the effort, and the cooldown is reported in cooldown_kcal.
    w = None if work_min is None else max(0, min(len(window), int(work_min)))
    if w is not None and w < len(window):
        eff_gross = sum(window[:w]) * k
        eff_rest = bmr_min * w * k
        cool_gross = sum(window[w:]) * k
        cool_rest = bmr_min * (len(window) - w) * k
        effort_net = round(eff_gross - eff_rest)
        cooldown_net = round(cool_gross - cool_rest)
    else:
        effort_net = round(gross - rest)
        cooldown_net = 0

    return {
        "date": day["date"],
        "start_min": a,
        "duration_min": len(window),
        "work_min": w,
        "gross_kcal": round(gross),
        "rest_kcal": round(rest),
        "net_kcal": effort_net,
        "cooldown_kcal": cooldown_net,
        "total_net_kcal": round(gross - rest),
        "kcal_per_min": round(gross / len(window), 2),
        "minutes_counted": len(window),
        "minutes_measured": measured,
        "coverage_pct": round(100.0 * measured / len(window), 1),
        "modes": dict(counted),
    }


def session_cost(date_iso: str, duration_sec: int, **kw) -> dict:
    """Cost of an app-tracked workout, from its own start timestamp.

    workout_session stores a full UTC timestamp and a duration, so a session
    logged through the app is priced over exactly the minutes it ran -- no
    detection and no guessing at when it started.
    """
    start = datetime.datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
    load_tz = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    try:
        load_tz_offsets(load_tz)
    finally:
        load_tz.close()
    local_date = start.astimezone(BC.day_tz(start.astimezone(TZ).date())).date()
    t0, _ = BC.day_bounds_s(local_date)
    start_min = int((start.timestamp() - t0) // 60)
    return activity_cost(local_date, start_min, max(1, round(duration_sec / 60)), **kw)


def estimate_range(days: int = 14, end=None) -> list[dict]:
    acta = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    gb = sqlite3.connect(f"file:{config.GADGETBRIDGE_DB}?mode=ro&immutable=1", uri=True)
    try:
        load_tz_offsets(acta)
        events = BC.load_events()
        end = end or datetime.datetime.now(TZ).date()
        out = []
        for i in range(days - 1, -1, -1):
            d = end - datetime.timedelta(days=i)
            try:
                out.append(estimate_day(gb, acta, d, events=events))
            except Exception as e:                      # noqa: BLE001
                out.append({"date": d.isoformat(), "error": str(e)})
        return out
    finally:
        acta.close()
        gb.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--range", type=int, metavar="N", help="last N days, one line each")
    ap.add_argument("--update", action="store_true",
                    help="compute + store missing days into daily_energy")
    args = ap.parse_args()

    if args.update:
        print(f"wrote {update(verbose=True)} days")
        return

    if args.range:
        print(f"{'date':<12} {'total':>7} {'rest':>7} {'active':>7} "
              f"{'cov%':>6}  sleep/sed/act/str/imp")
        for r in estimate_range(args.range):
            if "error" in r:
                print(f"{r['date']:<12} {'—':>7}  {r['error']}")
                continue
            m = r["minutes"]
            print(f"{r['date']:<12} {r['total_kcal']:>7} {r['rest_kcal']:>7} "
                  f"{r['active_kcal']:>7} {r['coverage_pct']:>6} "
                  f"  {m['sleep']}/{m['sedentary']}/{m['active']}/"
                  f"{m['strength']}/{m['imputed']}")
        return

    d = (datetime.date.fromisoformat(args.date) if args.date
         else datetime.datetime.now(TZ).date())
    acta = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    gb = sqlite3.connect(f"file:{config.GADGETBRIDGE_DB}?mode=ro&immutable=1", uri=True)
    try:
        load_tz_offsets(acta)
        r = estimate_day(gb, acta, d)
    finally:
        acta.close()
        gb.close()

    print(f"=== {r['date']}  ({r['elapsed_min']} min elapsed) ===")
    print(f"  TOTAL           {r['total_kcal']:>6} kcal")
    print(f"    resting floor {r['rest_kcal']:>6}")
    print(f"    above resting {r['active_kcal']:>6}   (incl. EPOC {r['epoc_kcal']})")
    print(f"  BMR             {r['bmr_kcal_day']:>6} kcal/day")
    print(f"  HR coverage     {r['coverage_pct']:>6}%")
    print("  minutes         " + "  ".join(f"{k}={v}" for k, v in r["minutes"].items()))
    print("  inputs          " + "  ".join(f"{k}={v}" for k, v in r["inputs"].items()))


if __name__ == "__main__":
    _main()
