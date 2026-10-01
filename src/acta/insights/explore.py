"""explore — Auspex proposes, Acta tests.

The problem this solves
-----------------------
lag_mining sweeps a fixed grid: 16 predictors x 9 targets x 3 lags. It is
genuinely hypothesis-free within that grid and completely blind outside it. If a
relationship needs a pairing nobody thought to list, the sweep will never find
it, and widening the grid by hand only moves the boundary -- it does not remove
it. Put simply: Python is rigid, it needs edits to change.

Letting a language model loose on the data instead is not the answer either: it
reads variance as trend, even when the contradicting statistic is in front of it.

So the roles are inverted rather than swapped. The model never measures
anything. It proposes *which pairs are worth measuring* -- a question about
plausibility, where a wide prior is an asset -- and every proposal is then run
through the same correlation, the same gates, and the same definitional filter
that lag_mining uses. A hypothesis that fails is reported as failed. Nothing
reaches the user as a finding unless the arithmetic agreed.

That keeps the standing rule intact: the model never produces the insight, only
the candidate. The statistics produce the insight.

What is deliberately not in the vocabulary
------------------------------------------
The subjective columns. mental_day and mental_evening are readable by the
preset question that describes them, but a discovery engine fed self-reports can
only rediscover what the user already told it, and the point of Acta is the
opposite. lag_mining's targets follow the same rule.

CLI:
    python -m acta.insights.explore --vocab        # what the model may propose from
    python -m acta.insights.explore --dry          # propose + test, no narration, no logging
    python -m acta.insights.explore --run          # full loop, logged to auspex_query
"""
import argparse
import json
import statistics

from acta.insights import auspex, features
from acta.insights import lag_mining as LM

N_PROPOSALS = 8

# Objective features only, with English labels -- the model answers in English
# and lag_mining's own labels are Portuguese.
VOCAB = [
    ("bed_hour",          "bedtime (decimal hours; >24 means after midnight)"),
    ("wake_hour",         "wake time (decimal hours)"),
    ("mid_sleep",         "mid-point of the sleep window (decimal hours)"),
    ("asleep_min",        "minutes asleep"),
    ("waso_min",          "minutes awake after first falling asleep"),
    ("deep_min",          "minutes of deep sleep"),
    ("rem_min",           "minutes of REM sleep"),
    ("score",             "sleep score, 0-100"),
    ("rhr",               "resting heart rate at wake (bpm)"),
    ("hrv",               "mean overnight HRV (ms)"),
    ("bio_2200",          "biocharge level at 22:00 the evening before"),
    ("pai_prev",          "PAI points earned the day before"),
    ("pai_mod_min_prev",  "minutes in the moderate HR zone the day before"),
    ("pai_high_min_prev", "minutes in the high HR zone the day before"),
    ("coffee_prev",       "coffees logged the day before"),
    ("alcohol_prev",      "alcohol logged the day before (0/1)"),
    ("workout_prev_day",  "a workout was logged the day before (0/1)"),
    ("workout_volume",    "logged strength-training volume the day before"),
    ("weekend_wake",      "woke on a Saturday or Sunday (0/1)"),
]
VOCAB_KEYS = {k for k, _ in VOCAB}


def coverage() -> dict:
    """Informative nights per feature. Given to the model so it stops proposing
    hypotheses that cannot be tested -- alcohol has 5, and no amount of
    plausibility fixes that."""
    feats = features.build()
    order = sorted(feats)
    return {k: features.effective_n([feats[d].get(k) for d in order])
            for k, _ in VOCAB}


def _vocab_block(cov: dict) -> str:
    lines = ["key | meaning | informative nights available"]
    for k, label in VOCAB:
        lines.append(f"{k} | {label} | {cov.get(k, 0)}")
    return "\n".join(lines)


def _known_block() -> str:
    """Findings in the same vocabulary the model must propose in.

    lag_mining's own labels are Portuguese display strings, and feeding those to
    a model that has been handed an English key list meant the "do not re-propose
    what is already known" rule referred to names it could not match. It now
    prints the raw keys.
    """
    fs = LM.mine()
    if not fs:
        return "Nothing found yet."
    return "\n".join(
        f"{f.get('pk', f['predictor'])} -> {f.get('tk', f['target'])} "
        f"(lag {f['lag']}): r={f['r']:+.2f}, n={f['n']}"
        for f in fs)


PROPOSE_SYSTEM = """You generate hypotheses to be tested statistically. You never
measure anything yourself and you never state a finding.

You will be given the features available from a personal health dataset, how many
informative nights each has, and the relationships already discovered by an
exhaustive sweep of the obvious pairings.

Propose pairs worth testing that the existing sweep would NOT already cover well.
Good proposals are ones where a real mechanism is plausible but the pairing is
not obvious. Rules:

- Use ONLY the exact keys given. A key not in the list is a wasted proposal.
- Do not propose a pair already listed as discovered.
- Do not propose pairs that are true by definition: a variable against itself, a
  component against the total it is part of (deep_min vs asleep_min), or a
  quantity against something arithmetically derived from it (rhr vs hr_dip,
  bed_hour vs mid_sleep).
- Do not propose a feature with fewer informative nights than 20. It cannot be
  tested and the proposal is wasted.
- Prefer lag 1 or 2 where a delayed effect is plausible; lag 0 means same night.
- Each hypothesis needs a one-sentence mechanism. "They might be related" is not
  a mechanism.

Return ONLY a JSON object, no prose and no markdown fence:
{"hypotheses": [{"predictor": "<key>", "target": "<key>", "lag": 0,
                 "direction": "+" or "-", "rationale": "<one sentence>"}]}"""


def propose(n: int = N_PROPOSALS) -> tuple[list, dict]:
    cov = coverage()
    user = (f"Features available:\n{_vocab_block(cov)}\n\n"
            f"Already discovered by the exhaustive sweep (do not re-propose):\n"
            f"{_known_block()}\n\n"
            f"Propose {n} hypotheses worth testing.")
    res = auspex.call_model(PROPOSE_SYSTEM, user, json_mode=True, max_tokens=1200)
    if res["status"] != "ok":
        return [], res
    try:
        data = json.loads(res["content"])
        hyps = data.get("hypotheses", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, AttributeError):
        res["status"] = "error"
        res["error"] = "model did not return parseable JSON"
        return [], res
    return hyps, res


def validate(hyps: list) -> tuple[list, list]:
    """Drop what cannot or should not be tested, keeping the reason. The model
    is told these rules; this enforces them rather than trusting it."""
    ok, rejected = [], []
    seen = set()
    # Raw keys, not display labels -- see _known_block. Matching on the
    # Portuguese prose meant this rule silently never fired.
    known = set()
    for f in LM.mine():
        pk, tk, lag = f.get("pk"), f.get("tk"), f["lag"]
        if pk and tk:
            known.add((pk, tk, lag))
            if lag == 0:
                known.add((tk, pk, lag))       # lag 0 is symmetric
    for h in hyps:
        pk, tk = str(h.get("predictor", "")), str(h.get("target", ""))
        try:
            lag = int(h.get("lag", 0))
        except (TypeError, ValueError):
            lag = -1
        why = None
        if pk not in VOCAB_KEYS or tk not in VOCAB_KEYS:
            why = "uses a key that does not exist"
        elif lag not in LM.LAGS:
            why = f"lag {h.get('lag')} is not one of {LM.LAGS}"
        elif (pk, tk, lag) in seen:
            why = "duplicate of another proposal"
        elif (pk, tk, lag) in known:
            why = "already found by the exhaustive sweep"
        elif LM._structural(pk, tk, lag):
            why = "definitional: " + LM._structural(pk, tk, lag)
        # Coverage is not a hard reject: the model is told how many informative nights
        # each feature has, and the bootstrap in test() answers honestly for a thin one.
        seen.add((pk, tk, lag))
        if lag == 0:
            seen.add((tk, pk, lag))   # A->B and B->A same night are one test
        h = dict(h, predictor=pk, target=tk, lag=lag)
        (rejected if why else ok).append(dict(h, reason=why) if why else h)
    return ok, rejected


def test(hyps: list) -> list:
    """Run each hypothesis through lag_mining's own gates. Same MIN_R, same
    MIN_N, same effective-n rule -- a hit here means what a hit there means."""
    feats = features.build()
    order = sorted(feats)
    rows = LM._series(feats, order)
    out = []
    for h in hyps:
        pk, tk, lag = h["predictor"], h["target"], h["lag"]
        xs, ys = [], []
        for i in range(len(rows) - lag):
            x, y = rows[i].get(pk), rows[i + lag].get(tk)
            if x is not None and y is not None:
                xs.append(float(x))
                ys.append(float(y))
        rec = dict(h, n=len(xs))
        if len(xs) < LM.MIN_N or len(set(xs)) < 2 or len(set(ys)) < 2:
            rec.update(verdict="UNTESTABLE", detail=f"only {len(xs)} usable pairs")
            out.append(rec)
            continue
        eff = min(features.effective_n(xs), features.effective_n(ys))
        rec["effective_n"] = eff
        r = statistics.correlation(xs, ys)
        rec["r"] = round(r, 3)
        if abs(r) < LM.MIN_R:
            rec.update(verdict="NOT SUPPORTED",
                       detail=f"r={r:+.3f}, below the {LM.MIN_R} bar (n={len(xs)})")
            out.append(rec)
            continue
        lo, hi = features.bootstrap_ci(
            xs, ys, B=LM.BOOTSTRAP_B, seed=features.stable_seed(pk, tk, lag))
        rec["ci_low"], rec["ci_high"] = (
            None if lo is None else round(lo, 3), None if hi is None else round(hi, 3))
        if not features.ci_excludes_zero(lo, hi):
            rec.update(verdict="NOT SUPPORTED",
                       detail=(f"r={r:+.3f} (n={len(xs)}), but the 95% interval "
                               f"[{lo:+.3f}, {hi:+.3f}] includes zero — with "
                               f"{eff} informative nights the data cannot rule out "
                               f"no relationship"
                               if lo is not None else
                               f"r={r:+.3f}, interval could not be estimated"))
            out.append(rec)
            continue
        predicted = h.get("direction", "?")
        agrees = (r > 0 and predicted == "+") or (r < 0 and predicted == "-")
        rec.update(verdict="SUPPORTED" if agrees else "SUPPORTED, OPPOSITE DIRECTION",
                   detail=(f"r={r:+.3f} (n={len(xs)}), 95% interval "
                           f"[{lo:+.3f}, {hi:+.3f}]; predicted {predicted}"))
        out.append(rec)
    return out


NARRATE_SYSTEM = """You are Auspex, the analyst inside Acta, a personal health dashboard.
Answer in English.

You proposed these hypotheses and each has now been tested against the user's own
data. The verdicts are authoritative and you may not argue with them.

- Report only what was SUPPORTED. Give the number and say what it would mean.
- State plainly how many were tested and how many survived. If none survived,
  say so directly -- that is a real result, not a failure to explain away.
- A rejected or unsupported hypothesis is not evidence of anything. Never
  present one as a "trend" or "worth watching".
- Correlation is not cause, and this is a small personal dataset. Frame anything
  that survived as a lead to test deliberately, not as an established fact.
- Be concise. No preamble."""


def run(n: int = N_PROPOSALS, persist: bool = True) -> dict:
    hyps, pres = propose(n)
    if pres["status"] != "ok":
        # Logged like any other failure, so everything the model was asked lands in one place.
        if persist:
            auspex.log_query(question_id="explore",
                             question_text=f"Propose and test {n} new hypotheses about my data",
                             params={"n": n, "stage": "propose"},
                             facts={"stage": "propose", "hypotheses": []},
                             context="(proposal stage failed before any test)", res=pres)
        return {"status": "error", "error": pres.get("error"), "stage": "propose"}
    accepted, rejected = validate(hyps)
    results = test(accepted)
    supported = [r for r in results if r["verdict"].startswith("SUPPORTED")]

    lines = [f"{len(hyps)} hypotheses proposed, {len(accepted)} testable, "
             f"{len(supported)} supported.", ""]
    if results:
        lines.append("TESTED (verdicts are authoritative):")
        for r in results:
            lines.append(f"- {r['predictor']} -> {r['target']} (lag {r['lag']}): "
                         f"{r['verdict']} — {r['detail']}. Reasoning given: {r.get('rationale','')}")
    if rejected:
        lines.append("")
        lines.append("REJECTED before testing:")
        for r in rejected:
            lines.append(f"- {r['predictor']} -> {r['target']} (lag {r['lag']}): {r['reason']}")
    context = "\n".join(lines)

    res = auspex.call_model(NARRATE_SYSTEM, context, max_tokens=700)
    facts = {"proposed": hyps, "accepted": accepted, "rejected": rejected,
             "results": results, "n_supported": len(supported),
             "propose_cost_usd": pres.get("cost_usd"),
             "propose_provider": pres.get("provider")}
    out = {"status": res["status"], "answer": res["content"], "error": res["error"],
           "provider": res["provider"], "latency_ms": res["latency_ms"],
           "cost_usd": (res.get("cost_usd") or 0) + (pres.get("cost_usd") or 0),
           "results": results, "rejected": rejected, "n_supported": len(supported),
           "context": context}
    if persist:
        merged = dict(res)
        merged["cost_usd"] = out["cost_usd"]
        out["id"] = auspex.log_query(
            question_id="explore",
            question_text=f"Propose and test {n} new hypotheses about my data",
            params={"n": n}, facts=facts, context=context, res=merged)
    return out


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vocab", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("-n", type=int, default=N_PROPOSALS)
    a = ap.parse_args()
    if a.vocab:
        cov = coverage()
        print(_vocab_block(cov))
        thin = [(k, cov.get(k, 0)) for k, _ in VOCAB if cov.get(k, 0) < 20]
        print("\nInformative nights is context, not a gate -- a thin feature can "
              "still be proposed and\nwill simply come back with an interval that "
              "includes zero. Thinnest: "
              + ", ".join(f"{k}({n})" for k, n in sorted(thin, key=lambda x: x[1])))
        return
    if a.dry:
        hyps, res = propose(a.n)
        if res["status"] != "ok":
            print("propose failed:", res["error"])
            return
        acc, rej = validate(hyps)
        print(f"proposed {len(hyps)}, testable {len(acc)}, rejected {len(rej)}  "
              f"[{res['provider']} ${res.get('cost_usd') or 0:.6f}]\n")
        for r in test(acc):
            print(f"  {r['verdict']:28} {r['predictor']} -> {r['target']} "
                  f"(lag {r['lag']})  {r['detail']}")
        for r in rej:
            print(f"  {'REJECTED':28} {r['predictor']} -> {r['target']}  {r['reason']}")
        return
    if a.run:
        out = run(a.n)
        if out["status"] != "ok":
            print("failed:", out.get("error"))
            return
        print(f"[{out['provider']}] ${out['cost_usd']:.6f} {out['latency_ms']}ms "
              f"id={out.get('id')}  supported={out['n_supported']}")
        print("-" * 78)
        print(out["answer"])
        return
    ap.print_help()


if __name__ == "__main__":
    _main()
