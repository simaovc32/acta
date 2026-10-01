"""
Body constants + weight log — shared by api/routers/body.py (writes) and calories.py (reads).

Weight is stored as a *time series*, never a constant, for two reasons:
  1. Backfilling calories over history needs the weight in effect on that date,
     not today's weight.
  2. The calibration loop (fitting `k` against measured weight trend) needs
     the same series.

Height/birth date/sex/k live in a singleton `body_config` row.

The protein target is stored as **g per kg of bodyweight**, not as an absolute
gram figure, for the same reason weight is a series: an absolute target silently
goes stale the moment the weight it was derived from changes. Storing the ratio
means the target re-derives itself on every read, and the one number that is
genuinely a preference (how aggressive the intake goal is) stays the only knob.

CLI:
    python -m acta.tracking.body                     # show config + recent weights
    python -m acta.tracking.body --weight 74.0       # log a weight for now
    python -m acta.tracking.body --protein 2.0       # set the protein target, g per kg
"""
import argparse
import datetime
import sqlite3

from acta import config

TZ = config.TZ
SCHEMA = """
CREATE TABLE IF NOT EXISTS body_config (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  height_cm   REAL NOT NULL,
  birth_date  TEXT NOT NULL,              -- YYYY-MM-DD
  sex         TEXT NOT NULL DEFAULT 'male',
  k           REAL NOT NULL DEFAULT 1.0,  -- calibration factor (see calories.py)
  protein_g_per_kg REAL NOT NULL DEFAULT 1.8,  -- target ratio; see protein_target()
  updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS body_metrics (
  ts           TEXT PRIMARY KEY,   -- ISO8601 with offset, the instant measured
  measured_on  TEXT NOT NULL,      -- YYYY-MM-DD, local day of that instant
  weight_kg    REAL,
  bodyfat_pct  REAL,
  waist_cm     REAL,               -- navel-level, for the VO2max estimate (fitness.py)
  note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_body_metrics_day ON body_metrics(measured_on);
"""

# Sleeping metabolic rate sits a few percent under BMR; the classic 0.85 figure
# describes deep sleep only, not a whole night including REM and WASO.
SEX_CONST = {"male": 5.0, "female": -161.0}   # Mifflin-St Jeor tail term

# Protein target, grams per kg of bodyweight. 1.6-2.2 g/kg is the range the
# resistance-training literature converges on; 1.8 sits mid-band, so it is a
# sensible default for someone lifting seriously without being a bulking figure.
# The validation bounds are deliberately wider than that band — this is a
# preference, and refusing a value a user deliberately chose would be wrong.
PROTEIN_G_PER_KG_DEFAULT = 1.8
PROTEIN_G_PER_KG_MIN     = 0.8
PROTEIN_G_PER_KG_MAX     = 3.5
# The target is a goal derived from an estimate, not a measurement. Rounding to
# 5 g keeps it from reading as false precision (74.0 kg x 1.8 = 133.2 g).
PROTEIN_TARGET_ROUND_G   = 5


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # Additive-only migration, same pattern as nutrition.ensure()/ingest.migrate().
    have = {r[1] for r in con.execute("PRAGMA table_info(body_config)")}
    if "protein_g_per_kg" not in have:
        con.execute("ALTER TABLE body_config ADD COLUMN protein_g_per_kg "
                    f"REAL NOT NULL DEFAULT {PROTEIN_G_PER_KG_DEFAULT}")
    if "run_stride_m" not in have:
        # Running step length, metres. NULL → derive from height in run_stride().
        # Feeds the step-based distance/pace estimate in the workout run review.
        con.execute("ALTER TABLE body_config ADD COLUMN run_stride_m REAL")
    have_m = {r[1] for r in con.execute("PRAGMA table_info(body_metrics)")}
    if "waist_cm" not in have_m:
        con.execute("ALTER TABLE body_metrics ADD COLUMN waist_cm REAL")


# ── config ────────────────────────────────────────────────────────────────────

def get_config(con: sqlite3.Connection) -> dict | None:
    row = con.execute(
        "SELECT height_cm, birth_date, sex, k, protein_g_per_kg, run_stride_m "
        "FROM body_config WHERE id = 1"
    ).fetchone()
    if not row:
        return None
    return {"height_cm": row[0], "birth_date": row[1], "sex": row[2], "k": row[3],
            "protein_g_per_kg": row[4], "run_stride_m": row[5]}


# Running step length ≈ 0.75 × height is a rough population mean; real running
# stride varies with pace (~0.7–1.0 × height). Used only for the ±15 %
# step-based distance/pace estimate — replace with a measured value when known.
RUN_STRIDE_HEIGHT_FACTOR = 0.75


def run_stride(con: sqlite3.Connection) -> float | None:
    """Metres per running step. Configured value, else derived from height."""
    cfg = get_config(con)
    if not cfg:
        return None
    if cfg.get("run_stride_m"):
        return float(cfg["run_stride_m"])
    if cfg.get("height_cm"):
        return round(cfg["height_cm"] / 100.0 * RUN_STRIDE_HEIGHT_FACTOR, 3)
    return None


def set_config(con, height_cm, birth_date, sex="male", k=1.0) -> None:
    if sex not in SEX_CONST:
        raise ValueError(f"sex must be one of {sorted(SEX_CONST)}")
    datetime.date.fromisoformat(birth_date)   # validate
    con.execute(
        "INSERT INTO body_config (id, height_cm, birth_date, sex, k, updated_at) "
        "VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "height_cm=excluded.height_cm, birth_date=excluded.birth_date, "
        "sex=excluded.sex, k=excluded.k, updated_at=excluded.updated_at",
        (float(height_cm), birth_date, sex, float(k),
         datetime.datetime.now(TZ).isoformat(timespec="seconds")),
    )


def set_k(con: sqlite3.Connection, k: float) -> None:
    """Calibration factor. Left at 1.0 until there is weight+intake data to fit."""
    con.execute("UPDATE body_config SET k = ?, updated_at = ? WHERE id = 1",
                (float(k), datetime.datetime.now(TZ).isoformat(timespec="seconds")))


def set_protein_g_per_kg(con: sqlite3.Connection, g_per_kg: float) -> None:
    """Set the protein target ratio. The absolute target re-derives from weight."""
    g = float(g_per_kg)
    if not (PROTEIN_G_PER_KG_MIN <= g <= PROTEIN_G_PER_KG_MAX):
        raise ValueError(f"protein_g_per_kg out of range "
                         f"({PROTEIN_G_PER_KG_MIN}-{PROTEIN_G_PER_KG_MAX})")
    cur = con.execute(
        "UPDATE body_config SET protein_g_per_kg = ?, updated_at = ? WHERE id = 1",
        (g, datetime.datetime.now(TZ).isoformat(timespec="seconds")))
    if cur.rowcount == 0:
        raise RuntimeError("body_config is empty — run body.py --init first")


# ── weight log ────────────────────────────────────────────────────────────────

def log_weight(con, weight_kg=None, ts=None, bodyfat_pct=None, note=None,
               waist_cm=None) -> str:
    """Record a body measurement. `ts` is an aware datetime; defaults to now.

    weight_kg is optional so a waist-only measurement is loggable (they are often
    taken on different days). At least one of weight_kg / waist_cm is required.
    """
    if weight_kg is None and waist_cm is None:
        raise ValueError("nothing to log — pass weight_kg and/or waist_cm")
    if weight_kg is not None and not (25.0 <= float(weight_kg) <= 250.0):
        raise ValueError("weight_kg out of plausible range (25-250)")
    if bodyfat_pct is not None and not (3.0 <= float(bodyfat_pct) <= 60.0):
        raise ValueError("bodyfat_pct out of plausible range (3-60)")
    if waist_cm is not None and not (40.0 <= float(waist_cm) <= 200.0):
        raise ValueError("waist_cm out of plausible range (40-200)")
    ts = ts or datetime.datetime.now(TZ)
    iso = ts.isoformat(timespec="seconds")
    con.execute(
        "INSERT INTO body_metrics (ts, measured_on, weight_kg, bodyfat_pct, "
        "waist_cm, note) VALUES (?,?,?,?,?,?) ON CONFLICT(ts) DO UPDATE SET "
        "weight_kg=COALESCE(excluded.weight_kg, body_metrics.weight_kg), "
        "bodyfat_pct=COALESCE(excluded.bodyfat_pct, body_metrics.bodyfat_pct), "
        "waist_cm=COALESCE(excluded.waist_cm, body_metrics.waist_cm), "
        "note=excluded.note",
        (iso, ts.date().isoformat(),
         float(weight_kg) if weight_kg is not None else None,
         float(bodyfat_pct) if bodyfat_pct is not None else None,
         float(waist_cm) if waist_cm is not None else None, note),
    )
    return iso


def weight_at(con: sqlite3.Connection, date_iso: str) -> tuple[float | None, float | None]:
    """(weight_kg, bodyfat_pct) in effect on `date_iso`.

    Carries the last measurement forward. Before the first-ever measurement it
    reaches *backward* to the earliest one instead of returning None — otherwise
    backfilling history would produce a hole for every day before the first
    weigh-in, which is most of the history on day one.
    """
    row = con.execute(
        "SELECT weight_kg, bodyfat_pct FROM body_metrics "
        "WHERE measured_on <= ? AND weight_kg IS NOT NULL "
        "ORDER BY measured_on DESC, ts DESC LIMIT 1", (date_iso,)
    ).fetchone()
    if row:
        return row[0], row[1]
    row = con.execute(
        "SELECT weight_kg, bodyfat_pct FROM body_metrics "
        "WHERE weight_kg IS NOT NULL ORDER BY measured_on ASC, ts ASC LIMIT 1"
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def waist_at(con: sqlite3.Connection, date_iso: str) -> float | None:
    """Waist (cm) in effect on `date_iso`, or None if none ever logged.

    Same carry-forward-then-reach-backward rule as weight_at(): waist changes
    slowly, so a backfill uses the earliest known reading for days before it
    rather than leaving a hole. fitness.py falls back to a height-based prior
    when this returns None.
    """
    row = con.execute(
        "SELECT waist_cm FROM body_metrics "
        "WHERE measured_on <= ? AND waist_cm IS NOT NULL "
        "ORDER BY measured_on DESC, ts DESC LIMIT 1", (date_iso,),
    ).fetchone()
    if row:
        return row[0]
    row = con.execute(
        "SELECT waist_cm FROM body_metrics "
        "WHERE waist_cm IS NOT NULL ORDER BY measured_on ASC, ts ASC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def waist_measured_on(con: sqlite3.Connection) -> str | None:
    """Local day of the most recent waist measurement — for the stale nudge."""
    row = con.execute(
        "SELECT measured_on FROM body_metrics WHERE waist_cm IS NOT NULL "
        "ORDER BY measured_on DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def weight_series(con: sqlite3.Connection, days: int = 90) -> list[dict]:
    """Daily last-reading-wins series, oldest first."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    rows = con.execute(
        "SELECT measured_on, weight_kg, bodyfat_pct FROM body_metrics "
        "WHERE measured_on >= ? AND weight_kg IS NOT NULL "
        "ORDER BY measured_on, ts", (cutoff,)
    ).fetchall()
    out = {}
    for d, w, bf in rows:
        out[d] = {"date": d, "weight_kg": w, "bodyfat_pct": bf}
    return [out[d] for d in sorted(out)]


# ── derived ───────────────────────────────────────────────────────────────────

def age_at(birth_date: str, date_iso: str) -> int:
    b = datetime.date.fromisoformat(birth_date)
    d = datetime.date.fromisoformat(date_iso)
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


def bmr_kcal_day(weight_kg, height_cm, age, sex="male", bodyfat_pct=None) -> float:
    """Basal metabolic rate, kcal/day.

    Katch-McArdle when body fat is known (lean-mass based, so it doesn't
    under-read for a trained body), Mifflin-St Jeor otherwise.
    """
    if bodyfat_pct is not None:
        lbm = weight_kg * (1.0 - bodyfat_pct / 100.0)
        return 370.0 + 21.6 * lbm
    return (10.0 * weight_kg + 6.25 * height_cm - 5.0 * age + SEX_CONST[sex])


def bmr_for_date(con: sqlite3.Connection, date_iso: str) -> tuple[float, dict]:
    """BMR kcal/day on a given date, plus the inputs used. Raises if unconfigured."""
    cfg = get_config(con)
    if cfg is None:
        raise RuntimeError("body_config is empty — run body.py --init first")
    weight, bodyfat = weight_at(con, date_iso)
    if weight is None:
        raise RuntimeError("no weight logged — log one before computing calories")
    age = age_at(cfg["birth_date"], date_iso)
    bmr = bmr_kcal_day(weight, cfg["height_cm"], age, cfg["sex"], bodyfat)
    return bmr, {"weight_kg": weight, "bodyfat_pct": bodyfat, "age": age,
                 "height_cm": cfg["height_cm"], "sex": cfg["sex"], "k": cfg["k"]}


def protein_target(con: sqlite3.Connection, date_iso: str | None = None) -> dict | None:
    """Daily protein target in grams, derived from the weight in effect that day.

    Returns None when it cannot be derived — unconfigured body, or no weight ever
    logged. A caller must treat that as "no target yet" rather than substituting
    a number, since a target invented from a guessed weight is worse than none.
    """
    date_iso = date_iso or datetime.date.today().isoformat()
    cfg = get_config(con)
    if cfg is None:
        return None
    weight, _ = weight_at(con, date_iso)
    if weight is None:
        return None
    g_per_kg = cfg["protein_g_per_kg"]
    raw = weight * g_per_kg
    target = int(round(raw / PROTEIN_TARGET_ROUND_G) * PROTEIN_TARGET_ROUND_G)
    return {"date": date_iso, "target_g": target, "g_per_kg": g_per_kg,
            "weight_kg": weight, "exact_g": round(raw, 1)}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", nargs=3, metavar=("HEIGHT_CM", "BIRTH_DATE", "SEX"))
    ap.add_argument("--weight", type=float)
    ap.add_argument("--bodyfat", type=float)
    ap.add_argument("--protein", type=float, metavar="G_PER_KG",
                    help=f"protein target, g/kg "
                         f"({PROTEIN_G_PER_KG_MIN}-{PROTEIN_G_PER_KG_MAX})")
    ap.add_argument("--on", help="YYYY-MM-DD (defaults to today)")
    args = ap.parse_args()

    con = sqlite3.connect(config.ACTA_DB)
    ensure(con)
    if args.init:
        set_config(con, float(args.init[0]), args.init[1], args.init[2])
        con.commit()
        print("config set:", get_config(con))
    if args.protein is not None:
        set_protein_g_per_kg(con, args.protein)
        con.commit()
    if args.weight is not None:
        ts = datetime.datetime.now(TZ)
        if args.on:
            d = datetime.date.fromisoformat(args.on)
            ts = datetime.datetime(d.year, d.month, d.day, 8, 0, tzinfo=TZ)
        print("logged:", log_weight(con, args.weight, ts, args.bodyfat))
        con.commit()

    cfg = get_config(con)
    print("\nconfig:", cfg)
    if cfg:
        today = datetime.date.today().isoformat()
        try:
            bmr, used = bmr_for_date(con, today)
            print(f"BMR today: {bmr:.0f} kcal/day  ({bmr/1440:.4f} kcal/min)")
            print("  inputs:", used)
        except RuntimeError as e:
            print("  BMR:", e)
        pt = protein_target(con, today)
        if pt:
            print(f"protein target: {pt['target_g']} g/day "
                  f"({pt['g_per_kg']} g/kg x {pt['weight_kg']} kg = {pt['exact_g']} g)")
        else:
            print("protein target: not derivable (no weight logged)")
    print("\nweights:")
    for r in weight_series(con, 365):
        print(f"  {r['date']}  {r['weight_kg']} kg"
              + (f"  bf {r['bodyfat_pct']}%" if r["bodyfat_pct"] else ""))
    con.close()


if __name__ == "__main__":
    _main()
