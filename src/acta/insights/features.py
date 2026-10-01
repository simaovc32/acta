"""features — one row of daily/nightly features per night_of, shared by the
analysis modules (lag_mining, presleep_clusters, social_jetlag, experiments,
monthly_report).

Each feature dict is keyed by night_of (YYYY-MM-DD = wake date). "Evening"
features (bio_2200, mental_evening, alcohol, workout) refer to the evening
BEFORE that wake date — the conditions leading into the night.

Two mental-state features, with different meanings
--------------------------------------------------

  mental_evening — mean of entries from 17:00 on, attributed to the night that
                   FOLLOWS. State going into a night; a predictor of it. Sparse,
                   since most entries are logged in the morning.
  mental_day     — mean of every entry logged on a date, attributed to the night
                   that ended THAT MORNING (night_of == the log date). How the
                   day went after a night; an outcome of it, not a predictor.

Pick by direction: `mental_day` for "what did this night lead to", and
`mental_evening` for "what led into this night". Neither belongs in anything
that computes a score -- they are self-reports, and Acta's whole point is
telling the user what they cannot already feel.

All values may be None; consumers must filter.
"""
import collections
import datetime
import hashlib
import json
import random
import sqlite3
import statistics

from acta import config
from acta.insights.ml.mental_predictor import CAP as MENTAL_CAP

# Legacy 1-10 entries aren't comparable with the 1-5 scale. mental_predictor
# drops them and owns the bounds, so they are imported rather than restated.
from acta.insights.ml.mental_predictor import FLOOR as MENTAL_FLOOR

TZ = config.TZ
# An entry at or after this hour is treated as describing the evening ahead
# rather than the day behind. Named because the number is a judgement, not a
# fact, and the docstring above explains what it costs.
EVENING_FROM_HOUR = 17


def _clock_hour(ts_ms):
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000, tz=TZ)
    return dt.hour + dt.minute / 60


def _virtual_hour(ts_ms):
    """Hour of day where post-midnight bedtimes continue past 24 (01:16 → 25.27)."""
    h = _clock_hour(ts_ms)
    return h + 24 if h < 12 else h


def _load_events():
    try:
        with open(config.EVENTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def effective_n(values):
    """How many observations actually carry information: the count minus the
    size of the single most common value.

    A correlation's n is normally the number of paired rows, which overstates
    the sparse flags in here: a flag that is 0 on almost every night is really
    carried by the few nights it is set.

    Using the modal count rather than a zero-count keeps this honest for
    continuous series too, where the mode repeats a handful of times at most and
    effective_n lands within a couple of the raw n.
    """
    xs = [v for v in values if v is not None]
    if not xs:
        return 0
    modal = collections.Counter(xs).most_common(1)[0][1]
    return len(xs) - modal


def bootstrap_ci(xs, ys, *, B=1000, seed=0, alpha=0.05):
    """Percentile bootstrap confidence interval for a Pearson correlation.

    Answers the question effective_n was only approximating: how much does this
    correlation depend on which particular nights happened to be in the data?
    Resample the pairs with replacement B times, recompute r each time, and take
    the middle 1-alpha of the results.

    Unlike effective_n it doesn't care how a number is written, and doesn't
    punish a value for being common (e.g. a score at its ceiling). A sparse flag
    still fails, for the true reason: resampling swings its correlation wildly
    and the interval spans zero.

    Deterministic by design. `seed` is derived by callers from the pair being
    tested, so a finding cannot blink in and out between two runs of the same
    report.
    """
    pairs = [(float(a), float(b)) for a, b in zip(xs, ys)
             if a is not None and b is not None]
    n = len(pairs)
    if n < 3:
        return None, None
    rng = random.Random(seed)
    out = []
    for _ in range(B):
        s = [pairs[rng.randrange(n)] for _ in range(n)]
        a_ = [q[0] for q in s]
        b_ = [q[1] for q in s]
        if len(set(a_)) < 2 or len(set(b_)) < 2:
            continue
        out.append(statistics.correlation(a_, b_))
    if len(out) < B * 0.5:
        return None, None
    out.sort()
    lo = out[int(len(out) * alpha / 2)]
    hi = out[min(len(out) - 1, int(len(out) * (1 - alpha / 2)))]
    return lo, hi


def ci_excludes_zero(lo, hi) -> bool:
    return lo is not None and hi is not None and lo * hi > 0


def stable_seed(*parts) -> int:
    """A seed that depends only on what is being tested, never on run order."""
    return int(hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:8], 16)


def build():
    """Returns {night_of: {feature: value|None}} for all scored nights."""
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    feats = {}
    for r in con.execute("SELECT * FROM sleep_score ORDER BY night_of"):
        n = r["night_of"]
        mid = None
        if r["bedtime_ts"] and r["waketime_ts"]:
            mid_ms = (r["bedtime_ts"] + r["waketime_ts"]) / 2
            mid = _virtual_hour(mid_ms)
        feats[n] = {
            "score":        r["score"],
            "asleep_min":   r["asleep_min"],
            "waso_min":     r["waso_min"],
            "onset_min":    r["onset_latency_min"],
            "deep_min":     r["deep_min"],
            "rem_min":      r["rem_min"],
            "bed_hour":     _virtual_hour(r["bedtime_ts"]) if r["bedtime_ts"] else None,
            "wake_hour":    _clock_hour(r["waketime_ts"]) if r["waketime_ts"] else None,
            "mid_sleep":    mid,
            "weekend_wake": datetime.date.fromisoformat(n).weekday() >= 5,
        }

    for r in con.execute("SELECT night_of, resting_hr, hrv_mean, hr_dip FROM night_physio"):
        if r["night_of"] in feats:
            feats[r["night_of"]].update(
                rhr=r["resting_hr"], hrv=r["hrv_mean"], hr_dip=r["hr_dip"])

    # biocharge: level at 22:00 of the evening before, and at wake (mean 07-10 max)
    for n, f in feats.items():
        d = datetime.date.fromisoformat(n)
        eve = datetime.datetime.combine(d - datetime.timedelta(days=1),
                                        datetime.time(22, 0), tzinfo=TZ)
        row = con.execute(
            "SELECT level FROM biocharge WHERE minute_ts=?",
            (int(eve.timestamp() * 1000),)).fetchone()
        f["bio_2200"] = row["level"] if row else None

    # user_log: mental state (see the module docstring for why there are two
    # features rather than one), plus the daily ratings.
    for r in con.execute(
            "SELECT ts, kind, value, night_of FROM user_log WHERE value IS NOT NULL"):
        t = datetime.datetime.fromtimestamp(r["ts"] / 1000, tz=TZ)
        if r["kind"] == "mental_state":
            if not MENTAL_FLOOR <= r["value"] <= MENTAL_CAP:
                continue          # legacy 1-10 entry, not on today's scale
            day = t.date().isoformat()
            # Outcome side: every entry counts, against the night that ended
            # that morning. This is the one with enough data to be usable.
            if day in feats:
                feats[day].setdefault("_mental_day", []).append(r["value"])
            # Predictor side: only genuine evening entries, against the night
            # they lead into. Sparse by nature, kept because the direction is
            # the one a "what led into this night" question needs.
            if t.hour >= EVENING_FROM_HOUR:
                nxt = (t.date() + datetime.timedelta(days=1)).isoformat()
                if nxt in feats:
                    feats[nxt].setdefault("_mental_eve", []).append(r["value"])
        # sleep_rating / body_energy are deliberately not read (subjective, no longer logged).

    for f in feats.values():
        ms = f.pop("_mental_eve", None)
        f["mental_evening"] = round(statistics.mean(ms), 1) if ms else None
        md = f.pop("_mental_day", None)
        f["mental_day"] = round(statistics.mean(md), 1) if md else None
        f["mental_day_n"] = len(md) if md else 0

    # workout sessions (day before the night) + volume
    for r in con.execute("SELECT date_iso, volume FROM workout_session"):
        day = r["date_iso"][:10]
        target = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
        if target in feats:
            feats[target]["workout_prev_day"] = 1
            feats[target]["workout_volume"] = r["volume"]
    con.close()

    # events.json: alcohol/coffee/workout on the evening/day before the night
    for e in _load_events():
        try:
            dt = datetime.datetime.strptime(e["datetime"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        target = (dt.date() + datetime.timedelta(days=1)).isoformat()
        if target not in feats:
            continue
        if e.get("type") == "alcohol":
            feats[target]["alcohol_prev"] = 1
        elif e.get("type") == "coffee":
            feats[target]["coffee_prev"] = feats[target].get("coffee_prev", 0) + 1
        elif e.get("type") == "workout":
            feats[target]["workout_prev_day"] = 1

    # PAI: objective training load for the day before the night.
    #
    # `workout_prev_day` above depends on the user having logged something and
    # misses most real sessions. These come from the strap's own zone accounting
    # instead: no logging required, available for every day in history.
    try:
        from acta.engine import pai as _pai
        _gb = _pai.open_gb()
        try:
            _days = _pai.daily(_gb)
        finally:
            _gb.close()
        for day, row in _days.items():
            target = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
            if target not in feats:
                continue
            feats[target]["pai_prev"] = round(row["PAI_TODAY"] or 0, 2)
            feats[target]["pai_high_min_prev"] = row["TIME_HIGH"] or 0
            feats[target]["pai_mod_min_prev"] = row["TIME_MODERATE"] or 0
    except Exception:
        # Gadgetbridge unreadable -> leave the PAI columns as None below, so the
        # miner simply skips those pairs rather than the whole table failing.
        pass

    # fill flag defaults (0 = known absence, since logging exists for these)
    for f in feats.values():
        f.setdefault("alcohol_prev", 0)
        f.setdefault("coffee_prev", 0)
        f.setdefault("workout_prev_day", 0)
        f.setdefault("pai_prev", None)
        f.setdefault("pai_high_min_prev", None)
        f.setdefault("pai_mod_min_prev", None)
        f.setdefault("workout_volume", None)
        f.setdefault("rhr", None)
        f.setdefault("hrv", None)
        f.setdefault("hr_dip", None)

    return feats


if __name__ == "__main__":
    fs = build()
    print(f"{len(fs)} nights")
    last = sorted(fs)[-1]
    print(last, fs[last])
