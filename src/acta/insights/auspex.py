"""
auspex — the analyst that explains Acta's data back to you.

Why Python decides and the model only narrates
----------------------------------------------
Language models read high variance as a trend and ignore statistics they are
handed, but they reliably respect an authoritative `verdict` field. So every
judgment -- is this a trend, is this day unusual, which nights are the worst,
does this group differ from that one -- is computed in Python and shipped as a
decided fact. The model only explains and connects facts, never derives them.
A question that needs a new judgment needs a new Python function, not a
cleverer prompt.

Read-only over health data
--------------------------
Auspex never modifies anything it analyses. Health tables are opened through a
`mode=ro` URI so a stray write raises instead of corrupting; the only table it
writes is its own `auspex_query` log, through a separate connection. Structural,
not a convention someone has to remember.

Privacy
-------
Prompts carry health data, so routing is pinned to Zero Data Retention
providers: `order` picks DeepInfra first (cheapest ZDR endpoint) with two ZDR
fallbacks, and `zdr: true` is the guardrail -- without it, a fallback past the
ordered list can land on a provider that logs. If no ZDR provider is up the
request fails, which is the correct direction to fail in. The provider that
actually served each request is recorded so the pinning can be audited rather
than trusted.

mental_state is a one-way valve
-------------------------------
`user_log.kind='mental_state'` may be *read* here and described. It must never
feed biocharge, readiness or any score: reading it to explain is fine; letting
it move a number is not.

CLI:
    python -m acta.insights.auspex --list
    python -m acta.insights.auspex --facts sleep_component_drift     # context only, no API call
    python -m acta.insights.auspex --ask  sleep_component_drift
    python -m acta.insights.auspex --ask  day_energy --date 2026-08-21
    python -m acta.insights.auspex --history 10
"""
import argparse
import datetime
import json
import math
import os
import sqlite3
import statistics
import time

from acta import config

TZ = config.TZ
ENV_FILES = (config.ENV_FILE,)
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# gemma-3-12b holds the verdicts as reliably as larger models on the regression
# cases and answers much faster; qwen stays as the fallback. Any smaller model
# must be re-tested against the regression cases before it goes in.
LLM_MODEL = "google/gemma-3-12b-it"
LLM_FALLBACKS = ["qwen/qwen3-235b-a22b-2507"]
# See "Privacy" above. zdr is the guardrail, order is the preference.
LLM_PROVIDER = {"order": ["deepinfra", "novita", "parasail"],
                "zdr": True, "allow_fallbacks": True}
LLM_TIMEOUT = 120
LLM_MAX_TOKENS = 900

# ── verdict thresholds ────────────────────────────────────────────────────────
# A drift only counts as a trend if it is large next to ordinary night-to-night
# variation AND the series actually correlates with time. Two gates because
# either alone is easy to trip by chance across a handful of components: a big
# slope can come from two outliers at the ends, and a modest r can ride on a
# drift too small to matter. MIN_R matches lag_mining's gate, deliberately.
TREND_WINDOW = 30       # nights; drift is expressed per this many nights
TREND_MIN_RATIO = 0.5   # |drift over window| / sd
TREND_MIN_R = 0.30
TREND_MIN_N = 20

GROUP_MIN_N = 10        # per side; below this the normal approximation is junk
GROUP_MAX_P = 0.05
GROUP_MIN_D = 0.30      # Cohen's d

UNUSUAL_LOW_PCT = 20.0
UNUSUAL_HIGH_PCT = 80.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS auspex_query (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                INTEGER NOT NULL,
  question_id       TEXT    NOT NULL,
  question_text     TEXT    NOT NULL,
  params_json       TEXT,
  facts_json        TEXT    NOT NULL,
  context_text      TEXT    NOT NULL,
  model             TEXT    NOT NULL,
  provider          TEXT,
  answer            TEXT,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  cost_usd          REAL,
  latency_ms        INTEGER,
  status            TEXT    NOT NULL,
  error             TEXT,
  created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auspex_ts ON auspex_query(ts);
CREATE INDEX IF NOT EXISTS idx_auspex_qid ON auspex_query(question_id);
"""


def ensure(con: sqlite3.Connection) -> None:
    """Create the log table. API-owned, like the workout tables -- ingest.py
    never touches it."""
    con.executescript(SCHEMA)


def open_ro() -> sqlite3.Connection:
    """Health data. Read-only at the driver level, not by convention."""
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def open_rw() -> sqlite3.Connection:
    """Writable, and used for exactly one thing: appending to auspex_query."""
    con = sqlite3.connect(config.ACTA_DB, timeout=30)
    con.row_factory = sqlite3.Row
    ensure(con)
    return con


_TZ_READY = False


def _register_tz() -> None:
    """biocharge's minute grid is keyed to the device's zone, and that registry
    is populated by ingest.py -- in any other process it is empty and the grid
    silently falls back to Lisbon. pai.py hit exactly this; same fix, same
    source of truth (sleep_score.tz_offset_min)."""
    global _TZ_READY
    if _TZ_READY:
        return
    from acta.engine import pai
    pai.register_tz()
    _TZ_READY = True


# ── statistical primitives ────────────────────────────────────────────────────

def _mean(xs):
    return statistics.fmean(xs) if xs else None


def _sd(xs):
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def _slope(ys) -> float:
    """Least-squares slope per index step."""
    n = len(ys)
    xs = range(n)
    mx = (n - 1) / 2
    my = statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def trend_verdict(values, *, window: int = TREND_WINDOW) -> dict:
    """Is this series trending, or is it just noisy? Decided here, not by the
    model. Returns the numbers *and* the conclusion."""
    ys = [float(v) for v in values if v is not None]
    n = len(ys)
    if n < TREND_MIN_N:
        return {"n": n, "verdict": "INSUFFICIENT DATA",
                "detail": f"only {n} points, need {TREND_MIN_N}"}
    sd = _sd(ys)
    drift = _slope(ys) * window
    ratio = abs(drift) / sd if sd else 0.0
    if sd == 0:
        r = 0.0
    else:
        try:
            r = statistics.correlation(list(range(n)), ys)
        except statistics.StatisticsError:
            r = 0.0
    gate_ratio = ratio >= TREND_MIN_RATIO
    gate_r = abs(r) >= TREND_MIN_R
    real = gate_ratio and gate_r
    direction = "rising" if drift > 0 else "falling"
    # Spell out which gate decided it. A near-miss printed as a rounded "0.30"
    # against a "0.30" bar reads as a contradiction, and a verdict the reader
    # cannot reconcile with the numbers beside it is worse than no verdict.
    base = (f"drift of {drift:+.2f} over {window} nights against a "
            f"night-to-night sd of {sd:.2f}")
    if real:
        detail = (f"{base}; ratio {ratio:.4f} >= {TREND_MIN_RATIO} and "
                  f"|r| {abs(r):.4f} >= {TREND_MIN_R} — both gates cleared")
    else:
        misses = []
        if not gate_ratio:
            misses.append(f"ratio {ratio:.4f} is below the {TREND_MIN_RATIO} bar")
        if not gate_r:
            misses.append(f"|r| {abs(r):.4f} is below the {TREND_MIN_R} bar")
        detail = f"{base}; " + " and ".join(misses)
    return {
        "n": n, "mean": round(_mean(ys), 2), "sd": round(sd, 2),
        "drift_per_window": round(drift, 2), "window_nights": window,
        "drift_vs_sd": round(ratio, 4), "r_with_time": round(r, 4),
        "gate_ratio_passed": gate_ratio, "gate_r_passed": gate_r,
        "verdict": f"REAL TREND ({direction})" if real else "NOISE",
        "detail": detail,
    }


def compare_groups(a, b, *, label_a: str = "A", label_b: str = "B") -> dict:
    """Welch's t between two groups, with a normal approximation for p (fine at
    the n we gate for). Effect size required as well as significance, so a
    trivially small difference across many nights does not get called real."""
    xa = [float(v) for v in a if v is not None]
    xb = [float(v) for v in b if v is not None]
    na, nb = len(xa), len(xb)
    if na < GROUP_MIN_N or nb < GROUP_MIN_N:
        return {"n_a": na, "n_b": nb, "verdict": "INSUFFICIENT DATA",
                "detail": f"{label_a} n={na}, {label_b} n={nb}; "
                          f"need {GROUP_MIN_N} each"}
    ma, mb = _mean(xa), _mean(xb)
    va, vb = statistics.variance(xa), statistics.variance(xb)
    se = math.sqrt(va / na + vb / nb)
    pooled = math.sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2))
    if se == 0:
        t, p = 0.0, 1.0
    else:
        t = (mb - ma) / se
        p = 2 * (1 - statistics.NormalDist().cdf(abs(t)))
    d = (mb - ma) / pooled if pooled else 0.0
    gate_p = p < GROUP_MAX_P
    gate_d = abs(d) >= GROUP_MIN_D
    real = gate_p and gate_d
    base = f"{label_a} {ma:.2f} vs {label_b} {mb:.2f} (difference {mb - ma:+.2f})"
    if real:
        detail = (f"{base}; p {p:.4f} < {GROUP_MAX_P} and |d| {abs(d):.4f} "
                  f">= {GROUP_MIN_D} — both gates cleared")
    else:
        misses = []
        if not gate_p:
            misses.append(f"p {p:.4f} is not below {GROUP_MAX_P}")
        if not gate_d:
            misses.append(f"effect size |d| {abs(d):.4f} is below the {GROUP_MIN_D} bar")
        detail = f"{base}; " + " and ".join(misses)
    return {
        "label_a": label_a, "label_b": label_b, "n_a": na, "n_b": nb,
        "mean_a": round(ma, 2), "mean_b": round(mb, 2),
        "difference": round(mb - ma, 2), "cohens_d": round(d, 4),
        "p_approx": round(p, 4),
        "gate_p_passed": gate_p, "gate_d_passed": gate_d,
        "verdict": "MEANINGFUL DIFFERENCE" if real else "NO MEANINGFUL DIFFERENCE",
        "detail": detail,
    }


def one_sample_verdict(deltas, *, label: str = "change") -> dict:
    """Is a set of paired differences distinguishable from zero?

    One paired delta per session is the unit: pooling every session's baseline
    would re-count the same nights and inflate n.
    """
    xs = [float(v) for v in deltas if v is not None]
    n = len(xs)
    if n < GROUP_MIN_N:
        return {"label": label, "n": n, "verdict": "INSUFFICIENT DATA",
                "detail": f"only {n} sessions with a usable baseline, need {GROUP_MIN_N}"}
    m = _mean(xs)
    sd = _sd(xs)
    if sd == 0:
        return {"label": label, "n": n, "verdict": "NO MEANINGFUL DIFFERENCE",
                "detail": f"every session moved by exactly {m:+.2f}"}
    t = m / (sd / math.sqrt(n))
    p = 2 * (1 - statistics.NormalDist().cdf(abs(t)))
    d = m / sd
    real = p < GROUP_MAX_P and abs(d) >= GROUP_MIN_D
    if real:
        detail = (f"mean change {m:+.2f} across {n} sessions (sd {sd:.2f}); "
                  f"p {p:.4f} < {GROUP_MAX_P} and |d| {abs(d):.4f} >= {GROUP_MIN_D}")
    else:
        misses = []
        if p >= GROUP_MAX_P:
            misses.append(f"p {p:.4f} is not below {GROUP_MAX_P}")
        if abs(d) < GROUP_MIN_D:
            misses.append(f"effect size |d| {abs(d):.4f} is below the {GROUP_MIN_D} bar")
        detail = (f"mean change {m:+.2f} across {n} sessions (sd {sd:.2f}); "
                  + " and ".join(misses))
    return {"label": label, "n": n, "mean_change": round(m, 3), "sd": round(sd, 3),
            "cohens_d": round(d, 4), "p_approx": round(p, 4),
            "verdict": "MEANINGFUL CHANGE" if real else "NO MEANINGFUL DIFFERENCE",
            "detail": detail}


def _pct_rank(value: float, pool) -> float:
    """Percentile of `value` within `pool` (both endpoints inclusive-ish)."""
    xs = [float(v) for v in pool if v is not None]
    if not xs:
        return 50.0
    below = sum(1 for x in xs if x < value)
    equal = sum(1 for x in xs if x == value)
    return 100.0 * (below + 0.5 * equal) / len(xs)


def unusual_verdict(value: float, pool, *, label: str = "value") -> dict:
    """Is today actually unusual, or does it only feel that way? Stops the model
    inventing an explanation for a normal day (or agreeing with a false premise)."""
    xs = [float(v) for v in pool if v is not None]
    if len(xs) < 5:
        return {"verdict": "INSUFFICIENT DATA", "n": len(xs),
                "detail": f"only {len(xs)} comparable days"}
    pct = _pct_rank(value, xs)
    if pct <= UNUSUAL_LOW_PCT:
        v = "UNUSUALLY LOW"
    elif pct >= UNUSUAL_HIGH_PCT:
        v = "UNUSUALLY HIGH"
    else:
        v = "NORMAL"
    return {"verdict": v, "value": round(value, 1), "percentile": round(pct, 1),
            "n": len(xs), "median": round(statistics.median(xs), 1),
            "min": round(min(xs), 1), "max": round(max(xs), 1),
            "detail": (f"{label} {value:.1f} sits at the {pct:.0f}th percentile "
                       f"of the last {len(xs)} comparable days "
                       f"(median {statistics.median(xs):.1f})")}


# ── fact builders ─────────────────────────────────────────────────────────────
# Each returns (facts, context_text). `facts` is the structured record of what
# Python decided; `context_text` is what the model actually sees. They are
# stored separately so a wrong answer can be traced to the analysis or to the
# narration, rather than leaving both suspect.

def _render(title: str, blocks: list[str]) -> str:
    return title + "\n\n" + "\n\n".join(b for b in blocks if b)


def _num(v, spec: str = "", dash: str = "—") -> str:
    """Format a number that may be absent (None appears when history is thin)."""
    if v is None:
        return dash
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return str(v)


def _range_str(rng, dash: str = "not enough history") -> str:
    if not rng or rng[0] is None:
        return dash
    return f"{rng[0]} to {rng[1]}"


def _verdict_line(name: str, v: dict) -> str:
    return f"{name}: {v['verdict']} — {v.get('detail', '')}"


def _night(con, date_iso: str):
    return con.execute(
        "SELECT s.*, p.resting_hr, p.hrv_mean, p.hr_dip FROM sleep_score s "
        "LEFT JOIN night_physio p USING(night_of) WHERE s.night_of = ?",
        (date_iso,)).fetchone()


def _hhmm(ts_ms, tz=None):
    if not ts_ms:
        return "?"
    return datetime.datetime.fromtimestamp(ts_ms / 1000, tz or TZ).strftime("%H:%M")


def _day_window(date_iso: str):
    """Device-local midnight..midnight in ms. Uses biocharge's zone timeline, so
    a travel day is 23h or 25h rather than a wrong 24h slice of the wrong zone."""
    _register_tz()
    from acta.engine import biocharge as BC
    t0, t1 = BC.day_bounds_s(datetime.date.fromisoformat(date_iso))
    return t0 * 1000, t1 * 1000, t0


def _now_ms() -> int:
    return int(datetime.datetime.now(TZ).timestamp() * 1000)


def _window_mean(con, date_iso: str, h_from: int, h_to: int):
    """Mean biocharge between two device-local hours. h_to is exclusive.

    Capped at the present moment: the biocharge table is pre-filled for all 1440
    minutes of today, so an uncapped average would blend in extrapolated hours
    that haven't happened.
    """
    _a, _b, t0 = _day_window(date_iso)
    a = (t0 + h_from * 3600) * 1000
    b = min((t0 + h_to * 3600) * 1000, _now_ms())
    if b <= a:
        return None                      # window has not started yet
    r = con.execute("SELECT AVG(level) m FROM biocharge WHERE minute_ts>=? AND minute_ts<?",
                    (a, b)).fetchone()
    return r["m"] if r else None


def day_deep_dive(con, date_iso: str = None, **_) -> tuple[dict, str]:
    """One day, hour by hour, with the surrounding days as the yardstick."""
    if not date_iso:
        date_iso = (datetime.datetime.now(TZ).date() - datetime.timedelta(days=1)).isoformat()
    a_ms, b_ms, t0 = _day_window(date_iso)
    b_ms = min(b_ms, _now_ms())          # never show the model hours that have not happened

    hours = []
    for r in con.execute(
            "SELECT CAST((minute_ts/1000 - ?) / 3600 AS INT) h, AVG(level) avg, "
            "MIN(level) mn, MAX(level) mx, SUM(COALESCE(recharge,0)) rc, "
            "SUM(COALESCE(drain,0)) dr, GROUP_CONCAT(DISTINCT why_label) lb "
            "FROM biocharge WHERE minute_ts>=? AND minute_ts<? GROUP BY h ORDER BY h",
            (t0, a_ms, b_ms)):
        hours.append({"hour": r["h"], "avg": round(r["avg"], 1), "min": round(r["mn"], 1),
                      "max": round(r["mx"], 1), "recharge": round(float(r["rc"]), 1),
                      "drain": round(float(r["dr"]), 1), "labels": r["lb"] or ""})

    prior = [(datetime.date.fromisoformat(date_iso) - datetime.timedelta(days=k)).isoformat()
             for k in range(1, 31)]
    aft = _window_mean(con, date_iso, 12, 19)
    aft_pool = [m for m in (_window_mean(con, d, 12, 19) for d in prior) if m is not None]
    mor = _window_mean(con, date_iso, 6, 12)
    mor_pool = [m for m in (_window_mean(con, d, 6, 12) for d in prior) if m is not None]

    facts = {"date": date_iso, "hours": hours}
    if aft is not None:
        facts["afternoon"] = unusual_verdict(aft, aft_pool, label="afternoon mean biocharge")
    if mor is not None:
        facts["morning"] = unusual_verdict(mor, mor_pool, label="morning mean biocharge")

    n = _night(con, date_iso)
    if n:
        wake_lvl = con.execute(
            "SELECT level FROM biocharge WHERE minute_ts<=? ORDER BY minute_ts DESC LIMIT 1",
            (n["waketime_ts"],)).fetchone() if n["waketime_ts"] else None
        facts["night_before"] = {
            "night_of": n["night_of"], "score": round(n["score"], 1),
            "asleep_min": n["asleep_min"], "bedtime": _hhmm(n["bedtime_ts"]),
            "waketime": _hhmm(n["waketime_ts"]), "deep_min": n["deep_min"],
            "rem_min": n["rem_min"], "awake_min": n["awake_min"],
            "n_awakenings": n["n_awakenings"], "waso_min": n["waso_min"],
            "onset_latency_min": n["onset_latency_min"],
            "resting_hr": n["resting_hr"], "hrv_mean": n["hrv_mean"],
            "wake_biocharge": round(wake_lvl["level"], 1) if wake_lvl else None,
            "components": {"efficiency": n["c_efficiency"], "regularity": n["c_regularity"],
                           "duration": n["c_duration"], "stage_balance": n["c_stage_balance"],
                           "physio": n["c_physio"]},
        }
        rhr_pool = [r["resting_hr"] for r in con.execute(
            "SELECT resting_hr FROM night_physio WHERE night_of < ? AND resting_hr IS NOT NULL "
            "ORDER BY night_of DESC LIMIT 30", (date_iso,))]
        if n["resting_hr"] is not None and rhr_pool:
            facts["rhr_vs_baseline"] = unusual_verdict(
                n["resting_hr"], rhr_pool, label="resting HR")

    pai_rows = con.execute(
        "SELECT start_min, duration_min, pai_today, peak_hr, avg_hr, status "
        "FROM pai_detection WHERE date = ?", (date_iso,)).fetchall()
    facts["activity"] = [dict(r) for r in pai_rows]

    blocks = [
        f"Day analysed: {date_iso}. Hours are device-local.",
        "Hourly biocharge (0-100). hour | avg | min | max | recharge | drain | labels\n"
        + "\n".join(f'{h["hour"]:02d} | {h["avg"]} | {h["min"]} | {h["max"]} | '
                    f'{h["recharge"]} | {h["drain"]} | {h["labels"]}' for h in hours),
    ]
    vb = []
    for k, lbl in (("morning", "Morning (06:00-11:59) vs last 30 days"),
                   ("afternoon", "Afternoon (12:00-18:59) vs last 30 days"),
                   ("rhr_vs_baseline", "Resting HR vs last 30 nights")):
        if k in facts:
            vb.append(_verdict_line(lbl, facts[k]))
    if vb:
        blocks.append("PRE-COMPUTED VERDICTS (authoritative):\n" + "\n".join(vb))
    if "night_before" in facts:
        nb = facts["night_before"]
        blocks.append(
            f'Preceding night ({nb["night_of"]}): score {nb["score"]}, asleep {nb["asleep_min"]}min '
            f'({nb["bedtime"]} -> {nb["waketime"]}), deep {nb["deep_min"]}, rem {nb["rem_min"]}, '
            f'awake {nb["awake_min"]}, {nb["n_awakenings"]} awakenings, WASO {nb["waso_min"]}min, '
            f'onset {nb["onset_latency_min"]}min, RHR {nb["resting_hr"]}, HRV {nb["hrv_mean"]}, '
            f'biocharge at wake {nb["wake_biocharge"]}.\n'
            f'Components — efficiency {nb["components"]["efficiency"]}/25, '
            f'regularity {nb["components"]["regularity"]}/20, duration {nb["components"]["duration"]}/15, '
            f'stage_balance {nb["components"]["stage_balance"]}/25, physio {nb["components"]["physio"]}/15')
    blocks.append("Detected activity that day: " + (
        "; ".join(f'{r["duration_min"]}min from device-minute {r["start_min"]}, '
                  f'PAI {r["pai_today"]}, peak HR {r["peak_hr"]}, avg HR {r["avg_hr"]} ({r["status"]})'
                  for r in pai_rows) if pai_rows else "none detected"))
    return facts, _render(f"DAY ANALYSIS — {date_iso}", blocks)


def _bc_level_at(con, ts_ms):
    """Stored biocharge level at (or just before) an instant. Capped at now so
    the pre-fill for the rest of today is never read as a real value."""
    if not ts_ms:
        return None
    r = con.execute(
        "SELECT level FROM biocharge WHERE minute_ts <= ? ORDER BY minute_ts DESC LIMIT 1",
        (min(int(ts_ms), _now_ms()),)).fetchone()
    return round(r["level"], 1) if r else None


# A term in the wake-level decomposition below this many points is not the story.
WAKE_MATCH_TOL = 3.0


def wake_recharge(con, date_iso: str = None, **_) -> tuple[dict, str]:
    """What set the biocharge level you woke up at — decomposed, not guessed.

    The wake level is an identity, not a correlation:

        wake ≈ clamp(score_to_seed(sleep_score) · recharge_factor, 15, 95)

    reached by recharging from the level you went to bed at, stage-weighted,
    and it falls short of that target only when the night was too short to
    deliver the full recharge. So this computes the identity and ships a
    decided dominant factor.

    score_to_seed and the SEED bounds are biocharge.py's; the recharge factor
    (RHR vs its 28-night baseline, forward-only gated, clamped 0.85–1.10) is
    night_physio's — the exact two inputs compute_day() itself uses.
    """
    from acta.engine import biocharge as BC
    from acta.engine import night_physio

    if not date_iso:
        date_iso = datetime.datetime.now(TZ).date().isoformat()

    n = _night(con, date_iso)
    used_date = date_iso
    if not n or not n["waketime_ts"]:
        # This morning may not be scored yet (asked at 03:00). Fall back one
        # night, the same way day_start_ms() anchors the visible window.
        used_date = (datetime.date.fromisoformat(date_iso)
                     - datetime.timedelta(days=1)).isoformat()
        n = _night(con, used_date)
    if not n or not n["waketime_ts"] or not n["bedtime_ts"]:
        return ({"dominant_factor": {"verdict": "INSUFFICIENT DATA",
                                     "detail": "no scored night with bed/wake times"}},
                _render("WAKE LEVEL",
                        [f"No scored night with bedtime and waketime for {date_iso} "
                         f"or the night before — nothing to decompose yet."]))

    score = float(n["score"])
    seed = BC.score_to_seed(score)
    factor, rhr, rhr_base = night_physio.recharge_factor_info(used_date)
    adj_target = max(BC.SEED_MIN, min(BC.SEED_MAX, seed * factor))

    bed_lvl = _bc_level_at(con, n["bedtime_ts"])
    wake_lvl = _bc_level_at(con, n["waketime_ts"])
    if wake_lvl is None:
        return ({"dominant_factor": {"verdict": "INSUFFICIENT DATA",
                                     "detail": "night scored but no biocharge row at wake"}},
                _render("WAKE LEVEL",
                        [f"Night {used_date} is scored but has no biocharge row at wake."]))

    # seed - wake  ==  (seed - adj_target) + (adj_target - wake), exactly.
    gap_vs_score = round(seed - wake_lvl, 1)      # + => woke below what the score implies
    rhr_term = round(seed - adj_target, 1)        # + => RHR scaled the target down
    shortfall = round(adj_target - wake_lvl, 1)   # + => recharge didn't reach the target
    headroom = round(adj_target - bed_lvl, 1) if bed_lvl is not None else None

    if headroom is not None and headroom <= WAKE_MATCH_TOL:
        verdict = "ALREADY CHARGED AT BEDTIME"
        detail = (f"went to bed at {bed_lvl}, only {headroom:+.1f} below the night's "
                  f"target of {adj_target:.0f} — almost no recharge to add, so the "
                  f"wake level is essentially the level carried into the night")
    elif gap_vs_score <= -WAKE_MATCH_TOL:
        verdict = "WOKE ABOVE THE SCORE-IMPLIED LEVEL"
        why = (f"resting HR {rhr} was under the {rhr_base:.0f} baseline (factor "
               f"x{factor:.2f}), lifting the target" if rhr_base is not None and factor > 1
               else "pre-midnight sleep from the next night's session topped it up")
        detail = (f"woke at {wake_lvl}, {-gap_vs_score:.1f} above the {seed:.0f} the "
                  f"sleep score alone implies — {why}")
    elif abs(gap_vs_score) <= WAKE_MATCH_TOL:
        verdict = "MATCHES THE SLEEP SCORE"
        detail = (f"woke at {wake_lvl}, within {abs(gap_vs_score):.1f} of the {seed:.0f} "
                  f"implied by the sleep score of {score:.0f} — nothing pulled it down")
    else:
        big_rhr = rhr_term >= WAKE_MATCH_TOL
        big_short = shortfall >= WAKE_MATCH_TOL
        verdict = ("ELEVATED RESTING HR AND SHORT RECHARGE" if big_rhr and big_short
                   else "ELEVATED RESTING HR" if big_rhr
                   else "RECHARGE FELL SHORT OF TARGET" if big_short
                   else "BELOW THE SCORE, NO SINGLE CAUSE")
        bits = []
        if big_rhr:
            bits.append(f"RHR {rhr} vs {rhr_base:.0f} baseline scaled the target from "
                        f"{seed:.0f} down to {adj_target:.0f} (−{rhr_term:.1f} pts)")
        if big_short:
            bits.append(f"the night reached only {wake_lvl}, {shortfall:.1f} short of that "
                        f"{adj_target:.0f} target — {n['asleep_min']} min asleep of "
                        f"{n['time_in_bed_min']} in bed")
        if not bits:
            bits.append(f"woke {gap_vs_score:.1f} below the score-implied {seed:.0f}, "
                        f"split {rhr_term:+.1f} RHR / {shortfall:+.1f} recharge — "
                        f"neither over {WAKE_MATCH_TOL:.0f} pts")
        detail = "; ".join(bits)

    facts = {
        "date_analysed": used_date, "fell_back": used_date != date_iso,
        "sleep_score": round(score, 1), "seed_from_score": round(seed, 1),
        "recharge_factor": round(factor, 3), "resting_hr": rhr,
        "rhr_baseline": round(rhr_base, 1) if rhr_base is not None else None,
        "adjusted_target": round(adj_target, 1),
        "level_at_bedtime": bed_lvl, "level_at_wake": wake_lvl,
        "asleep_min": n["asleep_min"], "time_in_bed_min": n["time_in_bed_min"],
        "gap_below_score": gap_vs_score, "points_from_rhr": rhr_term,
        "points_from_short_recharge": shortfall,
        "dominant_factor": {"verdict": verdict, "detail": detail},
    }

    prior = con.execute(
        "SELECT waketime_ts FROM sleep_score WHERE night_of < ? AND waketime_ts IS NOT NULL "
        "ORDER BY night_of DESC LIMIT 30", (used_date,)).fetchall()
    wake_pool = [w for w in (_bc_level_at(con, r["waketime_ts"]) for r in prior) if w is not None]
    if len(wake_pool) >= 5:
        facts["wake_level_vs_history"] = unusual_verdict(wake_lvl, wake_pool,
                                                        label="wake biocharge")
    if rhr is not None:
        rhr_pool = [r["resting_hr"] for r in con.execute(
            "SELECT resting_hr FROM night_physio WHERE night_of < ? AND resting_hr IS NOT NULL "
            "ORDER BY night_of DESC LIMIT 30", (used_date,))]
        if len(rhr_pool) >= 5:
            facts["rhr_vs_history"] = unusual_verdict(rhr, rhr_pool, label="resting HR")

    vlines = [_verdict_line("Dominant factor", facts["dominant_factor"])]
    for k, lbl in (("wake_level_vs_history", "Wake level vs last 30 nights"),
                   ("rhr_vs_history", "Resting HR vs last 30 nights")):
        if k in facts:
            vlines.append(_verdict_line(lbl, facts[k]))

    fell = (f"  (asked about {date_iso}; that night is not scored yet, so this is the "
            f"night before)\n") if facts["fell_back"] else ""
    if rhr_base is None:
        factor_note = f"x{factor:.3f} (neutral — not enough RHR history to compute it)"
    elif used_date < night_physio.RHR_FACTOR_START:
        factor_note = (f"x{factor:.3f} (neutral — {used_date} predates the "
                       f"{night_physio.RHR_FACTOR_START} forward-only gate; RHR {rhr} vs "
                       f"{rhr_base:.0f} baseline is recorded but was not applied)")
    else:
        factor_note = f"x{factor:.3f} (resting HR {rhr} vs {rhr_base:.0f} baseline)"
    blocks = [
        f"Night waking on {used_date}.\n{fell}".rstrip(),
        "PRE-COMPUTED VERDICTS (authoritative — do not contradict):\n" + "\n".join(vlines),
        "How the wake level is built — an identity, not an estimate:\n"
        f"  sleep score {score:.1f}  ->  score-implied target (seed) {seed:.1f}\n"
        f"  recharge factor {factor_note}  ->  adjusted target {adj_target:.1f}\n"
        f"  went to bed at {bed_lvl}  ->  recharged over {n['asleep_min']} min asleep "
        f"({n['time_in_bed_min']} in bed)  ->  woke at {wake_lvl}\n"
        f"  gap below the score-implied {seed:.1f}: {gap_vs_score:+.1f}"
        f"  =  {rhr_term:+.1f} from RHR scaling  +  {shortfall:+.1f} from recharge shortfall",
        "The recharge factor is resting HR against its own 28-night baseline, clamped "
        "0.85–1.10, and it scales the wake target directly: 0.90 means the wake level "
        "lands ~10% below what sleep duration and quality alone would give. The sleep "
        "score does not contain this — the score grades the sleep, the factor grades "
        "the overnight physiological recovery. They can disagree, and when they do this "
        "is why.",
    ]
    return facts, _render(f"WAKE LEVEL — {used_date}", blocks)


def sleep_components(con, nights: int = 120, **_) -> tuple[dict, str]:
    """Which part of the sleep score is moving, and which is only noisy.

    With a correction that changes what the question can honestly answer.
    Four of the five components are scored against a trailing personal baseline
    (regularity against a 14-night median, duration/stages/physio against a
    21-night one -- see Personal Dashboard/sleep_score.py WINDOW). They are
    therefore detrended by construction: a slow drift is absorbed into the
    baseline within two or three weeks and can never show up as a trend. Only
    c_efficiency is scored absolutely.

    Trending those four and reporting NOISE would be answering a question the
    data cannot be asked -- the verdicts would be guaranteed by the score's
    design rather than earned. So the raw quantities underneath are trended
    alongside them, and the context says plainly which is which.
    """
    cols = [("c_efficiency", "efficiency", 25, True), ("c_regularity", "regularity", 20, False),
            ("c_duration", "duration", 15, False), ("c_stage_balance", "stage_balance", 25, False),
            ("c_physio", "physio", 15, False)]
    raw = [("asleep_min", "minutes asleep"), ("waso_min", "WASO"),
           ("n_awakenings", "awakenings"), ("deep_min", "deep sleep minutes"),
           ("rem_min", "REM minutes")]
    rows = con.execute(
        "SELECT s.night_of, s.score, " + ", ".join(c for c, _, _, _ in cols) + ", "
        + ", ".join(f"s.{k}" for k, _ in raw) +
        ", p.resting_hr, p.hrv_mean FROM sleep_score s "
        "LEFT JOIN night_physio p USING(night_of) WHERE " +
        " AND ".join(f"s.{c} IS NOT NULL" for c, _, _, _ in cols) +
        " ORDER BY s.night_of DESC LIMIT ?", (nights,)).fetchall()
    rows = list(reversed(rows))
    facts = {"n_nights": len(rows),
             "range": [rows[0]["night_of"], rows[-1]["night_of"]] if rows else None,
             "components": {}, "raw": {},
             "total_score": trend_verdict([r["score"] for r in rows]) if rows else {}}
    for c, name, mx, absolute in cols:
        v = trend_verdict([r[c] for r in rows])
        v["max_possible"] = mx
        v["baseline_relative"] = not absolute
        facts["components"][name] = v
    for k, label in raw + [("resting_hr", "resting HR"), ("hrv_mean", "HRV")]:
        facts["raw"][label] = trend_verdict([r[k] for r in rows])

    comp_lines = []
    for _col, name, mx, absolute in cols:
        v = facts["components"][name]
        tag = "" if absolute else "  [baseline-relative: cannot show a slow drift]"
        comp_lines.append(f"{name} (max {mx}): {v['verdict']} — {v['detail']}{tag}")
    raw_lines = [f"{label}: {v['verdict']} — {v['detail']}"
                 for label, v in facts["raw"].items()]
    recent = "\n".join(
        r["night_of"] + " | " + " | ".join(f'{r[c]:.1f}' for c, _, _, _ in cols)
        for r in rows[-14:])
    return facts, _render(
        f"SLEEP COMPONENT ANALYSIS — {facts['n_nights']} nights "
        f"({facts['range'][0]} to {facts['range'][1]})" if rows else "SLEEP COMPONENT ANALYSIS",
        ["IMPORTANT — how to read this. Four of the five components (regularity, "
         "duration, stage_balance, physio) are scored against the user's own trailing "
         "14- or 21-night baseline. They are detrended by construction: a gradual "
         "change is absorbed into the baseline and cannot appear as a trend, so a "
         "NOISE verdict on those four means 'no change relative to his recent normal', "
         "NOT 'nothing changed'. Only efficiency is scored on an absolute scale. The "
         "raw measurements below are the ones that can show real drift.",
         "COMPONENT VERDICTS (authoritative — do not contradict):\n" + "\n".join(comp_lines),
         "RAW MEASUREMENT VERDICTS — these are absolute and can show real drift "
         "(authoritative — do not contradict):\n" + "\n".join(raw_lines),
         f"Total sleep score: {facts['total_score'].get('verdict', 'n/a')} — "
         f"{facts['total_score'].get('detail', '')}",
         "Recent 14 nights (date | " + " | ".join(n for _, n, _, _ in cols) + "):\n" + recent])


def bedtime_drift(con, weeks: int = 8, **_) -> tuple[dict, str]:
    """Is bedtime sliding later, and is it getting more erratic? Two separate
    questions -- a stable-but-late schedule and a drifting one need different
    answers, and the mean alone cannot tell them apart."""
    rows = con.execute(
        "SELECT night_of, bedtime_ts, c_regularity FROM sleep_score "
        "WHERE bedtime_ts IS NOT NULL ORDER BY night_of DESC LIMIT ?",
        (weeks * 7,)).fetchall()
    rows = list(reversed(rows))
    hours = []
    for r in rows:
        dt = datetime.datetime.fromtimestamp(r["bedtime_ts"] / 1000, TZ)
        h = dt.hour + dt.minute / 60
        if h < 12:          # after-midnight bedtimes belong to the prior evening
            h += 24
        hours.append(h)
    facts = {"n_nights": len(rows), "bed_hour_trend": trend_verdict(hours)}

    # Consistency, measured without a smoother: each night contributes one
    # independent observation (how far its bedtime sits from the window's median),
    # and drift is an ordinary two-group question. A rolling sd would overlap
    # windows and make the trend gate artificially easy to pass.
    if len(hours) >= 2 * GROUP_MIN_N:
        med = statistics.median(hours)
        dev = [abs(h - med) for h in hours]
        half = len(dev) // 2
        facts["consistency_change"] = compare_groups(
            dev[:half], dev[half:],
            label_a="first half of the window", label_b="second half")
    else:
        facts["consistency_change"] = {
            "verdict": "INSUFFICIENT DATA",
            "detail": f"{len(hours)} nights, need {2 * GROUP_MIN_N}"}
    # Kept as a description, deliberately with no verdict attached to it.
    blocks = [hours[i:i + 7] for i in range(0, len(hours) - 6, 7)]
    facts["weekly_spread"] = [round(_sd(b), 2) for b in blocks if len(b) == 7]
    facts["mean_bed_hour"] = round(_mean(hours), 2) if hours else None
    recent = "\n".join(
        f'{r["night_of"]} | {int(h % 24):02d}:{int((h % 1) * 60):02d} | '
        f'regularity {_num(r["c_regularity"], ".1f")}'
        for r, h in list(zip(rows, hours))[-14:])
    return facts, _render(
        f"BEDTIME DRIFT — last {facts['n_nights']} nights",
        ["PRE-COMPUTED VERDICTS (authoritative — do not contradict):\n"
         + _verdict_line("Bedtime moving later/earlier", facts["bed_hour_trend"]) + "\n"
         + _verdict_line("Schedule becoming more/less erratic (spread of each night's "
                         "bedtime around the window median, earlier half vs later half)",
                         facts["consistency_change"]),
         f"Mean bedtime over the window: {_num(facts['mean_bed_hour'], '.2f')} "
         f"(as decimal hours; values above 24 are after midnight). "
         + (f"Spread within each successive week, for description only and with no "
            f"verdict attached: {', '.join(str(x) for x in facts['weekly_spread'])} hours."
            if facts["weekly_spread"] else ""),
         "Recent 14 nights (date | bedtime | regularity component):\n" + recent])


def _nights(con, limit: int = 400) -> list:
    return list(reversed(con.execute(
        "SELECT s.night_of, s.score, s.c_efficiency, s.c_regularity, s.c_duration, "
        "s.c_stage_balance, s.c_physio, s.asleep_min, s.waso_min, s.onset_latency_min, "
        "s.n_awakenings, s.bedtime_ts, p.resting_hr, p.hrv_mean "
        "FROM sleep_score s LEFT JOIN night_physio p USING(night_of) "
        "ORDER BY s.night_of DESC LIMIT ?", (limit,)).fetchall()))


# onset_latency_min is deliberately absent: it is 0 on all 117 nights in the
# database -- the hypnogram decoder never populates it -- so comparing it only
# produces a confident "no difference" about a column that holds no data.
_METRICS = [("score", "sleep score"), ("asleep_min", "minutes asleep"),
            ("waso_min", "WASO (minutes awake after sleep onset)"),
            ("n_awakenings", "number of awakenings"),
            ("resting_hr", "resting HR"), ("hrv_mean", "HRV"),
            ("c_regularity", "regularity component"),
            ("c_duration", "duration component"),
            ("c_physio", "physio component")]


def period_compare(con, days: int = 30, **_) -> tuple[dict, str]:
    """Last N nights against the N before. Every metric gets its own verdict, so
    'what changed' cannot quietly become 'what looks different'."""
    rows = _nights(con, days * 2)
    if len(rows) < days + GROUP_MIN_N:
        days = max(GROUP_MIN_N, len(rows) // 2)
    recent, prior = rows[-days:], rows[:-days]
    facts = {"window_nights": days,
             "recent_range": [recent[0]["night_of"], recent[-1]["night_of"]] if recent else None,
             "prior_range": [prior[0]["night_of"], prior[-1]["night_of"]] if prior else None,
             "metrics": {}}
    for k, label in _METRICS:
        facts["metrics"][label] = compare_groups(
            [r[k] for r in prior], [r[k] for r in recent],
            label_a="earlier period", label_b="recent period")
    lines = [_verdict_line(label, facts["metrics"][label]) for _, label in _METRICS]
    return facts, _render(
        f"PERIOD COMPARISON — most recent {days} nights vs the {days} before",
        [f"Recent: {_range_str(facts['recent_range'])}. "
         f"Earlier: {_range_str(facts['prior_range'])}.",
         "PRE-COMPUTED VERDICTS (authoritative — do not contradict):\n" + "\n".join(lines),
         "A metric marked NO MEANINGFUL DIFFERENCE has not changed, regardless of "
         "how its two averages read."])


def weekday_weekend(con, **_) -> tuple[dict, str]:
    """Weekday nights against weekend nights, plus the social-jetlag figure the
    dedicated module already computes."""
    from acta.insights import features, social_jetlag
    feats = features.build()
    rows = _nights(con, 400)
    by_date = {r["night_of"]: r for r in rows}
    wk, we = [], []
    for d, f in feats.items():
        r = by_date.get(d)
        if r is None:
            continue
        (we if f.get("weekend_wake") else wk).append(r)
    facts = {"n_weekday": len(wk), "n_weekend": len(we), "metrics": {},
             "social_jetlag": social_jetlag.analyze() or {}}
    for k, label in _METRICS:
        facts["metrics"][label] = compare_groups(
            [r[k] for r in wk], [r[k] for r in we],
            label_a="weekday", label_b="weekend")
    sj = facts["social_jetlag"]
    lines = [_verdict_line(label, facts["metrics"][label]) for _, label in _METRICS]
    return facts, _render(
        f"WEEKDAY vs WEEKEND — {len(wk)} weekday nights, {len(we)} weekend nights",
        ["PRE-COMPUTED VERDICTS (authoritative — do not contradict):\n" + "\n".join(lines),
         (f"Social jetlag (computed separately): mid-sleep {sj.get('mid_week')} on weekdays "
          f"vs {sj.get('mid_weekend')} at weekends = {sj.get('jetlag_min')} minutes of drift "
          f"(n={sj.get('n_week')}/{sj.get('n_weekend')}). Monday effect: score "
          f"{_num(sj.get('monday_score_delta'), '+.1f')}, RHR "
          f"{_num(sj.get('monday_rhr_delta'), '+.1f')} bpm versus the rest of the week."
          if sj else "Social jetlag: not enough weekday/weekend nights to compute yet."),
         "Social jetlag is a descriptive figure, not a tested difference — treat the "
         "verdicts above as the authority on what actually differs."])


def recovery_curve(con, **_) -> tuple[dict, str]:
    """How many nights does a hard session actually cost? Each session's next
    nights are measured against the fortnight before it, so a session during an
    already-bad stretch is not scored as if it caused the whole stretch."""
    from acta.engine import training_response
    rows = _nights(con, 400)
    idx = {r["night_of"]: i for i, r in enumerate(rows)}
    sessions = [r["date"] for r in con.execute(
        "SELECT DISTINCT date FROM pai_detection ORDER BY date")]
    sessions += [str(s[0]) for s in training_response.session_impacts()]
    sessions = sorted(set(sessions))

    # One delta per session per lag: the post-session night measured against
    # THAT session's own preceding fortnight. Sessions are the unit of
    # replication, so a session during an already-bad stretch is not scored as
    # if it caused the stretch, and no night is counted more than once.
    deltas = {1: {"rhr": [], "hrv": []}, 2: {"rhr": [], "hrv": []},
              3: {"rhr": [], "hrv": []}}
    used = 0
    for d in sessions:
        i = idx.get(d)
        if i is None or i < 14:
            continue
        base = rows[i - 14:i]
        used += 1
        for lag in (1, 2, 3):
            j = i + lag
            if j >= len(rows):
                continue
            for metric, key in (("resting_hr", "rhr"), ("hrv_mean", "hrv")):
                v = rows[j][metric]
                bl = [b[metric] for b in base if b[metric] is not None]
                if v is None or not bl:
                    continue
                deltas[lag][key].append(float(v) - _mean(bl))

    facts = {"n_sessions_used": used, "n_sessions_found": len(sessions), "lags": {}}
    for lag in (1, 2, 3):
        facts["lags"][lag] = {
            "resting_hr": one_sample_verdict(
                deltas[lag]["rhr"], label=f"resting HR on night +{lag} vs that "
                                          f"session's own 14-night baseline"),
            "hrv": one_sample_verdict(
                deltas[lag]["hrv"], label=f"HRV on night +{lag} vs that "
                                          f"session's own 14-night baseline"),
        }
    impacts = training_response.session_impacts()
    lines = []
    for lag in (1, 2, 3):
        lines.append(f"Night +{lag} — resting HR: {facts['lags'][lag]['resting_hr']['verdict']} "
                     f"— {facts['lags'][lag]['resting_hr']['detail']}")
        lines.append(f"Night +{lag} — HRV: {facts['lags'][lag]['hrv']['verdict']} "
                     f"— {facts['lags'][lag]['hrv']['detail']}")
    detail = "\n".join(
        f"{d} | {title} | volume {_num(vol)} | RPE {_num(rpe)} | sRPE {_num(srpe, '.0f')} | "
        f"next-night RHR {_num(drhr, '+.1f')} | next-night HRV {_num(dhrv, '+.1f')}%"
        for d, title, vol, rpe, srpe, drhr, dhrv in impacts) or "no strength sessions logged"
    return facts, _render(
        f"RECOVERY AFTER ACTIVITY — {used} sessions with a full 14-night baseline "
        f"({len(sessions)} activity days found in total)",
        ["PRE-COMPUTED VERDICTS (authoritative — do not contradict). Each compares the "
         "nights following a session against the 14 nights preceding it:\n" + "\n".join(lines),
         "Logged strength sessions with their measured next-night cost:\n" + detail,
         "If every lag reads NO MEANINGFUL DIFFERENCE, the honest answer is that "
         "recovery is not measurably disturbed at this sample size — say so."])


# bio_2200 is deliberately absent. Biocharge at 22:00 is an OUTPUT of Acta's own
# model -- last night's sleep plus today's drain -- not something the user can
# decide to change. Listing it as a lever under a prompt that says "recommend
# only the levers listed" invites the advice "have a higher biocharge at 22:00".
_CONTROLLABLE = [("bed_hour", "bedtime"),
                 ("coffee_prev", "coffees that day"), ("alcohol_prev", "alcohol that day"),
                 ("workout_prev_day", "trained that day"),
                 ("pai_prev", "PAI earned that day"),
                 ("pai_high_min_prev", "minutes in the high HR zone"),
                 ("pai_mod_min_prev", "minutes in the moderate HR zone")]


def _corr_verdict(xs, ys, *, label: str, pk: str = None, tk: str = None,
                  lag: int = 0) -> dict:
    """Pearson r on lag_mining's gates. Pass pk/tk to get the structural
    (definitional-pair) check; the significance gate always applies.
    """
    from acta.insights import features, lag_mining
    if pk and tk:
        why = lag_mining._structural(pk, tk, lag)
        if why:
            return {"label": label, "verdict": "NOT TESTABLE",
                    "detail": f"definitional: {why}"}
    pairs = [(float(a), float(b)) for a, b in zip(xs, ys) if a is not None and b is not None]
    n = len(pairs)
    if n < TREND_MIN_N:
        return {"label": label, "n": n, "verdict": "INSUFFICIENT DATA",
                "detail": f"only {n} paired nights, need {TREND_MIN_N}"}
    a = [p[0] for p in pairs]
    b = [p[1] for p in pairs]
    if len(set(a)) < 2 or len(set(b)) < 2:
        return {"label": label, "n": n, "verdict": "NO SIGNAL",
                "detail": "one of the two series never varies"}
    # effective_n is reported, never decisive: as a gate it reacts to how a number
    # is written rather than what it means (see features.bootstrap_ci).
    eff = min(features.effective_n(a), features.effective_n(b))
    r = statistics.correlation(a, b)
    gate_r = abs(r) >= TREND_MIN_R
    if not gate_r:
        # Cheap pre-filter: no point bootstrapping a correlation too small to
        # care about even if it were certain.
        return {"label": label, "n": n, "effective_n": eff, "r": round(r, 4),
                "verdict": "NOT RELATED",
                "detail": (f"r={r:+.3f} over {n} paired nights; "
                           f"|r| {abs(r):.4f} is below the {TREND_MIN_R} bar")}
    lo, hi = features.bootstrap_ci(
        a, b, seed=features.stable_seed(label, pk or "", tk or "", lag))
    real = features.ci_excludes_zero(lo, hi)
    if lo is None:
        detail = f"r={r:+.3f} over {n} paired nights; interval could not be estimated"
    elif real:
        detail = (f"r={r:+.3f} over {n} paired nights, 95% confident the true value "
                  f"is between {lo:+.3f} and {hi:+.3f}")
    else:
        detail = (f"r={r:+.3f} over {n} paired nights, but the 95% interval "
                  f"[{lo:+.3f}, {hi:+.3f}] includes zero — the data cannot rule out "
                  f"no relationship at all")
    return {"label": label, "n": n, "effective_n": eff, "r": round(r, 4),
            "ci_low": None if lo is None else round(lo, 4),
            "ci_high": None if hi is None else round(hi, 4),
            "verdict": "RELATED" if real else "NOT RELATED", "detail": detail}


def today_advisory(con, **_) -> tuple[dict, str]:
    """What is tonight most likely to turn on? Only levers the user can actually
    pull, each tested against his own history rather than assumed.

    Every lever is tried against several outcomes, not just the sleep score. A
    later bedtime barely moves the score but does cut sleep short and lift RHR
    -- testing against the score alone would have reported "nothing matters",
    which is false and useless."""
    from acta.insights import features, presleep_clusters
    now = datetime.datetime.now(TZ)
    today = now.date().isoformat()
    cur = con.execute(
        "SELECT minute_ts, level, why_label FROM biocharge WHERE minute_ts<=? "
        "ORDER BY minute_ts DESC LIMIT 1", (int(now.timestamp() * 1000),)).fetchone()
    last = _night(con, today)
    rows = _nights(con, 400)
    feats = features.build()
    order = sorted(feats)

    outcomes = [("score", "sleep score"), ("asleep_min", "minutes asleep"),
                ("rhr", "resting HR"), ("hrv", "HRV"), ("waso_min", "WASO")]
    tested, related = {}, []
    for key, lever in _CONTROLLABLE:
        xs = [feats[d].get(key) for d in order]
        for okey, olabel in outcomes:
            ys = [feats[d].get(okey) for d in order]
            v = _corr_verdict(xs, ys, label=f"{lever} -> {olabel}",
                              pk=key, tk=okey, lag=0)
            tested[v["label"]] = v
            if v["verdict"] == "RELATED":
                related.append(v)

    facts = {
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "current_biocharge": round(cur["level"], 1) if cur else None,
        "current_label": cur["why_label"] if cur else None,
        "last_night": {"night_of": last["night_of"], "score": round(last["score"], 1),
                       "asleep_min": last["asleep_min"],
                       "resting_hr": last["resting_hr"],
                       "hrv_mean": last["hrv_mean"]} if last else None,
        "recent_7_night_mean_score": round(_mean([r["score"] for r in rows[-7:]]), 1) if rows else None,
        "levers_tested": len(tested), "levers_related": len(related),
        "related": {v["label"]: v for v in related},
        "evening_types": presleep_clusters.clusters(),
    }
    cl_lines = []
    for i, c in enumerate(facts["evening_types"], 1):
        ce, out = c["centroid"], c["outcome"]
        cl_lines.append(
            f"Type {i} ({c['n']} nights): bedtime ~{ce['bed_hour']:.2f}h, "
            f"biocharge at 22:00 ~{ce['bio_2200']:.0f}, alcohol {ce['alcohol_prev']:.1f}, "
            f"trained {ce['workout_prev_day']:.1f} -> sleep score {out['score']:.1f}, "
            f"RHR {out['rhr']:.1f}, asleep {out['asleep_min']:.0f}min, WASO {out['waso_min']:.1f}min")
    rel = ("\n".join(_verdict_line(v["label"], v) for v in related)
           if related else "NONE. No controllable lever reaches the correlation bar "
                           "against any measured outcome.")
    return facts, _render(
        f"TONIGHT — state as of {facts['now']}",
        [f"Current biocharge {facts['current_biocharge']} "
         f"({facts['current_label'] or 'no label'}). "
         f"Last night ({facts['last_night']['night_of']}): score {facts['last_night']['score']}, "
         f"{facts['last_night']['asleep_min']}min asleep, RHR {facts['last_night']['resting_hr']}, "
         f"HRV {facts['last_night']['hrv_mean']}. "
         f"Mean score over the last 7 nights: {facts['recent_7_night_mean_score']}."
         if facts["last_night"] else f"Current biocharge {facts['current_biocharge']}.",
         f"{len(tested)} lever/outcome pairs were tested. Those that cleared the bar "
         f"(authoritative — do not contradict):\n" + rel,
         "Recurring evening types found by clustering his own nights:\n" + "\n".join(cl_lines),
         "Recommend ONLY the levers listed above as clearing the bar. Any lever not "
         "listed has no measured effect and must not be suggested, however sensible "
         "it sounds in general. If the list is empty, say so plainly."])


def mental_correlates(con, **_) -> tuple[dict, str]:
    """What goes with his better days.

    Deliberately does NOT use features.mental_evening. That feature keeps only
    entries logged from 17:00 onward, on the assumption the rating describes the
    evening leading into a night -- but the ratings are mostly logged between 08:00
    and 12:00, so it discarded 81 of 100 ratings and never even reaches the n>=20
    gate. Read here straight from user_log and paired the way the logging
    actually happens: a rating on day D against the night that ended that
    morning (night_of = D) and the activity of the day before.

    Read-only by design: described, never fed back into a score (see the module
    docstring).
    """
    # Legacy 1-10 entries aren't comparable with the 1-5 scale. mental_predictor
    # owns the bounds and drops them, so they are imported rather than restated.
    from acta.insights.ml.mental_predictor import CAP, FLOOR
    rows = con.execute(
        "SELECT ts, value FROM user_log WHERE kind='mental_state' AND value IS NOT NULL "
        "AND value BETWEEN ? AND ? ORDER BY ts", (FLOOR, CAP)).fetchall()
    by_day = {}
    for r in rows:
        d = datetime.datetime.fromtimestamp(r["ts"] / 1000, TZ).date().isoformat()
        by_day.setdefault(d, []).append(float(r["value"]))
    rated = {d: statistics.fmean(v) for d, v in by_day.items()}

    from acta.insights import features
    nights = {r["night_of"]: r for r in _nights(con, 400)}
    # features already derives pai_prev from Gadgetbridge for EVERY day. The
    # previous version rebuilt it from pai_detection, which only holds days
    # where a bout cleared the detection threshold -- 17 dates -- and defaulted
    # the rest to 0.0, manufacturing a mode that collapsed effective_n from 114
    # to 9. One definition, in the module that owns it.
    feats = features.build()

    days = sorted(d for d in rated if d in nights)
    ys = [rated[d] for d in days]

    def col(fn):
        return [fn(d) for d in days]

    def bed_hour(d):
        ts = nights[d]["bedtime_ts"]
        if not ts:
            return None
        dt = datetime.datetime.fromtimestamp(ts / 1000, TZ)
        h = dt.hour + dt.minute / 60
        return h + 24 if h < 12 else h

    def prev_pai(d):
        f = feats.get(d)
        return f.get("pai_prev") if f else None

    candidates = [
        ("sleep score of the preceding night", lambda d: nights[d]["score"]),
        ("minutes asleep", lambda d: nights[d]["asleep_min"]),
        ("resting HR", lambda d: nights[d]["resting_hr"]),
        ("HRV", lambda d: nights[d]["hrv_mean"]),
        ("WASO", lambda d: nights[d]["waso_min"]),
        ("number of awakenings", lambda d: nights[d]["n_awakenings"]),
        ("bedtime", bed_hour),
        ("regularity component", lambda d: nights[d]["c_regularity"]),
        ("physio component", lambda d: nights[d]["c_physio"]),
        ("PAI earned the day before", prev_pai),
    ]
    tested = {}
    for label, fn in candidates:
        tested[label] = _corr_verdict(col(fn), ys, label=label)
    related = [v for v in tested.values() if v["verdict"] == "RELATED"]
    facts = {"n_rated_days": len(days),
             "n_log_entries": len(rows),
             "range": [days[0], days[-1]] if days else None,
             "mental_mean": round(statistics.fmean(ys), 2) if ys else None,
             "mental_min": min(ys) if ys else None, "mental_max": max(ys) if ys else None,
             "correlates": tested, "n_related": len(related)}
    lines = "\n".join(_verdict_line(v["label"], v) for v in tested.values())
    return facts, _render(
        f"MENTAL STATE — {facts['n_rated_days']} rated days "
        f"({facts['range'][0]} to {facts['range'][1]}), from {facts['n_log_entries']} log entries"
        if days else "MENTAL STATE",
        [f"Self-rating logged during the day, scale {_num(facts['mental_min'], '.0f')}-"
         f"{_num(facts['mental_max'], '.0f')} as used, mean {_num(facts['mental_mean'])}. "
         f"Each rating is paired with the night that ended that morning.",
         "PRE-COMPUTED VERDICTS (authoritative — do not contradict):\n" + lines,
         "This correlates a subjective rating against objective measurements. It "
         "describes what accompanies his better days; it is not evidence of cause, and "
         "nothing here feeds any Acta score. If nothing is marked RELATED, say plainly "
         "that none of the measured factors track his mental state."])


# The third field says whether the attribute is an input to the sleep score the
# nights are ranked by. It is: ranking by score and then reporting that the worst
# nights "share" short sleep and poor regularity is reading the formula back out.
# The independent attributes are the ones that can carry news.
_NIGHT_ATTRS = [("asleep_min", "minutes asleep", True), ("waso_min", "WASO", True),
                ("n_awakenings", "awakenings", False), ("resting_hr", "resting HR", False),
                ("hrv_mean", "HRV", False), ("c_regularity", "regularity component", True),
                ("c_duration", "duration component", True),
                ("c_physio", "physio component", True)]


def night_ranking(con, month: str = None, worst_n: int = 3, **_) -> tuple[dict, str]:
    """The worst nights of a month, ranked here rather than by the model.

    Ranking is arithmetic, so it is done in Python and handed over as a decided
    fact. The "what did they have in
    common" part is computed too: an attribute counts as shared only if all
    three nights sit in the same tail of that month's distribution.
    """
    if not month:
        month = datetime.datetime.now(TZ).strftime("%Y-%m")
    rows = con.execute(
        "SELECT s.night_of, s.score, s.asleep_min, s.waso_min, s.n_awakenings, "
        "s.c_regularity, s.c_duration, s.c_physio, s.bedtime_ts, "
        "p.resting_hr, p.hrv_mean FROM sleep_score s "
        "LEFT JOIN night_physio p USING(night_of) "
        "WHERE s.night_of LIKE ? ORDER BY s.night_of", (month + "%",)).fetchall()
    if not rows:
        return ({"month": month, "n_nights": 0},
                f"NIGHT RANKING — {month}\n\nNo nights recorded for this month.")

    by_score = sorted(rows, key=lambda r: r["score"])
    worst = by_score[:worst_n]

    shared = {}
    for key, label, is_input in _NIGHT_ATTRS:
        pool = [r[key] for r in rows if r[key] is not None]
        if len(pool) < 5:
            continue
        # Rank each of the worst nights against a pool that excludes it, the way
        # unusual_verdict already does. Including it lets a night contribute
        # ~2.3 percentage points to its own percentile at n=22.
        pcts = []
        for w in worst:
            if w[key] is None:
                continue
            others = [r[key] for r in rows
                      if r[key] is not None and r["night_of"] != w["night_of"]]
            pcts.append(_pct_rank(w[key], others))
        if len(pcts) < len(worst):
            continue
        direction = None
        if all(p <= 25 for p in pcts):
            direction = "all unusually LOW"
        elif all(p >= 75 for p in pcts):
            direction = "all unusually HIGH"
        if direction:
            shared[label] = {"direction": direction,
                             "percentiles": [round(p) for p in pcts],
                             "is_score_input": is_input}

    def top3(key, reverse):
        vals = [r for r in rows if r[key] is not None]
        return [(r["night_of"], r[key]) for r in
                sorted(vals, key=lambda r: r[key], reverse=reverse)[:worst_n]]

    facts = {
        "month": month, "n_nights": len(rows),
        "worst_by_score": [{"night_of": r["night_of"], "score": round(r["score"], 1)} for r in worst],
        "worst_by_resting_hr": top3("resting_hr", True),
        "worst_by_hrv": top3("hrv_mean", False),
        "shared_attributes": shared,
        "month_medians": {label: round(statistics.median([r[k] for r in rows if r[k] is not None]), 1)
                          for k, label, _is_input in _NIGHT_ATTRS
                          if any(r[k] is not None for r in rows)},
    }

    detail_lines = []
    for r in worst:
        bits = [f'{label} {r[k]:.1f}' if isinstance(r[k], float) else f'{label} {r[k]}'
                for k, label, _is_input in _NIGHT_ATTRS if r[k] is not None]
        detail_lines.append(f'{r["night_of"]} (score {r["score"]:.1f}): ' + ", ".join(bits))
    shared_txt = ("\n".join(
        f"{lbl}: {v['direction']} (percentiles within the month: "
        f"{', '.join(str(p) for p in v['percentiles'])})"
        + ("  [CIRCULAR: this is an input to the sleep score these nights were "
           "ranked by, so it is partly the ranking restated]" if v["is_score_input"] else
           "  [independent of the ranking]")
        for lbl, v in shared.items())
                  if shared else "NONE. No attribute is in the same tail for all "
                                 f"{worst_n} nights — they do not share a single cause.")
    independent = [name for name, v in shared.items() if not v["is_score_input"]]
    return facts, _render(
        f"NIGHT RANKING — {month}, {len(rows)} nights recorded",
        [f"The {worst_n} worst nights by sleep score, ranked (authoritative — this IS "
         f"the ranking, do not re-derive it):\n"
         + "\n".join(f'{i}. {d["night_of"]} — score {d["score"]}'
                     for i, d in enumerate(facts["worst_by_score"], 1)),
         "Their attributes:\n" + "\n".join(detail_lines),
         "Month medians for comparison: "
         + ", ".join(f"{lbl} {v}" for lbl, v in facts["month_medians"].items()),
         "SHARED ATTRIBUTES — computed, an attribute qualifies only if all "
         f"{worst_n} nights sit in the same tail of this month's distribution:\n" + shared_txt,
         ("Attributes marked CIRCULAR are components of the very score used to pick "
          "these nights, so they explain nothing on their own — mention them only as "
          "description, never as a cause. "
          + (f"Independent of the ranking: {', '.join(independent)}."
             if independent else
             "NOTHING independent of the ranking is shared across all three nights, "
             "which is itself the finding: they do not have a common cause outside "
             "the score's own inputs.")),
         "Worst by other markers, for context: highest resting HR — "
         + "; ".join(f"{d} ({v})" for d, v in facts["worst_by_resting_hr"])
         + ". Lowest HRV — "
         + "; ".join(f"{d} ({v:.0f})" for d, v in facts["worst_by_hrv"]) + ".",
         "Ranking by sleep score, by resting HR and by HRV need not agree; if they "
         "disagree, say so rather than picking one."])


# ── question registry ─────────────────────────────────────────────────────────
# The preset list is the product, not a convenience. Each entry pairs the text
# the user sees with the builder that assembles exactly the facts that question
# needs -- which is what keeps context small, cost bounded, and the model away
# from data it has no business reasoning about.

QUESTIONS = [
    {"id": "day_energy", "builder": day_deep_dive, "params": ["date"],
     "default_date": "yesterday",
     "text": "Why was my biocharge low yesterday afternoon?",
     "group": "Attribution"},
    {"id": "wake_level", "builder": wake_recharge, "params": ["date"],
     "default_date": "today",
     "text": "What drove my biocharge level when I woke up?",
     "group": "Attribution"},
    {"id": "worst_nights", "builder": night_ranking, "params": ["month"],
     "text": "Find my 3 worst recovery nights this month and what they had in common.",
     "group": "Attribution"},
    {"id": "sleep_component_drift", "builder": sleep_components, "params": [],
     "text": "Which component of my sleep score is actually degrading, and which is just noise?",
     "group": "Trends"},
    {"id": "period_compare", "builder": period_compare, "params": [],
     "text": "Compare my last 30 nights to the 30 before. What genuinely changed?",
     "group": "Trends"},
    {"id": "bedtime_drift", "builder": bedtime_drift, "params": [],
     "text": "Is my bedtime drifting later, and is my schedule getting more erratic?",
     "group": "Trends"},
    {"id": "weekday_weekend", "builder": weekday_weekend, "params": [],
     "text": "Weekday versus weekend — how different is my sleep really?",
     "group": "Comparisons"},
    {"id": "recovery_time", "builder": recovery_curve, "params": [],
     "text": "How long do I actually take to return to baseline after a hard session?",
     "group": "Comparisons"},
    {"id": "tonight", "builder": today_advisory, "params": [],
     "text": "Given today, what is most likely to improve tonight's sleep?",
     "group": "Advisory"},
    {"id": "mental_state", "builder": mental_correlates, "params": [],
     "text": "What actually goes with my better mental-state days?",
     "group": "Advisory"},
]
BY_ID = {q["id"]: q for q in QUESTIONS}


SYSTEM = """You are Auspex, the analyst inside Acta, a personal health dashboard.
Answer in English.

The data you are given has already been analysed. Lines marked as PRE-COMPUTED
VERDICTS are conclusions from statistical tests, and they are authoritative.

Rules, in order of importance:
1. Never contradict a verdict. If something is marked NOISE, it is noise, no
   matter how its individual rows look to you. Repeated extreme values are
   variance unless a verdict says otherwise. If a ranking is given, that IS the
   ranking -- do not re-derive it.
2. If the verdicts do not support the premise of the question, say so in your
   FIRST sentence and correct it, then explain what the data does show. Do not
   open by agreeing with a premise you are about to contradict.
3. Use only the data provided. Never introduce population norms, reference
   ranges, or typical values for athletes or anyone else. You have no
   information beyond this context.
4. A negative result is a valid and valuable answer. When the verdicts show
   nothing meaningful, say so directly in your own words and stop -- do not
   manufacture a finding to fill space. Equally, do not open with a negative if
   the verdicts DO show something: lead with whatever the verdicts actually
   support, positive or negative.
5. Your value is connecting facts, not deriving them. Point out where separate
   facts line up into one story.
6. Cite the specific numbers you are given. Be concise, lead with the finding,
   no preamble and no generic health advice."""


def _api_key() -> str | None:
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    for path in ENV_FILES:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("OPENROUTER_API_KEY="):
                        return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return None


def build_context(question_id: str, **params) -> tuple[dict, str, dict]:
    """Run a question's builder. Separated from ask() so the exact context can be
    inspected -- and regression-checked -- without spending an API call."""
    q = BY_ID.get(question_id)
    if not q:
        raise ValueError(f"unknown question id: {question_id}")
    con = open_ro()
    try:
        kwargs = {}
        if "date" in q["params"]:
            # Resolved here, not in the UI: "yesterday afternoon" and "when I
            # woke up" are different days, and which one a question means is a
            # property of the question rather than of the button that fires it.
            d = params.get("date")
            if not d:
                today = datetime.datetime.now(TZ).date()
                d = ((today - datetime.timedelta(days=1)).isoformat()
                     if q.get("default_date") == "yesterday" else today.isoformat())
            kwargs["date_iso"] = d
        if "month" in q["params"] and params.get("month"):
            kwargs["month"] = params["month"]
        facts, text = q["builder"](con, **kwargs)
    finally:
        con.close()
    return q, text, facts


def call_model(system: str, user: str, *, model: str = LLM_MODEL,
               json_mode: bool = False, max_tokens: int = LLM_MAX_TOKENS) -> dict:
    """The single place that talks to OpenRouter.

    Never raises on a bad response -- returns status='error' with the message,
    so a dead provider ends up in the log next to its context instead of
    vanishing into a traceback. Provider is returned because the ZDR pinning is
    only worth anything if it can be audited after the fact.
    """
    import requests

    key = _api_key()
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY (checked env and "
                           + ", ".join(ENV_FILES) + ")")
    body = {"model": model, "provider": LLM_PROVIDER, "temperature": 0.3,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    # OpenRouter tries these in order if the primary is down or refuses. The ZDR
    # pinning in LLM_PROVIDER applies to every one of them, so a fallback cannot
    # quietly route health data to a logging provider.
    if model == LLM_MODEL and LLM_FALLBACKS:
        body["models"] = [model] + LLM_FALLBACKS
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    out = {"content": None, "provider": None, "prompt_tokens": None,
           "completion_tokens": None, "cost_usd": None, "latency_ms": None,
           "status": "error", "error": None, "model": model}
    t0 = time.time()
    try:
        resp = requests.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "X-Title": "Acta Auspex"},
            json=body, timeout=LLM_TIMEOUT)
        out["latency_ms"] = int((time.time() - t0) * 1000)
        if resp.status_code != 200:
            out["error"] = f"OpenRouter {resp.status_code}: {resp.text[:300]}"
            return out
        d = resp.json()
        usage = d.get("usage") or {}
        out.update({"content": (d["choices"][0]["message"]["content"] or "").strip(),
                    "provider": d.get("provider"),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cost_usd": usage.get("cost"), "status": "ok"})
    except Exception as exc:                                  # network, timeout, shape
        out["latency_ms"] = int((time.time() - t0) * 1000)
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


def log_query(*, question_id, question_text, params, facts, context, res) -> int:
    """Append one row to auspex_query. Shared by the preset questions and by
    explore.py, so everything the model was asked lands in one place."""
    row = {"ts": int(time.time() * 1000), "question_id": question_id,
           "question_text": question_text, "params_json": json.dumps(params or {}),
           "facts_json": json.dumps(facts, default=str), "context_text": context,
           "model": res.get("model") or LLM_MODEL, "provider": res.get("provider"),
           "answer": res.get("content"), "prompt_tokens": res.get("prompt_tokens"),
           "completion_tokens": res.get("completion_tokens"),
           "cost_usd": res.get("cost_usd"), "latency_ms": res.get("latency_ms"),
           "status": res.get("status", "error"), "error": res.get("error"),
           "created_at": datetime.datetime.now(TZ).isoformat(timespec="seconds")}
    con = open_rw()
    try:
        cols = ", ".join(row)
        con.execute(f"INSERT INTO auspex_query ({cols}) VALUES "
                    f"({', '.join('?' for _ in row)})", tuple(row.values()))
        con.commit()
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        con.close()


def ask(question_id: str, *, model: str = LLM_MODEL, persist: bool = True,
        **params) -> dict:
    """Answer one preset question and log it."""
    q, context, facts = build_context(question_id, **params)
    res = call_model(SYSTEM, f"{q['text']}\n\n--- DATA ---\n{context}", model=model)
    row = {"ts": int(time.time() * 1000), "question_id": q["id"],
           "question_text": q["text"], "answer": res["content"],
           "provider": res["provider"], "model": model,
           "prompt_tokens": res["prompt_tokens"],
           "completion_tokens": res["completion_tokens"],
           "cost_usd": res["cost_usd"], "latency_ms": res["latency_ms"],
           "status": res["status"], "error": res["error"],
           "created_at": datetime.datetime.now(TZ).isoformat(timespec="seconds")}
    if persist:
        row["id"] = log_query(question_id=q["id"], question_text=q["text"],
                              params=params, facts=facts, context=context, res=res)
    return row


def day_start_ms(now: datetime.datetime = None) -> int:
    """Start of the window the UI shows. Everything before it stays in the
    database and disappears from the screen.

    Waking, not midnight. A mean bedtime around midnight means being regularly up
    past 01:00, so a midnight cut would blank the panel mid-evening -- ask
    something at 23:50, come back at 00:05, it is gone. Waking is also the
    boundary the rest of Acta already uses: sleep_score.night_of IS the wake
    date, and biocharge's day grid is built from the device's own midnight.

    Order matters in the fallbacks: if tonight has not been scored yet (e.g. at
    00:30), yesterday's wake time is the right anchor, not midnight.
    """
    now = now or datetime.datetime.now(TZ)
    today = now.date()
    con = open_ro()
    try:
        for d in (today, today - datetime.timedelta(days=1)):
            row = con.execute(
                "SELECT waketime_ts FROM sleep_score WHERE night_of = ? "
                "AND waketime_ts IS NOT NULL", (d.isoformat(),)).fetchone()
            if row and row["waketime_ts"] <= int(now.timestamp() * 1000):
                return int(row["waketime_ts"])
    finally:
        con.close()
    _register_tz()
    from acta.engine import biocharge as BC
    return BC.day_bounds_s(today)[0] * 1000


def _read_log(sql: str, args: tuple) -> list:
    """Read auspex_query without a writable handle.

    visible() and history() are pure reads but used open_rw(), so every
    GET /api/auspex/today opened a writable connection and re-ran the schema
    script -- forcing an implicit commit on a read path, and quietly weakening
    the "read-only is structural, not a convention" claim in the docstring
    above. The table is created at API start; if it somehow is not there, an
    empty history is the right answer, not a 500.
    """
    con = open_ro()
    try:
        return [dict(r) for r in con.execute(sql, args)]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
    finally:
        con.close()


def visible(limit: int = 50) -> dict:
    """Queries asked since the last wake -- what the tab renders."""
    since = day_start_ms()
    rows = _read_log(
        "SELECT id, ts, question_id, question_text, answer, provider, model, "
        "cost_usd, latency_ms, status, error, created_at FROM auspex_query "
        "WHERE ts >= ? AND status = 'ok' ORDER BY ts ASC LIMIT ?", (since, limit))
    return {"since_ms": since,
            "since_iso": datetime.datetime.fromtimestamp(since / 1000, TZ)
                                  .isoformat(timespec="seconds"),
            "queries": rows, "count": len(rows)}


def history(limit: int = 20, question_id: str = None) -> list[dict]:
    sql = ("SELECT id, ts, question_id, question_text, model, provider, answer, "
           "prompt_tokens, completion_tokens, cost_usd, latency_ms, status, error, "
           "created_at FROM auspex_query")
    args = []
    if question_id:
        sql += " WHERE question_id = ?"
        args.append(question_id)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return _read_log(sql, tuple(args))


def _main():
    ap = argparse.ArgumentParser(description="Auspex — ask Acta about itself")
    ap.add_argument("--list", action="store_true", help="list preset questions")
    ap.add_argument("--facts", metavar="ID", help="print the context only, no API call")
    ap.add_argument("--ask", metavar="ID", help="ask a preset question")
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    ap.add_argument("--month", metavar="YYYY-MM")
    ap.add_argument("--history", nargs="?", type=int, const=20, metavar="N")
    ap.add_argument("--no-persist", action="store_true")
    args = ap.parse_args()

    if args.list:
        for q in QUESTIONS:
            p = f"  (params: {', '.join(q['params'])})" if q["params"] else ""
            print(f"{q['id']:24} [{q['group']}] {q['text']}{p}")
        return
    if args.facts:
        _q, text, facts = build_context(args.facts, date=args.date, month=args.month)
        print(text)
        print(f"\n[{len(text)} chars, ~{len(text)//4} tokens]")
        return
    if args.ask:
        r = ask(args.ask, persist=not args.no_persist,
                date=args.date, month=args.month)
        print(f"Q: {r['question_text']}")
        print(f"[{r['status']}] provider={r['provider']} {r['latency_ms']}ms "
              f"in={r['prompt_tokens']} out={r['completion_tokens']} "
              f"cost=${r['cost_usd'] or 0:.6f}")
        print("-" * 78)
        print(r["answer"] or r["error"])
        return
    if args.history is not None:
        for h in history(args.history):
            print(f"{h['created_at']}  {h['question_id']:24} {h['status']:5} "
                  f"{h['provider'] or '-':14} ${h['cost_usd'] or 0:.6f}")
        return
    ap.print_help()


if __name__ == "__main__":
    _main()
