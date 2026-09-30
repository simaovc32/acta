"""illness_fingerprint — learn what YOUR body does before it gets sick.

Every time the recovery alarm fires (checked on each ingest), record() snapshots
the 7 days of features leading up to it. Episodes < 14 days apart merge into
one. Once ≥2 episodes exist, report() shows the average pre-alarm trajectory —
the personal prodrome — so future alerts can reference it (and eventually fire
earlier on the pattern, not just the threshold).

Storage: episodes table in acta.db (JSON snapshot per episode).
CLI:  python -m acta.insights.illness_fingerprint       # report
"""
import datetime
import json
import sqlite3
import statistics

from acta import config
from acta.insights import features

KEYS = ["rhr", "hrv", "hr_dip", "score", "asleep_min", "waso_min"]
MERGE_DAYS = 14

SCHEMA = """
CREATE TABLE IF NOT EXISTS illness_episode (
  fired_on  TEXT PRIMARY KEY,   -- date the alarm fired
  snapshot  TEXT NOT NULL,      -- JSON: [{night_of, rhr, hrv, ...} x 7]
  note      TEXT
)
"""


def record(fired_on=None, note=None):
    """Snapshot the 7 nights before `fired_on` (default today). Merges with a
    recent episode instead of creating a near-duplicate. Returns True if new.

    `note` tags the episode's trigger so confirmed vs. suspected can be told
    apart later: e.g. "acute" for a fast 2-night nosedive vs. "" for the
    sustained 3-of-3 hard alarm. Not ground truth (no illness is logged yet),
    just provenance."""
    fired_on = fired_on or datetime.date.today().isoformat()
    con = sqlite3.connect(config.ACTA_DB, timeout=30)
    con.execute(SCHEMA)
    recent = con.execute(
        "SELECT fired_on FROM illness_episode WHERE fired_on >= ? ",
        ((datetime.date.fromisoformat(fired_on)
          - datetime.timedelta(days=MERGE_DAYS)).isoformat(),)
    ).fetchone()
    if recent:
        con.close()
        return False  # same episode still running

    feats = features.build()
    end = datetime.date.fromisoformat(fired_on)
    snap = []
    for i in range(7, 0, -1):
        n = (end - datetime.timedelta(days=i - 1)).isoformat()
        f = feats.get(n, {})
        snap.append({"night_of": n, **{k: f.get(k) for k in KEYS}})
    con.execute("INSERT INTO illness_episode(fired_on, snapshot, note) VALUES(?,?,?)",
                (fired_on, json.dumps(snap), note))
    con.commit()
    con.close()
    return True


def report():
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT fired_on, snapshot FROM illness_episode ORDER BY fired_on"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
    if not rows:
        return ["No episodes recorded (a good sign). The fingerprint builds itself "
                "whenever the recovery alarm fires."]
    lines = [f"{len(rows)} episode(s): " + ", ".join(r[0] for r in rows)]
    if len(rows) >= 2:
        # average trajectory day -7 → -1 across episodes
        by_day = {k: [[] for _ in range(7)] for k in KEYS}
        for _, snap_json in rows:
            for i, day in enumerate(json.loads(snap_json)):
                for k in KEYS:
                    if day.get(k) is not None:
                        by_day[k][i].append(day[k])
        lines.append("Mean pre-alarm trajectory (D-7 → D-1):")
        for k in KEYS:
            vals = [round(statistics.mean(v), 1) if v else None for v in by_day[k]]
            lines.append(f"    {k}: {vals}")
    return lines


if __name__ == "__main__":
    for line in report():
        print(line)
