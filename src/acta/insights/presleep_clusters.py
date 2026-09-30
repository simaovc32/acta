"""presleep_clusters — group evenings into recurring "types" and compare how
each type sleeps. Unsupervised discovery (k-means, pure python), complements
the bedtime recommender (which is supervised prediction).

Features per evening: bed_hour, bio_2200, alcohol_prev, workout_prev_day
(mental_evening excluded: too sparse — would drop most nights).

CLI:  python -m acta.insights.presleep_clusters [k]
Used by monthly_report.py: summary()
"""
import random
import statistics
import sys

from acta.insights import features

FEATS = ["bed_hour", "bio_2200", "alcohol_prev", "workout_prev_day"]
OUTCOMES = ["score", "rhr", "asleep_min", "waso_min"]


def _rows():
    feats = features.build()
    rows = []
    for n in sorted(feats):
        f = feats[n]
        if all(f.get(k) is not None for k in FEATS):
            rows.append((n, [float(f[k]) for k in FEATS],
                         {o: f.get(o) for o in OUTCOMES}))
    return rows


def _normalize(vectors):
    cols = list(zip(*vectors))
    mins = [min(c) for c in cols]
    spans = [max(c) - mn or 1.0 for c, mn in zip(cols, mins)]
    return [[(v - mn) / sp for v, mn, sp in zip(vec, mins, spans)] for vec in vectors]


def _kmeans(X, k, iters=60, seed=7):
    rng = random.Random(seed)
    centroids = [list(x) for x in rng.sample(X, k)]
    assign = [0] * len(X)
    for _ in range(iters):
        changed = False
        for i, x in enumerate(X):
            best = min(range(k), key=lambda c: sum((a - b) ** 2 for a, b in zip(x, centroids[c])))
            if best != assign[i]:
                assign[i] = best
                changed = True
        for c in range(k):
            members = [X[i] for i in range(len(X)) if assign[i] == c]
            if members:
                centroids[c] = [statistics.mean(col) for col in zip(*members)]
        if not changed:
            break
    return assign


def clusters(k=3):
    rows = _rows()
    if len(rows) < k * 5:
        return []
    X = _normalize([vec for _, vec, _ in rows])
    assign = _kmeans(X, k)
    out = []
    for c in range(k):
        idx = [i for i, a in enumerate(assign) if a == c]
        if not idx:
            continue
        raw = [rows[i][1] for i in idx]
        outs = [rows[i][2] for i in idx]
        centroid = {f: round(statistics.mean(col), 2)
                    for f, col in zip(FEATS, zip(*raw))}
        outcome = {}
        for o in OUTCOMES:
            vals = [d[o] for d in outs if d[o] is not None]
            outcome[o] = round(statistics.mean(vals), 1) if vals else None
        out.append({"n": len(idx), "centroid": centroid, "outcome": outcome})
    out.sort(key=lambda c: -(c["outcome"]["score"] or 0))
    return out


def _label(c):
    cen = c["centroid"]
    bits = []
    h = cen["bed_hour"] % 24
    bits.append(f"bed ~{int(h):02d}:{int(round((h % 1) * 60)):02d}")
    bits.append(f"bio22h ~{cen['bio_2200']:.0f}")
    if cen["alcohol_prev"] >= 0.5:
        bits.append("with alcohol")
    if cen["workout_prev_day"] >= 0.5:
        bits.append("training day")
    return ", ".join(bits)


def summary(k=3):
    lines = []
    for i, c in enumerate(clusters(k), 1):
        o = c["outcome"]
        lines.append(
            f"Type {i} ({c['n']} nights: {_label(c)}): "
            f"score {o['score']}, RHR {o['rhr']}, "
            f"{(o['asleep_min'] or 0) / 60:.1f}h asleep, WASO {o['waso_min']}m")
    return lines


if __name__ == "__main__":
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    for line in summary(k):
        print(line)
