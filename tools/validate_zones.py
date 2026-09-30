"""validate_zones — check biocharge's HR zone boundaries against the device's own.

biocharge.HR_ZONE_THRESHOLDS was set by reasoning in May 2026 and has never been
independently checked. HUAMI_PAI_SAMPLE turns out to carry the device's *own*
zone accounting (TIME_LOW / TIME_MODERATE / TIME_HIGH minutes per day), computed
by Huami from the same raw heart rate. That makes it an independent second
opinion on where the zone lines sit -- the same cross-validation logic that made
the calories<->biocharge agreement meaningful, where two signals derived
separately agreed without either being built to match the other.

READ-ONLY DIAGNOSTIC. Prints a report and exits. It deliberately does not tune
anything: same posture as training_response.py, which exists to inform a human
decision and is never wired to adjust a constant on its own. Any change to
HR_ZONE_THRESHOLDS would be a separate, deliberate, forward-only edit -- the
zones below 160 bpm were calibrated in the 2026-05-25 rewrite specifically to
stop social nights draining to the floor, and that fix must not be undone by an
optimiser chasing agreement with Huami.

Two things it reports:
  1. AGREEMENT -- how biocharge's zone minutes compare to the device's, per day.
  2. INFERRED BOUNDARIES -- a sweep for the HR thresholds that best reproduce
     the device's own minute counts, i.e. where Huami's zone lines actually are.
     Huami does not document these anywhere, so this is the only way to know.

CLI:
    python tools/validate_zones.py               # last 30 days with data
    python tools/validate_zones.py --days 90
    python tools/validate_zones.py --infer       # add the boundary sweep (slower)
"""
import datetime
import statistics
import sys

from acta import config
from acta.engine import biocharge as BC
from acta.engine import pai

TZ = config.TZ

# The device's three PAI zones are coarser than biocharge's six. Map biocharge
# zones onto them for comparison: PAI's "low" is everything that registers at
# all but stays easy, "moderate" is aerobic work, "high" is hard work.
# Zone indices come from bisecting HR_ZONE_THRESHOLDS (offsets above RHR).
BIO_TO_PAI = {0: None, 1: "low", 2: "low", 3: "moderate", 4: "high", 5: "high"}


def bio_zone(hr, rhr_bl):
    """biocharge's zone index 0-5 for one HR reading."""
    above = hr - rhr_bl
    z = 0
    for t in BC.HR_ZONE_THRESHOLDS:
        if above > t:
            z += 1
        else:
            break
    return z


def day_minutes(gb, date):
    """biocharge-derived zone minutes for one date, plus the raw HR list."""
    activity = BC.load_activity(gb, date)
    if not activity:
        return None, []
    rhr_bl = BC.resting_hr_baseline(gb, date)
    counts = {"low": 0, "moderate": 0, "high": 0}
    hrs = []
    for m in range(1440):
        hr = activity.get(m, (0, None, 0))[1]
        if hr is None:
            continue
        hrs.append(hr)
        bucket = BIO_TO_PAI[bio_zone(hr, rhr_bl)]
        if bucket:
            counts[bucket] += 1
    return (counts, rhr_bl), hrs


def infer_boundaries(samples):
    """Sweep absolute-bpm cut points that best reproduce the device's counts.

    Reported as absolute bpm rather than offsets-above-RHR because that is what
    the sweep can actually identify: RHR barely moves across the window, so the
    two are not separable here. A large gap between these and biocharge's own
    lines is the finding; the exact numbers are indicative, not authoritative.
    """
    best = None
    for lo in range(70, 121, 2):            # low -> moderate
        for mid in range(lo + 5, 161, 2):   # moderate -> high
            err = 0
            for hrs, dev in samples:
                c = {"low": 0, "moderate": 0, "high": 0}
                for hr in hrs:
                    if hr >= mid:
                        c["high"] += 1
                    elif hr >= lo:
                        c["moderate"] += 1
                    elif hr >= 60:
                        c["low"] += 1
                err += sum(abs(c[k] - dev[k]) for k in c)
            if best is None or err < best[0]:
                best = (err, lo, mid)
    return best


def main():
    args = sys.argv[1:]
    days = int(args[args.index("--days") + 1]) if "--days" in args else 30

    gb = pai.open_gb()
    try:
        pai_days = pai.daily(gb)
        today = datetime.datetime.now(TZ).date().isoformat()
        dates = [d for d in sorted(pai_days) if d < today][-days:]

        rows, samples = [], []
        for d in dates:
            res, hrs = day_minutes(gb, datetime.date.fromisoformat(d))
            if not res or not hrs:
                continue
            counts, rhr_bl = res
            dev = {"low": pai_days[d]["TIME_LOW"] or 0,
                   "moderate": pai_days[d]["TIME_MODERATE"] or 0,
                   "high": pai_days[d]["TIME_HIGH"] or 0}
            rows.append((d, counts, dev, rhr_bl))
            samples.append((hrs, dev))
    finally:
        gb.close()

    if not rows:
        print("no overlapping days with both HR and PAI data")
        return

    print(f"=== biocharge zones vs device zones — {len(rows)} days ===")
    print(f"biocharge thresholds (bpm above RHR): {BC.HR_ZONE_THRESHOLDS}")
    print()
    print("  date         bio_low  dev_low   bio_mod  dev_mod   bio_high  dev_high")
    for d, c, dev, _ in rows[-14:]:
        print(f"  {d}   {c['low']:6d}  {dev['low']:7d}   "
              f"{c['moderate']:6d}  {dev['moderate']:7d}   "
              f"{c['high']:7d}  {dev['high']:8d}")

    print()
    print("=== totals & systematic bias ===")
    for k in ("low", "moderate", "high"):
        b = sum(c[k] for _, c, _, _ in rows)
        v = sum(dev[k] for _, _, dev, _ in rows)
        ratio = (b / v) if v else float("inf")
        diffs = [c[k] - dev[k] for _, c, dev, _ in rows]
        print(f"  {k:9s} biocharge={b:6d}  device={v:6d}  ratio={ratio:5.2f}  "
              f"median daily diff={statistics.median(diffs):+.0f} min")

    rhrs = [r[3] for r in rows]
    print(f"\n  RHR baseline over window: {min(rhrs):.0f}-{max(rhrs):.0f} bpm "
          f"(median {statistics.median(rhrs):.0f})")

    if "--infer" in args:
        print("\n=== inferred device boundaries (sweep) ===")
        err, lo, mid = infer_boundaries(samples)
        mae = err / len(samples)
        rhr_med = statistics.median(rhrs)
        print(f"  best fit: moderate starts ~{lo} bpm, high starts ~{mid} bpm")
        print(f"  (~RHR+{lo - rhr_med:.0f} and ~RHR+{mid - rhr_med:.0f} at median RHR {rhr_med:.0f})")
        print(f"  mean absolute error: {mae:.0f} min/day")
        print(f"  biocharge's equivalent lines: moderate(z3) starts RHR+{BC.HR_ZONE_THRESHOLDS[2]}, "
              f"high(z4) starts RHR+{BC.HR_ZONE_THRESHOLDS[3]}")
        # A large residual means the threshold model itself is wrong, not that
        # the best-fit numbers above are the device's real lines. Measured at
        # 735 min/day on the first run: no (lo, mid) pair reproduces Huami's
        # counts, and restricting to sustained bouts does not close the gap
        # either (bout minutes came out far BELOW the device's totals while
        # all-day elevated minutes came out far above). Huami's zone accounting
        # is therefore doing something not recoverable from per-minute HR alone.
        if mae > 60:
            print(f"\n  ⚠ residual is large ({mae:.0f} min/day) — the 'count minutes past a")
            print("    threshold' model does NOT reproduce the device's numbers, so the")
            print("    fitted values above are not Huami's real boundaries. Treat the")
            print("    low/moderate comparison as INVALID: the two systems are not")
            print("    counting the same thing. The high-zone comparison in the totals")
            print("    table stands on its own and is the meaningful check.")

    print("\nDiagnostic only — no constant is modified by this script.")
    print("Zones below 160 bpm were calibrated 2026-05-25 to stop social nights")
    print("draining to the floor; do not retune them to chase device agreement.")


if __name__ == "__main__":
    main()
