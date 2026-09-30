"""experiments — A/B your own nights.

An experiment is a tag in user_log (kind='experiment', note=tag, night_of=the
night it applies to). Log it with the `experiment` field of POST /api/log/feeling
in the evening (e.g. "magnesium"); the API maps it to the coming night. After enough tagged nights,
compare tagged vs untagged on objective outcomes.

Control group = untagged nights within ±45 days of the experiment window
(keeps seasonal/routine drift out of the comparison).

CLI:  python -m acta.insights.experiments            # report all tags
Used by monthly_report.py: summary()
"""
import datetime
import sqlite3
import statistics

from acta import config
from acta.insights import features

OUTCOMES = [("score", "score", 1), ("rhr", "RHR", 1),
            ("onset_min", "sleep latency (min)", 1), ("waso_min", "WASO (min)", 1),
            ("hrv", "HRV (ms)", 1)]
MIN_N = 5


def _tags():
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT note, night_of FROM user_log WHERE kind='experiment' "
        "AND note IS NOT NULL AND night_of IS NOT NULL"
    ).fetchall()
    con.close()
    tags = {}
    for note, night in rows:
        tags.setdefault(note.strip().lower(), set()).add(night)
    return tags


def compare(tag_nights, feats):
    """(tagged_stats, control_stats, n_tag, n_ctl) per outcome."""
    dates = [datetime.date.fromisoformat(n) for n in tag_nights]
    lo = min(dates) - datetime.timedelta(days=45)
    hi = max(dates) + datetime.timedelta(days=45)
    control = [n for n in feats
               if n not in tag_nights and lo <= datetime.date.fromisoformat(n) <= hi]
    out = {}
    for key, label, _ in OUTCOMES:
        tv = [feats[n][key] for n in tag_nights if n in feats and feats[n].get(key) is not None]
        cv = [feats[n][key] for n in control if feats[n].get(key) is not None]
        if len(tv) >= 2 and len(cv) >= 5:
            out[label] = (round(statistics.mean(tv), 1), round(statistics.mean(cv), 1),
                          len(tv), len(cv))
    return out


def summary():
    tags = _tags()
    if not tags:
        return ["No experiments logged. To start one, tag the nights you test something "
                "(the `experiment` field when logging how you feel)."]
    feats = features.build()
    lines = []
    for tag, nights in sorted(tags.items()):
        n = len(nights)
        header = f"'{tag}': {n} night(s)"
        if n < MIN_N:
            lines.append(f"{header}: collecting (min. {MIN_N} to compare)")
            continue
        res = compare(nights, feats)
        prelim = " [preliminary]" if n < 10 else ""
        lines.append(f"{header}{prelim}:")
        for label, (t, c, nt, nc) in res.items():
            lines.append(f"    {label}: {t} vs {c} untagged (Δ{t - c:+.1f}, n={nt}/{nc})")
    return lines


if __name__ == "__main__":
    for line in summary():
        print(line)
