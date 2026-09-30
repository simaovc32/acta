"""monthly_report — the feedback loop: one Markdown note per month with trends,
month-over-month deltas, and sections from every analysis module.

Written to $ACTA_DATA_DIR/reports/YYYY-MM.md (drop that folder into a notes
vault to read it there).

    python -m acta.insights.monthly_report                  # the month that just ended
    python -m acta.insights.monthly_report --month 2026-06
"""
import datetime
import statistics
import sys
from pathlib import Path

from acta import config
from acta.engine import pai
from acta.finance import ledger
from acta.insights import experiments, features, illness_fingerprint, lag_mining, presleep_clusters, social_jetlag

REPORT_DIR = Path(config.REPORT_DIR)
TZ = config.TZ


def _month_nights(feats, ym):
    return {n: f for n, f in feats.items() if n.startswith(ym)}


def _mean(vals, nd=1):
    vals = [v for v in vals if v is not None]
    return round(statistics.mean(vals), nd) if vals else None


def _fmt_delta(cur, prev, unit="", nd=1):
    if cur is None:
        return "—"
    s = f"{cur}{unit}"
    if prev is not None:
        s += f" ({cur - prev:+.{nd}f} vs previous month)"
    return s


def _stats(month_feats):
    g = lambda k: [f.get(k) for f in month_feats.values()]
    best = worst = None
    scored = [(n, f["score"]) for n, f in month_feats.items() if f.get("score") is not None]
    if scored:
        best = max(scored, key=lambda x: x[1])
        worst = min(scored, key=lambda x: x[1])
    return {
        "n": len(month_feats),
        "score": _mean(g("score")),
        "asleep_h": _mean([v / 60 for v in g("asleep_min") if v is not None]),
        "bed_hour": _mean(g("bed_hour")),
        "waso": _mean(g("waso_min")),
        "rhr": _mean(g("rhr")),
        "hrv": _mean(g("hrv")),
        "bio22": _mean(g("bio_2200")),
        "workouts": sum(1 for v in g("workout_prev_day") if v),
        "alcohol": sum(1 for v in g("alcohol_prev") if v),
        "coffee": sum(v or 0 for v in g("coffee_prev")),
        "best": best, "worst": worst,
    }


def _hhmm(virtual_h):
    if virtual_h is None:
        return "—"
    h = virtual_h % 24
    return f"{int(h):02d}:{int(round((h % 1) * 60)):02d}"


def _finance_summary():
    """Active subscriptions + totals. Not month-scoped like the sleep/recovery
    sections above — a subscription list is a current-state fact, not a
    historical one, so this reads today's finance_sub rows regardless of
    which ym is being reported."""
    subs = ledger.active_subs()
    if not subs:
        return ["No subscriptions."]
    totals = ledger.monthly_totals()
    lines = [f"{len(subs)} active subscriptions: €{totals['monthly_out']:.0f} out per month "
             f"(€{totals['annual_out']:.0f}/year) · €{totals['monthly_in']:.0f} in per month"]
    for s in sorted(subs, key=lambda s: s["day_of_month"]):
        sign = "+" if s["direction"] == "in" else "-"
        lines.append(f"{s['name']}: {sign}€{s['amount']:.0f} (day {s['day_of_month']})")
    return lines


def build_report(ym):
    feats = features.build()
    prev_ym = (datetime.date.fromisoformat(ym + "-01")
               - datetime.timedelta(days=1)).strftime("%Y-%m")
    cur = _stats(_month_nights(feats, ym))
    prev = _stats(_month_nights(feats, prev_ym))

    L = []
    L.append("---")
    L.append("tags: [report, monthly, acta]")
    L.append(f"created: {datetime.date.today().isoformat()}")
    L.append("---\n")
    L.append(f"# Monthly report: {ym}\n")
    L.append(f"*{cur['n']} nights recorded (previous month: {prev['n']})*\n")

    L.append("## Sleep")
    L.append(f"- Mean score: {_fmt_delta(cur['score'], prev['score'])}")
    L.append(f"- Mean duration: {_fmt_delta(cur['asleep_h'], prev['asleep_h'], 'h')}")
    L.append(f"- Mean bedtime: {_hhmm(cur['bed_hour'])}"
             + (f" (previous month {_hhmm(prev['bed_hour'])})" if prev['bed_hour'] else ""))
    L.append(f"- Mean WASO: {_fmt_delta(cur['waso'], prev['waso'], 'm')}")
    if cur["best"]:
        L.append(f"- Best night: {cur['best'][0]} ({cur['best'][1]:.0f}) · "
                 f"worst: {cur['worst'][0]} ({cur['worst'][1]:.0f})")
    L.append("")

    L.append("## Recovery")
    L.append(f"- Mean resting HR: {_fmt_delta(cur['rhr'], prev['rhr'], ' bpm')}")
    L.append(f"- Mean HRV: {_fmt_delta(cur['hrv'], prev['hrv'], ' ms')}")
    L.append(f"- Biocharge at 22:00 (mean): {_fmt_delta(cur['bio22'], prev['bio22'])}")
    L.append("")

    L.append("## Habits")
    L.append(f"- Training days: {cur['workouts']} (previous: {prev['workouts']})")
    L.append(f"- Nights after alcohol: {cur['alcohol']} · coffees logged: {cur['coffee']}")
    L.append("")

    L.append("## Cardio load (PAI)")
    # Guarded: this is the only section reading Gadgetbridge.db rather than
    # acta.db, so an unreachable strap database would otherwise take down the
    # whole monthly report.
    try:
        for line in pai.summary(ym):
            L.append(f"- {line}")
    except Exception as e:
        L.append(f"- (unavailable: {e})")
    L.append("")

    L.append("## Social jetlag")
    for line in social_jetlag.summary():
        L.append(f"- {line}")
    L.append("")

    L.append("## Night types (clustering)")
    for line in presleep_clusters.summary():
        L.append(f"- {line}")
    L.append("")

    L.append("## Correlations (lag mining: leads, not conclusions)")
    for line in lag_mining.top_findings(8):
        L.append(f"- {line}")
    L.append("")

    L.append("## Experiments")
    for line in experiments.summary():
        L.append(f"- {line}")
    L.append("")

    L.append("## Finance")
    for line in _finance_summary():
        L.append(f"- {line}")
    L.append("")

    L.append("## Health alarms")
    for line in illness_fingerprint.report():
        L.append(f"- {line}")
    L.append("")

    L.append(f"*Generated by acta.insights.monthly_report: the analysis sections use the "
             f"full history, the monthly statistics use {ym} only.*")
    return "\n".join(L), cur, prev


def main():
    args = sys.argv[1:]
    now = datetime.datetime.now(TZ)
    if "--month" in args:
        ym = args[args.index("--month") + 1]
    else:
        ym = (now.date().replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")

    text, _cur, _prev = build_report(ym)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{ym}.md"
    out.write_text(text, encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    main()
