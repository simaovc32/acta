"""training_response — what does each workout cost you overnight?

For every workout_session, compares the NEXT night's RHR/HRV against the
personal baseline at that time. Aggregated by session type (first word of the
title) once enough data exists.

CLI:  python -m acta.engine.training_response          # per-session + aggregate report
Also used by sensors.py: yesterday_impact() → digest warning when yesterday's
workout left RHR clearly elevated this morning.
"""
import datetime
import sqlite3
import statistics

from acta import config

TZ = config.TZ

IMPACT_BPM = 3  # digest warning threshold: next-morning RHR >= baseline + this


def _sessions(con):
    return con.execute(
        "SELECT date_iso, title, volume, avg_rpe, duration_sec "
        "FROM workout_session ORDER BY date_iso"
    ).fetchall()


def _srpe(rpe, duration_sec):
    """Session-RPE load = RPE × minutes. None when the session had no RPE."""
    if rpe is None:
        return None
    return rpe * (duration_sec or 0) / 60.0


def _pearson(xs, ys):
    """Pearson r over paired samples, or None if degenerate."""
    n = len(xs)
    if n < 3:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    if dx == 0 or dy == 0:
        return None
    return num / (dx ** 0.5 * dy ** 0.5)


def _rhr_baseline(con, night_of):
    hist = [r[0] for r in con.execute(
        "SELECT resting_hr FROM night_physio WHERE night_of < ? "
        "AND resting_hr IS NOT NULL ORDER BY night_of DESC LIMIT 30", (night_of,))]
    return statistics.median(hist) if len(hist) >= 7 else None


def session_impacts():
    """[(day, title, volume, rpe, srpe|None, rhr_delta|None, hrv_pct|None)] —
    deltas vs baseline for the night FOLLOWING each session."""
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    out = []
    for date_iso, title, volume, rpe, dur_sec in _sessions(con):
        day = date_iso[:10]
        night = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
        row = con.execute(
            "SELECT resting_hr, hrv_mean FROM night_physio WHERE night_of=?", (night,)
        ).fetchone()
        rhr_delta = hrv_pct = None
        if row and row[0] is not None:
            base = _rhr_baseline(con, night)
            if base:
                rhr_delta = row[0] - base
        if row and row[1] is not None:
            hrv_hist = [r[0] for r in con.execute(
                "SELECT hrv_mean FROM night_physio WHERE night_of < ? "
                "AND hrv_mean IS NOT NULL ORDER BY night_of DESC LIMIT 30", (night,))]
            if len(hrv_hist) >= 7:
                hrv_pct = (row[1] / statistics.median(hrv_hist) - 1) * 100
        out.append((day, title, volume, rpe, _srpe(rpe, dur_sec), rhr_delta, hrv_pct))
    con.close()
    return out


def yesterday_impact(now=None):
    """Digest line when yesterday's workout left RHR clearly elevated, else None."""
    now = now or datetime.datetime.now(TZ)
    yesterday = (now.date() - datetime.timedelta(days=1)).isoformat()
    today = now.date().isoformat()

    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    sess = con.execute(
        "SELECT title FROM workout_session WHERE date_iso LIKE ?", (f"{yesterday}%",)
    ).fetchone()
    if not sess:
        con.close()
        return None
    row = con.execute(
        "SELECT resting_hr FROM night_physio WHERE night_of=?", (today,)
    ).fetchone()
    base = _rhr_baseline(con, today)
    con.close()
    if not row or row[0] is None or not base:
        return None
    delta = row[0] - base
    if delta >= IMPACT_BPM:
        return (f"💪 Yesterday's session ({sess[0]}) left a mark: resting HR +{delta:.0f} "
                f"vs baseline. Recovery is incomplete; adjust today's intensity.")
    return None


def report():
    rows = session_impacts()
    if not rows:
        print("No sessions logged.")
        return
    print(f"{'day':<12} {'session':<32} {'vol':>6} {'RPE':>4} {'sRPE':>6} {'ΔRHR':>6} {'ΔHRV%':>7}")
    for day, title, vol, rpe, srpe, dr, dh in rows:
        print(f"{day:<12} {title[:32]:<32} {vol or 0:>6.0f} {rpe or 0:>4.1f} "
              f"{'—' if srpe is None else f'{srpe:.0f}':>6} "
              f"{'—' if dr is None else f'{dr:+.0f}':>6} "
              f"{'—' if dh is None else f'{dh:+.0f}':>7}")

    # Aggregate by session type once there's enough signal
    by_type = {}
    for _day, title, _vol, _rpe, _srpe, dr, _dh in rows:
        if dr is not None:
            by_type.setdefault(title.split()[0], []).append(dr)
    agg = {k: v for k, v in by_type.items() if len(v) >= 3}
    if agg:
        print("\nMean cost by session type (ΔRHR the next night):")
        for k, v in sorted(agg.items(), key=lambda kv: -statistics.mean(kv[1])):
            print(f"  {k:<16} {statistics.mean(v):+.1f} bpm  (n={len(v)})")
    else:
        print("\n(per-type averages appear once a type has ≥3 sessions)")

    # Does session-RPE load predict next-morning recovery? This is the check
    # that decides whether sRPE deserves its weight in readiness. Clues, not
    # proof, until n is healthy.
    pairs_rhr = [(s, dr) for _, _, _, _, s, dr, _ in rows if s is not None and dr is not None]
    pairs_hrv = [(s, dh) for _, _, _, _, s, _, dh in rows if s is not None and dh is not None]
    r_rhr = _pearson([p[0] for p in pairs_rhr], [p[1] for p in pairs_rhr])
    r_hrv = _pearson([p[0] for p in pairs_hrv], [p[1] for p in pairs_hrv])
    print("\nsRPE → recovery (validates the weight of sRPE in readiness):")
    if r_rhr is not None:
        print(f"  sRPE vs ΔRHR next night : r={r_rhr:+.2f}  (n={len(pairs_rhr)})"
              f"{'  ← more load raises resting HR' if r_rhr >= 0.3 else ''}")
    if r_hrv is not None:
        print(f"  sRPE vs ΔHRV% next night: r={r_hrv:+.2f}  (n={len(pairs_hrv)})"
              f"{'  ← more load lowers HRV' if r_hrv <= -0.3 else ''}")
    if r_rhr is None and r_hrv is None:
        print("  (appears once ≥3 sessions have an RPE and a following night)")


if __name__ == "__main__":
    report()
