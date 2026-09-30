"""lag_mining — does feature X on night/day N predict outcome Y on night N+k?

Sweeps a grid of (feature, lag, target) triples over the full history and
reports the ones that survive a false-discovery-rate correction. Pure stdlib
(statistics.correlation). Fully automatic — no user input.

Why FDR rather than a fixed |r| bar
-----------------------------------
The old gate was |r| >= 0.30, which is a round number and not a statistical
test. It was clipping real findings (bed_hour -> hrv at -0.294, bed_hour ->
deep_min at -0.292, both with intervals well clear of zero) while offering no
protection at all against the opposite problem: the grid runs hundreds of tests,
and chance alone produces about 21 hits at p<0.05. Lowering the bar made that
worse -- 0.30 gave 5 pairs, 0.20 gave 40.

Benjamini-Hochberg fixes both ends. It adapts to how many tests actually ran and
caps the expected share of false discoveries at FDR_Q, so the list grows only
where the evidence does. MIN_R survives as a size floor to skip trivia, and the
bootstrap interval is still computed for every survivor -- but to communicate
uncertainty, not to decide.

Correlation is still not causation, and the FDR bounds the false-discovery
fraction rather than eliminating it -- findings are leads for the experiment
runner.

Why the grid is all-objective, and why some pairs are skipped
------------------------------------------------------------
Two problems were fixed on 2026-08-22, after an audit found the sweep was
producing 19 findings of which roughly one was usable.

*Dead and subjective targets.* `sleep_rating` and `body_energy` accounted for 12
of the 19 findings, all frozen at n=39 — logging both stopped on
2026-07-04, deliberately: an app fitted to how you say you feel can only hand
back your own opinion, and the whole point is to surface what you cannot already
feel. They are gone, along with `mental_evening` on the predictor side for the
same reason, and `onset_min`, which is 0 on all 117 nights and so was only ever
a constant series the sweep silently skipped. Objective outcomes replace them.

*Structural pairs.* The strongest "finding" in the old output was RHR → HRV at
r=-0.90, which is not a discovery: both are computed from the same nocturnal
heart-rate signal. Likewise WASO → sleep score, when WASO is an input to that
score, and RHR → RHR one night later, which is autocorrelation. These crowded
out the real leads. They are now filtered — but only where the relationship is
definitional, i.e. at lag 0 for shared-signal and score-input pairs, and at
every lag for a variable against itself. Yesterday's WASO predicting tomorrow's
score is a genuine question and is still swept.

CLI:  python -m acta.insights.lag_mining            # full report
      python -m acta.insights.lag_mining --excluded # also list what the filter removed
Used by monthly_report.py: top_findings(n)
"""
import math
import statistics
import sys

from acta.insights import features

MIN_N = 20        # min paired samples

# A size floor, not a significance test. It exists only to skip trivia; whether
# a correlation is real is decided by FDR_Q below.
#
# It used to be 0.30, which was a round number rather than a statistical bar,
# and it was clipping real findings: bed_hour -> hrv at -0.294 and
# bed_hour -> deep_min at -0.292 were both excluded while their confidence
# intervals comfortably excluded zero.
MIN_R = 0.15

# Benjamini-Hochberg false-discovery rate.
#
# The grid runs 418 unique tests after the definitional filter, so chance alone
# yields about 21 hits at p<0.05 -- which is why simply lowering MIN_R floods
# the output (0.30 gives 5 pairs, 0.20 gives 40). BH adapts to how many tests
# actually ran and caps the expected share of false discoveries, so the list
# grows only where the evidence grows. At q=0.10 it reports 13.
FDR_Q = 0.10
# effective_n is REPORTED, not decisive. It answers "how many values are not the
# most repeated one", which reacts to how a number was written rather than what
# it means: pai_high_min_prev (whole minutes) scored 17 while pai_prev (two
# decimals, r=+0.96 with it -- the same signal) scored 114. What actually
# matters is how much the correlation depends on which nights happened to be in
# the data, and features.bootstrap_ci measures that directly.
BOOTSTRAP_B = 1000

# predictors: (key, human label)
PREDICTORS = [
    ("bed_hour",        "bedtime"),
    ("bio_2200",        "biocharge at 22:00"),
    ("alcohol_prev",    "alcohol the evening before"),
    ("coffee_prev",     "coffees the day before"),
    ("workout_prev_day","workout the day before"),
    ("pai_prev",        "PAI earned the day before"),
    ("pai_high_min_prev","high-zone minutes (day before)"),
    ("pai_mod_min_prev", "moderate-zone minutes (day before)"),
    ("asleep_min",      "sleep duration"),
    ("waso_min",        "WASO"),
    ("score",           "sleep score"),
    ("rhr",             "RHR"),
    ("hrv",             "HRV"),
    ("deep_min",        "deep sleep"),
    ("rem_min",         "REM sleep"),
    ("mid_sleep",       "sleep midpoint"),
]
TARGETS = [
    ("score",       "sleep score"),
    ("rhr",         "RHR"),
    ("hrv",         "HRV"),
    ("waso_min",    "WASO"),
    ("asleep_min",  "sleep duration"),
    ("bed_hour",    "bedtime"),
    ("deep_min",    "deep sleep"),
    ("rem_min",     "REM sleep"),
    ("mid_sleep",   "sleep midpoint"),
]

# hr_dip is deliberately in neither list. It is resting_hr minus the lowest
# sleeping HR, so it largely shadows RHR (r=+0.74 with it), and in this data a
# BIGGER dip tracks WORSE recovery -- against HRV it is r=-0.57. Every finding
# it produced therefore read backwards from intuition and needed a paragraph of
# explanation to be understood. It was generating 4 of 13 findings and adding
# confusion rather than information.
LAGS = [0, 1, 2]  # nights ahead

# Pairs whose correlation is definitional rather than informative.
#
# SAME_SIGNAL: computed from the same underlying measurement, so a same-night
# correlation says only that arithmetic works. SCORE_INPUTS: components the
# sleep score is built from, so predicting the score with them at lag 0 is
# reading the formula back out. Both are skipped ONLY at lag 0 — across nights
# they become real questions.
# Each entry is a pair whose same-night correlation is arithmetic, with the
# reason it is arithmetic. Listed one by one rather than inferred, so adding a
# feature forces a deliberate decision about what it is derived from.
DEFINITIONAL = {
    frozenset(("rhr", "hrv")):            "both computed from the same nocturnal HR signal",
    frozenset(("rhr", "hr_dip")):         "hr_dip is measured against RHR",
    frozenset(("hrv", "hr_dip")):         "both computed from the same nocturnal HR signal",
    # mid_sleep is (bedtime + waketime) / 2 -- a pure arithmetic function of the
    # two with no free term, so it is definitional against BOTH of them.
    frozenset(("bed_hour", "mid_sleep")):  "mid_sleep is computed from bedtime",
    frozenset(("mid_sleep", "wake_hour")): "mid_sleep is computed from wake time",
    # asleep_min against bed_hour or wake_hour is deliberately NOT here. Sleep
    # length has a free term (time awake in bed) and, more to the point, "later
    # bedtime costs sleep" is only true if he fails to compensate by waking
    # later -- which is a fact about his behaviour, not about arithmetic.
    # {asleep_min, mid_sleep} was listed here and was simply wrong: mid_sleep is
    # the sum and asleep_min the difference of the same two numbers, which are
    # independent, and the data agrees at r=+0.10.
    frozenset(("asleep_min", "deep_min")): "deep sleep is part of total sleep",
    frozenset(("asleep_min", "rem_min")):  "REM is part of total sleep",
    frozenset(("asleep_min", "waso_min")): "both partition time in bed",
    frozenset(("deep_min", "rem_min")):    "both scale with total sleep",
    # Huami computes PAI points FROM time in the heart-rate zones, so points
    # against zone minutes is the formula read back out. Measured r=+0.85 and
    # +0.96. Not reachable from lag_mining's own grid (no PAI key is a target),
    # but explore.py proposes freely and hit it on its first run.
    frozenset(("pai_prev", "pai_mod_min_prev")):  "PAI points are computed from zone minutes",
    frozenset(("pai_prev", "pai_high_min_prev")): "PAI points are computed from zone minutes",
}
# Verified against Personal Dashboard/sleep_score.py: score_regularity scores
# (bedtime + waketime)/2, so wake_hour is as much an input as bed_hour.
# hr_dip was dropped from score_physio's v2 spec (2026-09-07) but stays here: it
# is resting_hr minus the lowest sleeping HR (r=+0.73 with RHR), and RHR remains
# a physio input, so hr_dip -> score is still arithmetic, not a discovery.
SCORE_INPUTS = {"asleep_min", "waso_min", "bed_hour", "wake_hour", "deep_min",
                "rem_min", "rhr", "hrv", "hr_dip", "mid_sleep"}


def _structural(pk, tk, lag):
    """Reason this pair is definitional, or None if it is a fair question."""
    if pk == tk:
        return "same variable (autocorrelation)"
    if lag == 0:
        why = DEFINITIONAL.get(frozenset((pk, tk)))
        if why:
            return why
        if tk == "score" and pk in SCORE_INPUTS:
            return "predictor is an input to the sleep score"
        if pk == "score" and tk in SCORE_INPUTS:
            return "target is an input to the sleep score"
    return None


def _series(feats, order):
    return [feats[n] for n in order]


def mine():
    feats = features.build()
    order = sorted(feats)
    rows = _series(feats, order)
    findings = []
    seen = set()
    excluded = []
    candidates, pvals, tested = [], [], 0
    for pk, plabel in PREDICTORS:
        for tk, tlabel in TARGETS:
            for lag in LAGS:
                why = _structural(pk, tk, lag)
                if why:
                    if lag == 0 or pk == tk:
                        excluded.append({"predictor": plabel, "target": tlabel,
                                         "lag": lag, "reason": why})
                    continue
                key = tuple(sorted((pk, tk))) + (lag,)
                if lag == 0 and key in seen:
                    continue  # same-night pairs are symmetric — keep one
                seen.add(key)
                xs, ys = [], []
                for i in range(len(rows) - lag):
                    x = rows[i].get(pk)
                    y = rows[i + lag].get(tk)
                    if x is not None and y is not None:
                        xs.append(float(x))
                        ys.append(float(y))
                if len(xs) < MIN_N:
                    continue
                if len(set(xs)) < 2 or len(set(ys)) < 2:
                    continue  # constant series (e.g. flag never set)
                eff = min(features.effective_n(xs), features.effective_n(ys))
                r = statistics.correlation(xs, ys)
                # EVERY test that ran counts toward the multiple-comparison
                # correction, including the ones too small to report. Filtering
                # by size first and then correcting would understate how many
                # hypotheses were actually examined.
                n = len(xs)
                t = r * math.sqrt(max(n - 2, 1) / max(1e-9, 1 - r * r))
                pval = 2 * (1 - statistics.NormalDist().cdf(abs(t)))
                tested += 1
                pvals.append(pval)
                if abs(r) < MIN_R:
                    continue          # too small to be worth reporting
                # Raw keys travel alongside the display labels: explore.py compares
                # findings against its own English vocabulary, and matching on
                # translated prose never worked. The row is assembled after the BH
                # cutoff below, not here.
                candidates.append({
                    "predictor": plabel, "target": tlabel, "lag": lag,
                    "pk": pk, "tk": tk, "r": r, "p": pval,
                    "n": n, "effective_n": eff, "xs": xs, "ys": ys})
    # ── Benjamini-Hochberg ────────────────────────────────────────────────────
    # Sort every p-value that was computed, find the largest rank i where
    # p(i) <= i/m * q, and keep the candidates at or under that threshold. The
    # bar adapts to how many tests ran: 418 of them here, where p<0.05 alone
    # would let roughly 21 through by chance.
    pvals.sort()
    m = len(pvals) or 1
    cutoff = 0.0
    for i, pv in enumerate(pvals, 1):
        if pv <= i / m * FDR_Q:
            cutoff = pv
    mine.n_tested = tested
    mine.fdr_cutoff = cutoff

    for c in candidates:
        if c["p"] > cutoff:
            continue
        # Bootstrapped only for the survivors -- the interval is reported so the
        # uncertainty is visible, but BH above is what decides.
        lo, hi = features.bootstrap_ci(
            c["xs"], c["ys"], B=BOOTSTRAP_B,
            seed=features.stable_seed(c["pk"], c["tk"], c["lag"]))
        findings.append({
            "predictor": c["predictor"], "target": c["target"], "lag": c["lag"],
            "pk": c["pk"], "tk": c["tk"],
            "r": round(c["r"], 2), "n": c["n"], "effective_n": c["effective_n"],
            "p": round(c["p"], 5),
            "ci_low": None if lo is None else round(lo, 3),
            "ci_high": None if hi is None else round(hi, 3),
        })

    findings.sort(key=lambda f: -abs(f["r"]))
    mine.excluded = excluded          # inspectable without changing the return shape
    return findings


def _fmt(f):
    when = "the same night" if f["lag"] == 0 else f"{f['lag']} night(s) later"
    direction = "↑" if f["r"] > 0 else "↓"
    ci = (f" 95% CI [{f['ci_low']:+.2f}, {f['ci_high']:+.2f}]"
          if f.get("ci_low") is not None else "")
    return (f"{f['predictor']} → {f['target']} {when}: "
            f"r={f['r']:+.2f} {direction} (n={f['n']}){ci}")


def top_findings(n=8):
    """Formatted top findings for the monthly report."""
    return [_fmt(f) for f in mine()[:n]]


def excluded_report():
    """What the structural filter removed, and why."""
    mine()
    out = []
    for e in getattr(mine, "excluded", []):
        when = "same night" if e["lag"] == 0 else f"{e['lag']} night(s) later"
        out.append(f"{e['predictor']} → {e['target']} ({when}): {e['reason']}")
    return out


if __name__ == "__main__":
    fs = mine()
    print(f"{len(fs)} correlations from {getattr(mine, 'n_tested', 0)} tests, "
          f"Benjamini-Hochberg FDR control q={FDR_Q} "
          f"(p ≤ {getattr(mine, 'fdr_cutoff', 0):.5f}), |r| ≥ {MIN_R}, n ≥ {MIN_N}\n")
    for f in fs:
        print(" ", _fmt(f))
    print("\nNote: correlation is not causation. FDR caps the expected share of false "
          f"positives at {FDR_Q:.0%}, not at zero.")
    print("Use these as leads for the experiment runner, not as conclusions.")
    if "--excluded" in sys.argv:
        ex = excluded_report()
        print(f"\n{len(ex)} pairs excluded as definitional:")
        for line in ex:
            print("  ", line)
