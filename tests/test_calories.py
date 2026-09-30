"""
Unit tests for body.py + calories.py.
Runs entirely against temporary in-memory/temp-file databases — never touches
the production acta.db or Gadgetbridge.db.
"""
import datetime
import sqlite3

from acta.engine import calories as C
from acta.tracking import body
from conftest import check

TZ = C.TZ


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(b))


# ── body.py ───────────────────────────────────────────────────────────────────

def test_body():
    con = sqlite3.connect(":memory:")
    body.ensure(con)

    # Mifflin-St Jeor, male, hand-computed: 10*74 + 6.25*170 - 5*20 + 5
    check("mifflin male matches hand calc",
          approx(body.bmr_kcal_day(74, 170, 20, "male"), 1707.5),
          body.bmr_kcal_day(74, 170, 20, "male"))
    check("mifflin female differs by 166",
          approx(body.bmr_kcal_day(74, 170, 20, "male")
                 - body.bmr_kcal_day(74, 170, 20, "female"), 166.0))
    # Katch-McArdle takes over when body fat is known: 370 + 21.6 * LBM
    check("katch-mcardle used when bodyfat given",
          approx(body.bmr_kcal_day(74, 170, 20, "male", bodyfat_pct=15.0),
                 370.0 + 21.6 * (74 * 0.85)))

    check("age before birthday", body.age_at("2000-01-01", "2025-12-31") == 25)
    check("age on birthday", body.age_at("2000-01-01", "2026-01-01") == 26)

    body.set_config(con, 170, "2000-01-01", "male")
    check("config round-trips", body.get_config(con)["height_cm"] == 170.0)

    d = datetime.datetime(2026, 7, 1, 8, 0, tzinfo=TZ)
    body.log_weight(con, 75.0, d)
    body.log_weight(con, 74.0, d + datetime.timedelta(days=20))

    check("weight_at carries last value forward",
          body.weight_at(con, "2026-07-10")[0] == 75.0)
    check("weight_at picks the newer entry after it exists",
          body.weight_at(con, "2026-07-25")[0] == 74.0)
    check("weight_at reaches backward before the first entry",
          body.weight_at(con, "2026-05-01")[0] == 75.0)

    for bad in (10.0, 400.0):
        try:
            body.log_weight(con, bad, d)
            check(f"rejects implausible weight {bad}", False)
        except ValueError:
            check(f"rejects implausible weight {bad}", True)

    try:
        body.set_config(con, 170, "2000-01-01", "alien")
        check("rejects unknown sex", False)
    except ValueError:
        check("rejects unknown sex", True)

    # Same-instant re-log should update, not duplicate.
    body.log_weight(con, 73.5, d)
    n = con.execute("SELECT COUNT(*) FROM body_metrics").fetchone()[0]
    check("re-logging the same instant upserts", n == 2, f"rows={n}")


def test_protein_target():
    con = sqlite3.connect(":memory:")
    body.ensure(con)

    check("no target before the body is configured",
          body.protein_target(con, "2026-07-10") is None)

    body.set_config(con, 170, "2000-01-01", "male")
    check("no target before any weight is logged",
          body.protein_target(con, "2026-07-10") is None)

    check("default ratio applied on insert",
          body.get_config(con)["protein_g_per_kg"] == body.PROTEIN_G_PER_KG_DEFAULT)

    d = datetime.datetime(2026, 7, 1, 8, 0, tzinfo=TZ)
    body.log_weight(con, 74.0, d)
    t = body.protein_target(con, "2026-07-10")
    # 74.0 * 1.8 = 133.2 -> rounds to the nearest 5 g
    check("target derives from weight x ratio", t["target_g"] == 135,
          t)
    check("exact figure kept alongside the rounded one",
          approx(t["exact_g"], 133.2, 1e-3), t)

    # The point of storing a ratio: the target must follow the weight by itself.
    body.log_weight(con, 80.0, d + datetime.timedelta(days=20))
    t2 = body.protein_target(con, "2026-07-25")
    check("target tracks a weight change with no re-configuration",
          t2["target_g"] == 145, t2)          # 80 * 1.8 = 144 -> 145
    check("earlier date still uses the earlier weight",
          body.protein_target(con, "2026-07-10")["target_g"] == 135)

    body.set_protein_g_per_kg(con, 2.2)
    check("ratio setter changes the derived target",
          body.protein_target(con, "2026-07-25")["target_g"] == 175)  # 80*2.2=176

    for bad in (0.1, 9.0):
        try:
            body.set_protein_g_per_kg(con, bad)
            check(f"rejects implausible ratio {bad}", False)
        except ValueError:
            check(f"rejects implausible ratio {bad}", True)

    # set_config must not silently reset a ratio the user chose.
    body.set_config(con, 172, "2000-01-01", "male")
    check("re-running set_config preserves the protein ratio",
          body.get_config(con)["protein_g_per_kg"] == 2.2,
          body.get_config(con))

    # Migration path: a pre-existing DB without the column must gain it.
    old = sqlite3.connect(":memory:")
    old.execute("CREATE TABLE body_config (id INTEGER PRIMARY KEY CHECK (id=1),"
                " height_cm REAL NOT NULL, birth_date TEXT NOT NULL,"
                " sex TEXT NOT NULL DEFAULT 'male', k REAL NOT NULL DEFAULT 1.0,"
                " updated_at TEXT)")
    old.execute("INSERT INTO body_config (id,height_cm,birth_date,sex,k) "
                "VALUES (1,170,'2000-01-01','male',1.0)")
    body.ensure(old)
    cfg = body.get_config(old)
    check("migration adds the column to an existing row",
          cfg["protein_g_per_kg"] == body.PROTEIN_G_PER_KG_DEFAULT, cfg)
    check("migration preserves the existing row's other values",
          cfg["height_cm"] == 170.0 and cfg["k"] == 1.0, cfg)


# ── calories.py: pure functions ───────────────────────────────────────────────

def test_pure():
    # 1 L O2 -> 5 kcal; 3.5 ml/kg/min at 74 kg = 0.259 L/min = 1.295 kcal/min
    check("vo2 -> kcal conversion",
          approx(C.kcal_min_from_vo2(3.5, 74.0), 3.5 * 74 * 5 / 1000))

    check("vo2max clamped to upper bound",
          C.vo2max_estimate(220, 30, 3.2) <= C.VO2MAX_RANGE[1])
    check("vo2max clamped to lower bound",
          C.vo2max_estimate(120, 90, 3.2) >= C.VO2MAX_RANGE[0])
    check("vo2max always above resting vo2",
          C.vo2max_estimate(150, 100, 3.2) > 3.2)
    # Uth-Sorensen for the real numbers
    check("vo2max ~57 for hrmax 188 / rhr 50",
          56 < C.vo2max_estimate(188, 50, 3.2) < 59,
          C.vo2max_estimate(188, 50, 3.2))

    # The whole point of back-solving resting VO2 from BMR: the HR path must be
    # continuous with the resting floor at zero HR reserve.
    bmr_min = 1707.5 / 1440
    vo2_rest = bmr_min * 1000 / (C.KCAL_PER_L_O2 * 74.0)
    check("resting vo2 back-solve reproduces BMR exactly",
          approx(C.kcal_min_from_vo2(vo2_rest, 74.0), bmr_min))
    check("resting vo2 is below the 3.5 textbook value", vo2_rest < 3.5)


# ── calories.py: masks ────────────────────────────────────────────────────────

def _mem_acta():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE sleep_score (night_of TEXT, bedtime_ts INT, "
                "waketime_ts INT, tz_offset_min INT)")
    body.ensure(con)
    return con


def test_masks():
    C.BC.set_tz_offsets({})          # home zone for these tests
    con = _mem_acta()
    d = datetime.date(2026, 7, 15)

    # Session running 23:30 the previous day -> 07:00 on d.
    bed = datetime.datetime(2026, 7, 14, 23, 30, tzinfo=TZ)
    wake = datetime.datetime(2026, 7, 15, 7, 0, tzinfo=TZ)
    con.execute("INSERT INTO sleep_score VALUES (?,?,?,?)",
                ("2026-07-15", int(bed.timestamp() * 1000),
                 int(wake.timestamp() * 1000), None))

    m = C.sleep_minutes(con, d)
    check("midnight-crossing session marks 00:00 as sleep", m[0] is True)
    check("marks 06:59 as sleep", m[419] is True)
    check("does not mark 07:30 as sleep", m[450] is False)
    check("sleep minute count is the post-midnight portion",
          sum(m) == 421, sum(m))

    # And the pre-midnight portion lands on the previous day, not nowhere.
    mprev = C.sleep_minutes(con, datetime.date(2026, 7, 14))
    check("pre-midnight sleep lands on the previous day",
          sum(mprev) == 30, sum(mprev))

    ev = [(datetime.datetime(2026, 7, 15, 18, 0, tzinfo=TZ), "workout",
           {"duration_min": 30, "kind": "strength"}),
          (datetime.datetime(2026, 7, 15, 9, 0, tzinfo=TZ), "coffee", {})]
    wk = C.workout_kinds(d, ev)
    check("workout window start inclusive", wk[18 * 60] == "strength")
    check("workout window end exclusive", wk[18 * 60 + 30] is None)
    check("workout window length", sum(1 for x in wk if x) == 30)
    check("non-workout events ignored", wk[9 * 60] is None)


# ── calories.py: end-to-end on a synthetic day ────────────────────────────────

class FakeCursor(list):
    """A list that also answers fetchall()/fetchone(), like sqlite3 cursors do."""

    def fetchall(self):
        return list(self)

    def fetchone(self):
        return self[0] if self else None


class FakeGB:
    """Minimal stand-in for the Gadgetbridge connection."""

    def __init__(self, rows, rhr=50, hrmax_rows=((1_750_000_000, 188),)):
        self.rows, self.rhr, self.hrmax_rows = rows, rhr, hrmax_rows

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if "HUAMI_HEART_RATE_MAX_SAMPLE" in s:
            return FakeCursor(self.hrmax_rows)
        if "HUAMI_HEART_RATE_RESTING_SAMPLE" in s:
            return FakeCursor([(self.rhr,)] * 21)
        if "HUAMI_EXTENDED_ACTIVITY_SAMPLE" in s:
            t0, t1 = params[0], params[1]
            return FakeCursor([r for r in self.rows if t0 <= r[0] <= t1])
        return FakeCursor()


def _day(rows, date, acta, now=None, events=()):
    return C.estimate_day(FakeGB(rows), acta, date,
                          now=now or datetime.datetime(
                              date.year, date.month, date.day, 23, 59, tzinfo=TZ),
                          events=list(events))


def test_end_to_end():
    C.BC.set_tz_offsets({})
    con = _mem_acta()
    body.set_config(con, 170, "2000-01-01", "male")
    body.log_weight(con, 74.0, datetime.datetime(2026, 1, 1, 8, 0, tzinfo=TZ))
    d = datetime.date(2026, 7, 15)
    t0, _ = C.BC.day_bounds_s(d)

    # A completely still day: every minute present, HR at rest, no steps.
    still = [(t0 + m * 60, 0, 50, 0) for m in range(1440)]
    r_still = _day(still, d, con)
    mult = r_still["total_kcal"] / r_still["bmr_kcal_day"]
    check("all-still day sits just above BMR", 1.2 <= mult <= 1.35, f"x{mult:.2f}")
    check("all-still day has no active minutes",
          r_still["minutes"]["active"] == 0)
    check("all-still day reports full coverage",
          r_still["coverage_pct"] == 100.0)

    # Same day plus 10k steps spread over 100 minutes of walking.
    walk = list(still)
    for m in range(600, 700):
        walk[m] = (t0 + m * 60, 30, 95, 100)
    r_walk = _day(walk, d, con)
    check("walking raises the total", r_walk["total_kcal"] > r_still["total_kcal"])
    delta = r_walk["total_kcal"] - r_still["total_kcal"]
    check("100 min of walking adds a believable 200-350 kcal",
          200 <= delta <= 350, f"{delta} kcal")

    # HR at 95 with RHR 50 is only 32% of reserve — below the trust floor, so it
    # must be priced by steps, not by the HR path. This is the Keytel trap.
    check("HR 95 alone does not trigger the active path",
          r_walk["minutes"]["active"] == 0, r_walk["minutes"])

    # Genuine cardio: HR 140 (65% reserve) with corroborating movement.
    run = list(still)
    for m in range(600, 630):
        run[m] = (t0 + m * 60, 60, 140, 150)
    r_run = _day(run, d, con)
    check("real cardio triggers the active path",
          r_run["minutes"]["active"] == 30, r_run["minutes"])
    per_min = (r_run["total_kcal"] - r_still["total_kcal"]) / 30
    check("30 min of cardio costs 9-16 kcal/min",
          9 <= per_min <= 16, f"{per_min:.1f} kcal/min")

    # Ambient gate: same high HR, but no movement and no logged workout.
    ambient = list(still)
    for m in range(600, 630):
        ambient[m] = (t0 + m * 60, 2, 140, 0)
    r_amb = _day(ambient, d, con)
    check("ambient gate blocks HR-only spikes",
          r_amb["minutes"]["active"] == 0, r_amb["minutes"])
    check("ambient day costs far less than real cardio",
          r_amb["total_kcal"] < r_run["total_kcal"] - 200,
          f"{r_amb['total_kcal']} vs {r_run['total_kcal']}")

    # A logged workout disables the gate (declared beats inferred).
    ev = [(datetime.datetime(2026, 7, 15, 10, 0, tzinfo=TZ), "workout",
           {"duration_min": 30, "kind": "cardio"})]
    r_logged = _day(ambient, d, con, events=ev)
    check("logged workout overrides the ambient gate",
          r_logged["minutes"]["active"] == 30, r_logged["minutes"])

    # Strength is priced by MET, not by HR, so a hard-breathing lift and a calm
    # one cost the same.
    ev_s = [(datetime.datetime(2026, 7, 15, 10, 0, tzinfo=TZ), "workout",
             {"duration_min": 30, "kind": "strength"})]
    calm = list(still)
    hard = list(still)
    for m in range(600, 630):
        hard[m] = (t0 + m * 60, 60, 150, 20)
    a = _day(calm, d, con, events=ev_s)["total_kcal"]
    b = _day(hard, d, con, events=ev_s)["total_kcal"]
    check("strength cost is HR-independent", a == b, f"{a} vs {b}")
    check("strength session costs 150-250 kcal for 30 min",
          150 <= a - r_still["total_kcal"] + 0 <= 260,
          f"{a - r_still['total_kcal']} kcal")

    # Sentinels must not be read as a 255 bpm heart rate.
    sent = [(t0 + m * 60, 0, 255 if m < 720 else 50, 0) for m in range(1440)]
    r_sent = _day(sent, d, con)
    check("HR=255 sentinel is not treated as a real reading",
          r_sent["total_kcal"] < r_still["total_kcal"] * 1.15,
          f"{r_sent['total_kcal']} vs {r_still['total_kcal']}")
    check("sentinel minutes are excluded from coverage",
          r_sent["coverage_pct"] == 50.0, r_sent["coverage_pct"])

    # Missing rows entirely -> imputed, and clamped so they can't explode.
    gappy = [r for r in still if not (400 <= (r[0] - t0) // 60 < 700)]
    r_gap = _day(gappy, d, con)
    check("missing rows are counted as imputed",
          r_gap["minutes"]["imputed"] == 300, r_gap["minutes"])
    check("imputation stays within the clamp",
          abs(r_gap["total_kcal"] - r_still["total_kcal"]) < 250,
          f"{r_gap['total_kcal']} vs {r_still['total_kcal']}")

    # Partial day: "so far today" must not price minutes that haven't happened.
    noon = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=TZ)
    r_partial = _day(still, d, con, now=noon)
    check("partial day counts only elapsed minutes",
          r_partial["elapsed_min"] == 721, r_partial["elapsed_min"])
    check("partial day total is roughly half a full day",
          0.45 < r_partial["total_kcal"] / r_still["total_kcal"] < 0.55,
          r_partial["total_kcal"] / r_still["total_kcal"])

    # Monotonicity in the two inputs that should drive the number.
    heavier = sqlite3.connect(":memory:")
    heavier.execute("CREATE TABLE sleep_score (night_of TEXT, bedtime_ts INT, "
                    "waketime_ts INT, tz_offset_min INT)")
    body.ensure(heavier)
    body.set_config(heavier, 170, "2000-01-01", "male")
    body.log_weight(heavier, 90.0, datetime.datetime(2026, 1, 1, 8, 0, tzinfo=TZ))
    check("heavier body burns more at rest",
          _day(still, d, heavier)["total_kcal"] > r_still["total_kcal"])

    # k is a clean scalar on the whole total.
    body.set_k(con, 1.10)
    r_k = _day(still, d, con)
    check("k scales the total linearly",
          abs(r_k["total_kcal"] - r_still["total_kcal"] * 1.10) <= 2,
          f"{r_k['total_kcal']} vs {r_still['total_kcal'] * 1.10:.0f}")
    body.set_k(con, 1.0)


def test_hrmax_fallback():
    C.BC.set_tz_offsets({})
    d = datetime.date(2026, 7, 15)
    junk = FakeGB([], hrmax_rows=((1_750_000_000, 73), (1_750_000_000, 133)))
    hr, src = C.hr_max_estimate(junk, d, 20)
    check("junk max-HR rows are ignored", src == "tanaka", src)
    check("tanaka fallback value", abs(hr - (208 - 0.7 * 20)) < 0.01)

    real = FakeGB([], hrmax_rows=((int(datetime.datetime(2026, 7, 1, tzinfo=TZ)
                                       .timestamp()), 186),))
    hr, src = C.hr_max_estimate(real, d, 20)
    check("measured max-HR preferred when plausible", src == "measured" and hr == 186)

    ms = FakeGB([], hrmax_rows=((int(datetime.datetime(2026, 7, 1, tzinfo=TZ)
                                     .timestamp() * 1000), 186),))
    hr, src = C.hr_max_estimate(ms, d, 20)
    check("millisecond timestamps in the max-HR table are handled",
          src == "measured" and hr == 186, f"{src} {hr}")
