"""social_jetlag — weekday vs weekend sleep-schedule drift, fully automatic.

Social jetlag = |median mid-sleep on weekend wakes − median mid-sleep on
weekday wakes|. Also the "Monday effect": how Monday-wake nights compare to
the rest. All from sleep_score timestamps — no manual input.

CLI:  python -m acta.insights.social_jetlag [days_back]
Used by monthly_report.py: summary(month_nights)
"""
import datetime
import statistics

from acta.insights import features


def _hhmm(virtual_h):
    h = virtual_h % 24
    return f"{int(h):02d}:{int(round((h % 1) * 60)):02d}"


def analyze(nights=None):
    """nights: optional list of night_of strings to restrict to (e.g. a month)."""
    feats = features.build()
    keys = [n for n in sorted(feats) if nights is None or n in nights]
    week, weekend, monday = [], [], []
    scores_week, scores_monday = [], []
    rhr_week, rhr_monday = [], []
    for n in keys:
        f = feats[n]
        if f.get("mid_sleep") is None:
            continue
        wd = datetime.date.fromisoformat(n).weekday()
        if wd >= 5:
            weekend.append(f["mid_sleep"])
        else:
            week.append(f["mid_sleep"])
            if wd == 0:
                monday.append(f["mid_sleep"])
                if f.get("score") is not None: scores_monday.append(f["score"])
                if f.get("rhr") is not None:   rhr_monday.append(f["rhr"])
            else:
                if f.get("score") is not None: scores_week.append(f["score"])
                if f.get("rhr") is not None:   rhr_week.append(f["rhr"])
    if len(week) < 5 or len(weekend) < 3:
        return None
    mid_week = statistics.median(week)
    mid_wend = statistics.median(weekend)
    return {
        "jetlag_min": round(abs(mid_wend - mid_week) * 60),
        "mid_week": _hhmm(mid_week),
        "mid_weekend": _hhmm(mid_wend),
        "n_week": len(week), "n_weekend": len(weekend),
        "monday_score_delta": (round(statistics.mean(scores_monday)
                                     - statistics.mean(scores_week), 1)
                               if len(scores_monday) >= 3 and scores_week else None),
        "monday_rhr_delta": (round(statistics.mean(rhr_monday)
                                   - statistics.mean(rhr_week), 1)
                             if len(rhr_monday) >= 3 and rhr_week else None),
    }


def summary(nights=None):
    a = analyze(nights)
    if a is None:
        return ["Not enough data for social jetlag."]
    lines = [
        f"Mid-sleep weekdays {a['mid_week']} vs weekend {a['mid_weekend']} "
        f"→ social jetlag of {a['jetlag_min']} min "
        f"(n={a['n_week']}/{a['n_weekend']})"
    ]
    if a["monday_score_delta"] is not None:
        lines.append(f"Monday effect: score {a['monday_score_delta']:+.1f} "
                     f"vs the rest of the week"
                     + (f", RHR {a['monday_rhr_delta']:+.1f} bpm"
                        if a["monday_rhr_delta"] is not None else ""))
    return lines


if __name__ == "__main__":
    for line in summary():
        print(line)
