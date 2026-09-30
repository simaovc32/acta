"""
Sleep Score – §3, §4, §5, §7 of SLEEP_SCORE_PLAN.md

A library, not a program: acta/ingest.py owns the scoring run and writes to
acta.db. The standalone main() (which wrote sleep_nights.json), build_output()
and tune_weights() were removed on 2026-08-29 — they were dead, pointed at
Windows paths from the original laptop, and tune_weights in particular re-fitted
WEIGHTS to subjective morning ratings, which is the one thing Acta must never do.
"""

import datetime
import statistics
import struct

from acta import config

TZ            = config.TZ

WEIGHTS = dict(efficiency=25, regularity=20, duration=15, stages=25, physio=15)
WINDOW      = 21   # trailing nights for rolling baseline (duration, stages, physio)
REG_WINDOW  = 14   # regularity window — adapts within ~2 weeks when schedule shifts
MIN_BL      = 7    # minimum history nights before using personal baseline

# ── Scoring v2 (forward-only; the gate lives in ingest.score_nights) ──────────
# Two changes shipped 2026-09-07, both driven by the alcohol-night finding that
# the physio component barely moved on a night RHR rose 8 bpm and HRV fell 44%:
#   1. score_physio(v2=True) drops hr_dip (anti-correlated with recovery: r=+0.73
#      with RHR in 133 nights) and temp_drop (wrist skin temp, heavily confounded),
#      and weights the two real signals so they drive the score instead of being
#      one-fifth each of a flat mean.
#   2. The flat alcohol penalty halves (-4/-18 -> -2/-9): with physio now doing
#      real work, the full flat penalty double-counted the autonomic hit.
SLEEP_SCORE_V2_START            = "2026-09-07"
PHYSIO_V2_WEIGHTS               = {"resting_hr": 0.45, "hrv_mean": 0.40, "resp_std": 0.15}
PHYSIO_V2_SPEC                  = [("resting_hr", False), ("hrv_mean", True), ("resp_std", False)]
ALCOHOL_SCORE_PENALTY_PER_UNIT_V2 = 2.0
ALCOHOL_SCORE_PENALTY_CAP_V2      = 9.0


# ── §3  Hypnogram decoding ─────────────────────────────────────────────────────

def parse_hypnogram(blob):
    best = []
    i, n = 0, len(blob)
    while i < n - 5:
        rec, j = [], i
        while j < n - 5:
            s, e, code = struct.unpack_from("<HHB", blob, j)
            if code in (4, 5, 7, 8) and e >= s and (e - s) < 2000 and s < 3000:
                rec.append((s, e, code))
                j += 5
            else:
                break
        if len(rec) > len(best):
            best = rec
        i += 1
    return best


MAX_UTC_OFF =  14 * 3600   # Kiritimati
MIN_UTC_OFF = -12 * 3600   # Baker Island


def session_utc_offset(blob):
    """Device UTC offset in seconds, recovered from the session header.

    The header holds the session day's *local* midnight as an absolute epoch,
    so the offset falls straight out of `epoch % 86400`. Returns None when the
    value isn't a plausible offset (malformed blob) so callers fall back home.
    """
    t1 = struct.unpack_from("<I", blob, 0)[0]
    t2 = struct.unpack_from("<I", blob, 4)[0]
    off = (-(min(t1, t2) % 86400)) % 86400
    if off > MAX_UTC_OFF:
        off -= 86400                      # east of the dateline wraps to west
    if off < MIN_UTC_OFF or off % 900:    # every real zone is a quarter-hour
        return None
    return off


def session_base_date(blob):
    """Device-local midnight of the day *before* the session's own day.

    The header epoch is already that local midnight, so no flooring is needed.
    Flooring it in a fixed home zone was a no-op at home but shifted every
    session recorded abroad (-23h at UTC+2), landing nights a day early.
    """
    t1 = struct.unpack_from("<I", blob, 0)[0]
    t2 = struct.unpack_from("<I", blob, 4)[0]
    off = session_utc_offset(blob)
    if off is None:
        base = datetime.datetime.fromtimestamp(min(t1, t2), TZ).replace(
            hour=0, minute=0, second=0, microsecond=0)
    else:
        base = datetime.datetime.fromtimestamp(
            min(t1, t2), datetime.timezone(datetime.timedelta(seconds=off)))
    return base - datetime.timedelta(days=1)


# ── Helpers ────────────────────────────────────────────────────────────────────

def wrap_mins(dt):
    m = dt.hour * 60 + dt.minute
    return m + 1440 if m < 720 else m


def _med(vals):   return statistics.median(vals)
def _mad(vals):   return max(statistics.median([abs(x - _med(vals)) for x in vals]), 1e-6)
def _pick(h, k):  return [x[k] for x in h if x.get(k) is not None]


def _inv_u(val, center, full_band, zero_band):
    dev = abs(val - center)
    if dev <= full_band:  return 100.0
    if dev >= zero_band:  return 0.0
    return 100.0 * (1.0 - (dev - full_band) / (zero_band - full_band))


def _hm(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{h}h{m:02d}m"


# ── §4.1  Efficiency (weight 30, absolute curve) ───────────────────────────────

def extract_waso(segs):
    onset_idx = next((i for i, (_, _, c) in enumerate(segs) if c != 7), len(segs) - 1)
    onset_latency = sum(e - s for s, e, c in segs[:onset_idx] if c == 7)
    post_awake = [(s, e) for s, e, c in segs[onset_idx:] if c == 7]
    toilet_forgiven = 0
    if post_awake and (post_awake[0][1] - post_awake[0][0]) < 10:
        toilet_forgiven = post_awake[0][1] - post_awake[0][0]
    waso = sum(e - s for s, e in post_awake) - toilet_forgiven
    return onset_latency, waso, toilet_forgiven, len(post_awake)


def score_efficiency(asleep, time_in_bed, toilet_forgiven):
    """Returns (score_0_100, eff_pct)."""
    adj = asleep + toilet_forgiven
    eff_pct = adj / time_in_bed * 100 if time_in_bed else 0.0
    if eff_pct >= 92:   raw = 100.0
    elif eff_pct >= 80: raw = 50.0 + (eff_pct - 80) / 12.0 * 50.0
    elif eff_pct >= 60: raw = (eff_pct - 60) / 20.0 * 50.0
    else:               raw = 0.0
    return raw, eff_pct


def score_waso_penalty(waso_min):
    """Flat deduction applied to total score. 0 below 20 min, up to -6 at 60+ min."""
    if waso_min <= 20:
        return 0.0
    if waso_min >= 60:
        return -6.0
    return -6.0 * (waso_min - 20) / 40.0


# Alcohol degrades sleep quality directly — REM suppression, fragmentation, and
# lighter sleep in the second half of the night — so it belongs as a deduction to
# the sleep score itself, scaled by standard-drink units (1 unit = 10g ethanol).
# Because the biocharge wake target is derived from the sleep score
# (morning_target = f(score)), a lower score automatically lowers next-day recovery
# too — one lever, no double-counting.
ALCOHOL_SCORE_PENALTY_PER_UNIT = 4.0    # pts off the 0-100 score, per standard unit
ALCOHOL_SCORE_PENALTY_CAP      = 18.0   # never deduct more than this in total


def score_alcohol_penalty(units, per_unit=ALCOHOL_SCORE_PENALTY_PER_UNIT,
                          cap=ALCOHOL_SCORE_PENALTY_CAP):
    """Deduction (<=0) applied to the total sleep score, scaled by drink units.

    per_unit/cap default to the v1 figures (-4/-18); score_nights passes the v2
    figures (-2/-9) for nights on/after SLEEP_SCORE_V2_START, where score_physio
    now carries the measured autonomic hit and the flat term only needs to cover
    what sensors miss (next-day cognition, strap-off nights).
    """
    if units <= 0:
        return 0.0
    return -min(per_unit * units, cap)


# ── §4.2  Regularity (weight 20) ──────────────────────────────────────────────

def score_regularity(night, history):
    """Returns (score_0_100 | None, signed_dev_min | None)."""
    mids = [(h["bedtime_mins"] + h["waketime_mins"]) / 2 for h in history if "bedtime_mins" in h]
    if len(mids) < 2:
        return None, None
    tonight = (night["bedtime_mins"] + night["waketime_mins"]) / 2
    center  = _med(mids)
    dev     = tonight - center
    return _inv_u(tonight, center, 30, 90), dev


# ── §4.3  Duration (weight 15) ────────────────────────────────────────────────

def score_duration(asleep, history):
    """Returns (score_0_100, baseline_center_min)."""
    vals = _pick(history, "asleep")
    if len(vals) >= MIN_BL:
        center    = _med(vals)
        zero_band = max(120.0, _mad(vals) * 2.5)
    else:
        # Cold start (first week): no personal baseline yet, so centre on a
        # typical adult night (7.5 h) with a wide band. After MIN_BL nights the
        # score is judged against the person's own median, whatever it is.
        center, zero_band = 450.0, 150.0
    return _inv_u(asleep, center, 30.0, zero_band), center


# ── §4.4  Stage balance (weight 25; internally 15 = deep 8 + REM 7) ───────────────────

def score_stages(deep, rem, asleep, history):
    """Returns (pts_0_15, deep_prop, rem_prop, deep_baseline, rem_baseline)."""
    if asleep == 0:
        # Baselines must match the thin-history fallback below (0.25 / 0.25).
        # This branch used to hand back 0.08 for REM, so the same function held
        # two opinions about a typical REM share — and generate_why would have
        # written "REM below your normal (8%)" off a number nothing else uses.
        return 0.0, 0.0, 0.0, 0.25, 0.25
    dp = deep / asleep
    rp = rem  / asleep
    dvals = [h["deep"] / h["asleep"] for h in history if h.get("asleep", 0) > 0]
    rvals = [h["rem"]  / h["asleep"] for h in history if h.get("asleep", 0) > 0]
    if len(dvals) >= MIN_BL:
        dc = _med(dvals)
        dz = max(0.20, _mad(dvals) * 2.5)
    else:
        dc, dz = 0.25, 0.20
    if len(rvals) >= MIN_BL:
        rc = _med(rvals)
        rz = max(0.20, _mad(rvals) * 2.5)
    else:
        rc, rz = 0.25, 0.20   # fallback: ~25% REM (matches corrected data)
    deep_pts = _inv_u(dp, dc, 0.05, dz) * 8 / 100
    rem_pts  = _inv_u(rp, rc, 0.05, rz) * 7 / 100
    return deep_pts + rem_pts, dp, rp, dc, rc


# ── §4.5  Physiological recovery (weight 15) ──────────────────────────────────

def fetch_physio(con, bedtime, waketime):
    bed_s,  wake_s  = int(bedtime.timestamp()),  int(waketime.timestamp())
    bed_ms, wake_ms = bed_s * 1000, wake_s * 1000

    row = con.execute(
        "SELECT MIN(HEART_RATE) FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND HEART_RATE > 30 AND HEART_RATE != 255",
        (bed_s, wake_s),
    ).fetchone()
    min_sleep_hr = row[0] if row else None

    wake_day_ms = int(
        datetime.datetime(waketime.year, waketime.month, waketime.day, tzinfo=TZ).timestamp()
    ) * 1000
    row = con.execute(
        "SELECT HEART_RATE FROM HUAMI_HEART_RATE_RESTING_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND HEART_RATE > 0 ORDER BY TIMESTAMP LIMIT 1",
        (wake_day_ms, wake_day_ms + 86_400_000),
    ).fetchone()
    resting_hr = row[0] if row else None

    hr_dip = (resting_hr - min_sleep_hr) if (resting_hr and min_sleep_hr) else None

    row = con.execute(
        "SELECT AVG(VALUE), COUNT(*) FROM GENERIC_HRV_VALUE_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND VALUE > 0",
        (bed_ms, wake_ms),
    ).fetchone()
    hrv_mean = row[0] if (row and row[1] and row[1] >= 5) else None

    rates = [r[0] for r in con.execute(
        "SELECT RATE FROM HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND RATE > 0",
        (bed_ms, wake_ms),
    ).fetchall()]
    resp_std = statistics.stdev(rates) if len(rates) >= 3 else None

    temps = [r[0] for r in con.execute(
        "SELECT TEMPERATURE FROM GENERIC_TEMPERATURE_SAMPLE "
        "WHERE TIMESTAMP BETWEEN ? AND ? AND TEMPERATURE IS NOT NULL ORDER BY TIMESTAMP",
        (bed_ms, wake_ms),
    ).fetchall() if r[0] is not None]
    if len(temps) >= 10:
        temp_drop = statistics.mean(temps[:5]) - min(temps)
    else:
        temp_drop = None

    return dict(hr_dip=hr_dip, hrv_mean=hrv_mean, resting_hr=resting_hr,
                resp_std=resp_std, temp_drop=temp_drop)


def _physio_sub(val, hist_vals, higher_is_better):
    if val is None or len(hist_vals) < MIN_BL:
        return None
    c, m = _med(hist_vals), _mad(hist_vals)
    dev = (val - c) / m
    if not higher_is_better:
        dev = -dev
    return max(0.0, min(100.0, 50.0 + 25.0 * dev))


def score_physio(physio, history, v2=False):
    """Returns (pts_0_15 | None, sub_scores_dict).

    v1 (nights before SLEEP_SCORE_V2_START): flat mean of five sub-scores —
    hr_dip, hrv_mean, resting_hr, resp_std, temp_drop.

    v2: only the two signals that actually track overnight recovery, weighted so
    they drive the score (RHR 45%, HRV 40%, resp_std 15% as a minor tie-breaker).
    hr_dip is dropped (it is resting_hr minus the lowest sleeping HR, so on a bad
    night an elevated RHR makes the "dip" larger and it scored that as *better* —
    r=+0.73 with RHR, r=-0.57 with HRV across 133 nights). temp_drop is dropped
    (wrist skin temp is dominated by room temperature and bedding). Weights
    renormalise over whatever sub-scores are available, so a missing resp_std
    just leaves RHR+HRV.
    """
    def ph(k):
        return [h["physio"][k] for h in history
                if h.get("physio") and h["physio"].get(k) is not None]

    if v2:
        sub_scores = {k: _physio_sub(physio.get(k), ph(k), hi)
                      for k, hi in PHYSIO_V2_SPEC}
        num = den = 0.0
        for k, w in PHYSIO_V2_WEIGHTS.items():
            if sub_scores.get(k) is not None:
                num += sub_scores[k] * w
                den += w
        if den == 0:
            return None, sub_scores
        return (num / den) * 15 / 100, sub_scores

    spec = [("hr_dip", True), ("hrv_mean", True), ("resting_hr", False),
            ("resp_std", False), ("temp_drop", True)]
    sub_scores = {k: _physio_sub(physio.get(k), ph(k), hi) for k, hi in spec}
    valid = [s for s in sub_scores.values() if s is not None]
    if not valid:
        return None, sub_scores
    return statistics.mean(valid) * 15 / 100, sub_scores


# ── §7  Why text generators ────────────────────────────────────────────────────

def _why_efficiency(eff_pct, onset_latency, waso, toilet_forgiven, positive):
    if positive:
        label = "very little fragmentation" if eff_pct >= 82 else "reasonable continuity"
        return f"+efficiency: {eff_pct:.0f}%, {label}"
    parts = []
    if onset_latency >= 20:
        parts.append(f"{onset_latency} min to fall asleep")
    if waso >= 15:
        suffix = f" (excl. {toilet_forgiven} min toilet wake)" if toilet_forgiven else ""
        parts.append(f"{waso} min WASO{suffix}")
    extra = f", {', '.join(parts)}" if parts else ""
    return f"-efficiency: {eff_pct:.0f}%{extra}"


def _why_regularity(dev_min, bedtime, positive):
    t = bedtime.strftime("%H:%M")
    if positive:
        return "+regularity: bedtime and wake on schedule"
    if dev_min is None:
        return f"-regularity: irregular schedule (bed at {t})"
    direction = "late" if dev_min > 0 else "early"
    return f"-regularity: {abs(dev_min):.0f} min {direction} (bed at {t})"


def _why_duration(asleep, baseline, positive):
    s = _hm(asleep)
    if positive:
        return f"+duration: {s} asleep, at your typical"
    diff = asleep - baseline
    if diff < 0:
        return f"-duration: only {s} asleep, {_hm(-diff)} below your typical"
    return f"-duration: {s} asleep, {_hm(diff)} more than typical"


def _why_stages(dp, rp, dc, rc, positive):
    if positive:
        return f"+stage balance: deep ({dp*100:.0f}%) and REM ({rp*100:.0f}%) near your normal"
    if rp < 0.02:
        return f"-stage balance: REM nearly absent ({rp*100:.0f}%)"
    if abs(dp - dc) >= abs(rp - rc):
        direction = "below" if dp < dc else "above"
        return f"-stage balance: deep ({dp*100:.0f}%) {direction} your normal ({dc*100:.0f}%)"
    direction = "below" if rp < rc else "above"
    return f"-stage balance: REM ({rp*100:.0f}%) {direction} your normal ({rc*100:.0f}%)"


def _why_physio(physio, sub_scores, positive):
    if positive:
        parts = []
        if physio.get("hrv_mean"):   parts.append(f"HRV {physio['hrv_mean']:.0f} ms")
        if physio.get("resting_hr"): parts.append(f"resting HR {physio['resting_hr']:.0f} bpm")
        return f"+recovery: {', '.join(parts) or 'signals above baseline'}"
    scored = {k: v for k, v in sub_scores.items() if v is not None}
    if not scored:
        return "-recovery: physiological signals below baseline"
    wk  = min(scored, key=scored.get)
    val = physio.get(wk)
    labels = {
        "hr_dip":    f"shallow HR dip ({val:.0f} bpm)"            if val is not None else "shallow HR dip",
        "hrv_mean":  f"HRV below baseline ({val:.0f} ms)"          if val is not None else "HRV below baseline",
        "resting_hr":f"morning resting HR elevated ({val:.0f} bpm)"if val is not None else "resting HR elevated",
        "resp_std":  "respiratory rate less stable than usual",
        "temp_drop": "smaller overnight temperature drop",
    }
    return f"-recovery: {labels.get(wk, 'signals below baseline')}"


def generate_why(comp_norms, night, reg_dev, dur_baseline, dp, rp, dc, rc, physio, sub_scores):
    available = {k: v for k, v in comp_norms.items() if v is not None}
    if not available:
        return {"best": "insufficient data", "worst": "insufficient data"}
    best_comp  = max(available, key=available.get)
    worst_comp = min(available, key=available.get)

    def text(comp, positive):
        if comp == "efficiency":
            return _why_efficiency(night["eff_pct"], night["onset_latency"],
                                   night["waso"], night["toilet_forgiven"], positive)
        if comp == "regularity":
            return _why_regularity(reg_dev, night["bedtime"], positive)
        if comp == "duration":
            return _why_duration(night["asleep"], dur_baseline, positive)
        if comp == "stages":
            return _why_stages(dp, rp, dc, rc, positive)
        if comp == "physio":
            return _why_physio(physio, sub_scores, positive)

    return {"best": text(best_comp, True), "worst": text(worst_comp, False)}
