"""Unit tests for engine.fitness (estimated VO2max and heart-rate recovery)."""
import datetime
import sqlite3

from acta.engine import fitness as F
from acta.tracking import body
from conftest import check


def approx(a, b, tol=0.1):
    return a is not None and abs(a - b) <= tol


# ── an in-memory acta.db with just the tables fitness reads ────────────────────

def make_db():
    con = sqlite3.connect(":memory:")
    con.executescript("""
      CREATE TABLE sleep_score (night_of TEXT PRIMARY KEY, score REAL, tz_transition INTEGER);
      CREATE TABLE night_physio (night_of TEXT PRIMARY KEY, resting_hr INTEGER,
        min_sleep_hr INTEGER, hr_dip INTEGER, hrv_mean REAL, computed_at TEXT);
      CREATE TABLE daily_energy (date TEXT PRIMARY KEY, strength_min INTEGER);
      CREATE TABLE pai_detection (id INTEGER PRIMARY KEY, date TEXT, status TEXT,
        kind TEXT, start_min INTEGER, work_min INTEGER, duration_min INTEGER);
    """)
    body.ensure(con)
    body.set_config(con, 170.0, "2000-01-01", "male")
    body.log_weight(con, 74.0,
                    datetime.datetime(2026, 8, 1, 8, 0, tzinfo=F.TZ))
    con.commit()
    return con


class _Rows(list):
    def fetchone(self):
        return self[0] if self else None


class FakeGB:
    """Stands in for the Gadgetbridge read connection."""
    def __init__(self, hrmax_rows=None, pai_rows=None, hr_samples=None):
        # hrmax_rows: [(ts_ms, heart_rate)]  pai_rows: [(ts_ms, mod, high)]
        # hr_samples: [(ts_s, heart_rate)] for HUAMI_EXTENDED_ACTIVITY_SAMPLE (seconds)
        self.hrmax_rows = hrmax_rows or []
        self.pai_rows = pai_rows or []
        self.hr_samples = hr_samples or []

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if "HUAMI_HEART_RATE_MAX_SAMPLE" in s:
            lo, hi, thr = params
            vals = [hr for ts, hr in self.hrmax_rows if lo <= ts <= hi and hr >= thr]
            return _Rows([(max(vals) if vals else None,)])
        if "HUAMI_PAI_SAMPLE" in s:
            lo, hi = params
            return _Rows([(ts, m, h) for ts, m, h in self.pai_rows if lo <= ts < hi])
        if "HUAMI_EXTENDED_ACTIVITY_SAMPLE" in s and "MAX(HEART_RATE)" in s:
            lo, hi = params
            vals = [hr for ts, hr in self.hr_samples if lo <= ts <= hi]
            return _Rows([(max(vals) if vals else None,)])
        if "HUAMI_EXTENDED_ACTIVITY_SAMPLE" in s:            # nearest-sample lookup
            lo, hi, t0 = params
            near = [(abs(ts - t0), hr) for ts, hr in self.hr_samples if lo <= ts <= hi]
            return _Rows([(min(near)[1],)] if near else [])
        raise AssertionError("unexpected query: " + s)


def nights(con, start, hrs, tz_flags=None):
    """Seed `hrs` consecutive nights of resting_hr ending on `start`... backwards."""
    d = datetime.date.fromisoformat(start)
    for i, hr in enumerate(hrs):
        nd = (d - datetime.timedelta(days=i)).isoformat()
        tz = 0 if not tz_flags else tz_flags[i]
        con.execute("INSERT INTO sleep_score VALUES (?,?,?)", (nd, 85.0, tz))
        con.execute("INSERT INTO night_physio VALUES (?,?,?,?,?,?)",
                    (nd, hr, hr - 4, 4, 100.0, "x"))
    con.commit()


def test_estimator_math():
    """Estimator equations."""
    check("Uth: 15.3 * 195 / 46", approx(F.uth(195, 46), 15.3 * 195 / 46, 0.01))
    check("Uth rises as RHR falls", F.uth(195, 44) > F.uth(195, 50))
    # Nes male, age 20, PA 7.5, waist 85, rhr 46
    nes_expected = 100.27 - 0.296*20 + 0.226*7.5 - 0.369*85 - 0.155*46
    check("Nes male form", approx(F.nes(20, "male", 85, 46, 7.5), nes_expected, 0.01))
    check("Nes rises with more activity", F.nes(20, "male", 85, 46, 22.5) > F.nes(20, "male", 85, 46, 0))
    check("Nes rises with a smaller waist", F.nes(20, "male", 78, 46, 7.5) > F.nes(20, "male", 88, 46, 7.5))


def test_pa_index_mapping():
    """PA index mapping."""
    check("0 min/wk -> PA 0", F._vigeq_to_pa(0) == 0.0)
    check("75 min/wk -> PA 7.5", approx(F._vigeq_to_pa(75), 7.5, 0.01))
    check("225 min/wk -> PA 22.5", approx(F._vigeq_to_pa(225), 22.5, 0.01))
    check("500 min/wk clamps at 45", F._vigeq_to_pa(500) == 45.0)
    check("monotone", F._vigeq_to_pa(50) < F._vigeq_to_pa(150) < F._vigeq_to_pa(300))


def test_reconciliation():
    """Reconciliation."""
    c, lo, hi, conf, why = F._reconcile(60.0, 60.0, 0)
    check("tight agreement -> high confidence", conf == "high" and why is None)
    c, lo, hi, conf, why = F._reconcile(65.0, 55.0, 0)
    check("10-pt gap -> moderate + why_spread", conf == "moderate" and why and "65" in why)
    c, lo, hi, conf, why = F._reconcile(70.0, 50.0, 0)
    check("20-pt gap -> low", conf == "low")
    c, lo, hi, conf, why = F._reconcile(60.0, 60.0, 9)
    check("stale RHR -> stale confidence", conf == "stale")
    c, lo, hi, conf, why = F._reconcile(60.0, 58.0, 0)
    check("central is 0.55A + 0.45B", approx(c, 0.55*60 + 0.45*58, 0.05))
    c, *_ = F._reconcile(62.0, None, 0)
    check("one estimator missing still returns a value", approx(c, 62.0))


def test_percentile():
    """Percentile."""
    p_low = F._percentile(40.0, 20, "male")
    p_mid = F._percentile(51.4, 20, "male")
    p_high = F._percentile(70.0, 20, "male")
    check("below 10th clamps", p_low == 10.0)
    check("~51 -> ~50th for a 20M", approx(p_mid, 50.0, 2.0))
    check("percentile monotone", p_low < p_mid < p_high)
    check("older age reads a higher percentile for the same VO2max",
          F._percentile(45.0, 55, "male") > F._percentile(45.0, 25, "male"))


def test_hrmax_sourcing():
    """HRmax sourcing."""
    d = "2026-09-04"
    end = datetime.datetime.fromisoformat(d + "T23:59:59").timestamp() * 1000
    gb = FakeGB(hrmax_rows=[(end - 1e9, 195), (end - 2e9, 73), (end - 3e9, 133)])
    hm, meas = F._hrmax(gb, d, 20.1)
    check("junk (<150) rows ignored, 195 kept", hm == 195 and meas)
    gb2 = FakeGB(hrmax_rows=[(end - 1e9, 240)])
    hm2, _ = F._hrmax(gb2, d, 20.1)
    check("implausible 240 clamped to age-pred+12 (~210)", hm2 <= 211)
    gb3 = FakeGB(hrmax_rows=[])
    hm3, meas3 = F._hrmax(gb3, d, 20.1)
    check("no samples -> age-predicted fallback, flagged not-measured",
          not meas3 and 190 <= hm3 <= 200)


def test_rhr_median_exclusions():
    """RHR median (exclusions)."""
    con = make_db()
    nights(con, "2026-09-04", [46]*14 + [47]*14)
    check("median of a steady series", approx(F._rhr_median(con, "2026-09-04"), 46.0, 0.6))
    con2 = make_db()
    nights(con2, "2026-09-04", [46]*13 + [61] + [46]*14)   # one illness spike
    check("median absorbs a single spike", approx(F._rhr_median(con2, "2026-09-04"), 46.0, 0.6))
    con3 = make_db()
    nights(con3, "2026-09-04", [46]*27, tz_flags=[0]*13 + [1] + [0]*13)
    # night 13 has resting_hr 46 too but tz_transition=1 — still fine, just checks it runs
    check("tz-transition nights excluded without error",
          F._rhr_median(con3, "2026-09-04") is not None)
    con4 = make_db()
    nights(con4, "2026-09-04", [46]*4)   # too few
    check("thin history -> None", F._rhr_median(con4, "2026-09-04") is None)


def test_pa_floor_from_confirmed_detections():
    """PA floor from confirmed detections."""
    con = make_db()
    nights(con, "2026-09-04", [46]*28)
    gb = FakeGB(pai_rows=[])   # no zone minutes at all
    pa0, _, tot0 = F._pa_index(con, gb, "2026-09-04")
    check("no activity data -> PA 0", pa0 == 0.0)
    for i in range(3):
        con.execute("INSERT INTO pai_detection (id,date,status,kind,start_min,work_min,duration_min) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (i, f"2026-08-2{i}", "confirmed", "run", 1000, 40, 50))
    con.commit()
    pa1, _, tot1 = F._pa_index(con, gb, "2026-09-04")
    check(">=2 confirmed hard sessions put a floor under PA", pa1 > 0 and tot1 >= 150)


def test_hrr_companion_metric():
    """HRR (companion metric)."""
    check("rating bands", F.hrr_rating(8) == "sluggish" and F.hrr_rating(20) == "okay"
          and F.hrr_rating(31) == "good" and F.hrr_rating(45) == "athletic")
    check("None rating", F.hrr_rating(None) is None)
    con = make_db()
    day, mid = "2026-08-20", int(datetime.datetime(2026, 8, 20, tzinfo=F.TZ).timestamp())
    con.execute("INSERT INTO pai_detection (id,date,status,kind,start_min,work_min,duration_min) "
                "VALUES (1,?,?,?,?,?,?)", (day, "confirmed", "run", 600, 30, 30))
    con.commit()
    end = 630
    # working HR ~175 in the last 2 min, recovers to ~140 at +1min -> HRR ~35
    hs = [(mid + (end - 2) * 60 + k, 175) for k in range(0, 120, 15)] + \
         [(mid + (end + 1) * 60 + k, 140) for k in range(-30, 30, 15)] + \
         [(mid + (end + 2) * 60 + k, 128) for k in range(-30, 30, 15)]
    gb = FakeGB(hr_samples=hs)
    hrr1, hrr2, n = F._hrr(con, gb, "2026-09-04")
    check("1-min HRR computed from a confirmed run", hrr1 is not None and 30 <= hrr1 <= 40 and n == 1)
    check("2-min HRR is a bigger cumulative drop", hrr2 is not None and hrr2 > hrr1)
    con2 = make_db()   # no cardio detections
    check("no sessions -> (None, None, 0)", F._hrr(con2, FakeGB(), "2026-09-04") == (None, None, 0))


def test_end_to_end_estimate_idempotency():
    """End-to-end estimate + idempotency."""
    con = make_db()
    nights(con, "2026-09-04", [46]*28)
    d = "2026-09-04"
    end = datetime.datetime.fromisoformat(d + "T23:59:59").timestamp() * 1000
    gb = FakeGB(hrmax_rows=[(end - 1e9, 195)],
                pai_rows=[(int(end - k*86400e3), 20, 15) for k in range(28)])
    r = F.estimate(con, gb, d)
    check("estimate returns a plausible central", r and 45 <= r["vo2max_central"] <= 80)
    check("band brackets central", r["vo2max_lo"] <= r["vo2max_central"] <= r["vo2max_hi"])
    check("percentile present", r["percentile"] is not None)
    check("waist flagged estimated (none logged)", r["waist_estimated"] == 1)

    F.ensure(con)
    F._upsert(con, r)
    F._upsert(con, r)   # again — must not duplicate or error
    n = con.execute("SELECT COUNT(*) FROM fitness_estimate").fetchone()[0]
    check("re-upsert is idempotent (1 row)", n == 1)

    # no config -> None, no crash
    con5 = sqlite3.connect(":memory:")
    con5.executescript("CREATE TABLE sleep_score(night_of TEXT); "
                       "CREATE TABLE night_physio(night_of TEXT, resting_hr INT);")
    body.ensure(con5)
    check("no body_config -> estimate() returns None", F.estimate(con5, gb, d) is None)
