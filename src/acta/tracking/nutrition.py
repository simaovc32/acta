"""
Food + drink intake — a personal food library that grows as you log.

Why a library rather than a fresh estimate per meal
---------------------------------------------------
Calibrating `k` in calories.py (energy *out*) against measured weight change
needs the intake side to be *consistent*, not merely accurate: the same plate
must score the same number every time, or the noise swamps the signal. So a food
is resolved **once** — by you, or by an LLM you then confirm — and every later
log of that food reuses the stored value. First entry costs one API call; every
repeat costs nothing and is deterministic.

Portion size, not the food database, is the dominant error here. "Rice and
chicken" is 400-900 kcal depending on how much rice, so every log stores grams;
the typical-portion figure is a default to adjust, never a hidden assumption.

Logged values are denormalised into `food_log`
----------------------------------------------
Each log row copies the name and computed kcal at the moment it was logged.
Correcting a library entry therefore fixes future logs and leaves history alone
— the same reason finance_balance rows are snapshots rather than references.

Drinks
------
Coffee and alcohol still write to events.json (they drive biocharge drain and
the sleep-score alcohol penalty). Logging one here *also* records its calories,
so a beer counts as both an alcohol event and ~130 kcal without double entry.

CLI:
    python -m acta.tracking.nutrition --search arroz
    python -m acta.tracking.nutrition --resolve "arroz com frango, 1 prato"
    python -m acta.tracking.nutrition --today
"""
import argparse
import datetime
import json
import os
import re
import sqlite3
import unicodedata

from acta import config

TZ = config.TZ
# Checked in order so the key lives in one place and rotating it doesn't mean
# hunting through three projects.
ENV_FILES = (config.ENV_FILE,)
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# Estimating the calories in a described plate of food is an easy task — it does
# not need a frontier model, and a cheap one keeps a whole library under a cent.
LLM_MODEL = "google/gemini-3.1-flash-lite"
LLM_TIMEOUT = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS food_item (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,
  name_key      TEXT NOT NULL UNIQUE,   -- normalised; dedupe + search
  kind          TEXT NOT NULL DEFAULT 'food',   -- food | drink
  kcal_100g     REAL NOT NULL,
  protein_g     REAL,                   -- all macros per 100 g
  carbs_g       REAL,
  fat_g         REAL,
  portion_g     REAL,                   -- typical serving, grams or ml
  portion_label TEXT,                   -- "1 prato", "200 ml"
  sizes         TEXT,                   -- JSON [{label, grams}], for items with
                                         -- named sizes (Starbucks Tall/Grande/Venti);
                                         -- NULL for everything else
  abv_pct       REAL,                   -- alcohol by volume %, for kind='drink';
                                         -- 0 = non-alcoholic, NULL = unknown/not a drink.
                                         -- Logging one raises an alcohol event whose
                                         -- units = ml * abv/100 * 0.789 / 10.
  source        TEXT NOT NULL DEFAULT 'manual',  -- manual | llm | builtin
  pinned        INTEGER NOT NULL DEFAULT 0,      -- 1 = gets a one-tap button
  times_used    INTEGER NOT NULL DEFAULT 0,
  last_used     TEXT,
  created_at    TEXT,
  updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS food_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  measured_on TEXT NOT NULL,            -- local day, YYYY-MM-DD
  food_id     INTEGER,                  -- may go stale; name/kcal are the record
  name        TEXT NOT NULL,
  grams       REAL NOT NULL,
  kcal        REAL NOT NULL,
  protein_g   REAL,
  carbs_g     REAL,
  fat_g       REAL,
  meal_id     TEXT,                     -- groups the rows of one dish; NULL if standalone
  meal_name   TEXT,                     -- denormalised, like name/kcal
  meal_slot   TEXT,                     -- breakfast | morning_snack | lunch | ...
  note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_food_log_day ON food_log(measured_on);

-- A saved dish: which library foods, in what amounts. Components reference
-- food_item so a corrected ingredient improves every future log of the meal;
-- the amounts live here because they are the part that is personal to the dish.
CREATE TABLE IF NOT EXISTS meal (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  name_key    TEXT NOT NULL UNIQUE,
  components  TEXT NOT NULL,            -- JSON [{food_id, name, grams}]
  pinned      INTEGER NOT NULL DEFAULT 0,   -- 1 = gets a one-tap button, like food_item
  times_used  INTEGER NOT NULL DEFAULT 0,
  last_used   TEXT,
  created_at  TEXT,
  updated_at  TEXT
);
"""

# Seeded so the drink row works on first open. Alcohol figures use the same
# real pours already calibrated in biocharge.ALCOHOL_UNITS_PER_DRINK.
# ── meal slots ────────────────────────────────────────────────────────────────
# The slot is derived from the clock when a row is logged and then *stored*, so
# a correction sticks. Deriving on read instead would silently re-file history
# every time the windows moved.
MEAL_SLOTS = ("breakfast", "morning_snack", "lunch", "afternoon_snack", "dinner",
              "off_hours")
SLOT_LABELS = {"breakfast": "Breakfast", "morning_snack": "Morning snack",
               "lunch": "Lunch", "afternoon_snack": "Afternoon snack",
               "dinner": "Dinner", "off_hours": "Off hours"}
# (slot, start, end) in minutes past local midnight, start inclusive / end
# exclusive. The 5 real windows are deliberately *not* contiguous — there are
# real gaps between them (mid-morning, mid-afternoon, the evening lull, and the
# whole night). A time inside a gap is not any of these meals, so it is never
# forced into one: slot_for_time falls back to the honest "off_hours" bucket
# instead of guessing which meal it's nearest to (see _clean_slot).
SLOT_WINDOWS = (
    ("breakfast",       360,  570),   # 06:00-09:30
    ("morning_snack",   600,  690),   # 10:00-11:30
    ("lunch",           720,  870),   # 12:00-14:30
    ("afternoon_snack", 960,  1080),  # 16:00-18:00
    ("dinner",          1170, 1320),  # 19:30-22:00
)


def slot_for_time(dt: datetime.datetime) -> str:
    """"off_hours" means dt falls in a gap between the 5 real meal windows —
    e.g. a coffee at 15:00 is not lunch food, so it gets its own honest
    bucket rather than being filed under whichever meal is nearest."""
    minutes = dt.hour * 60 + dt.minute
    for slot, start, end in SLOT_WINDOWS:
        if start <= minutes < end:
            return slot
    return "off_hours"


def _clean_slot(slot: str | None, ts: datetime.datetime) -> str:
    """An explicit slot always wins; otherwise the clock decides, falling back
    to "off_hours" outside the 5 real windows instead of raising — the whole
    point of that bucket is that nothing needs to be misfiled into a meal it
    wasn't part of just to satisfy a NOT NULL column."""
    if slot in MEAL_SLOTS:
        return slot
    if slot:
        raise ValueError(f"meal_slot must be one of {', '.join(MEAL_SLOTS)}")
    return slot_for_time(ts)


BUILTINS = [
    # name,            kind,    kcal/100g, P,    C,    F,    portion, label,               abv%
    ("Water",          "drink",   0.0,   0.0,  0.0,  0.0,  250,  "1 glass (250 ml)",       0.0),
    ("Coffee",         "drink",   2.0,   0.1,  0.0,  0.0,   40,  "1 espresso (40 ml)",     0.0),
    ("Beer",           "drink",  43.0,   0.5,  3.6,  0.0,  200,  "1 glass (200 ml)",       5.0),
    ("Wine",           "drink",  83.0,   0.1,  2.6,  0.0,  125,  "1 glass (125 ml)",      12.0),
    ("Cider",          "drink",  49.0,   0.0,  5.3,  0.0,  200,  "1 glass (200 ml)",       4.5),
]


# ── helpers ───────────────────────────────────────────────────────────────────

def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # Additive-only migration, same pattern as ingest.migrate().
    have = {r[1] for r in con.execute("PRAGMA table_info(food_item)")}
    if "pinned" not in have:
        con.execute("ALTER TABLE food_item ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    if "sizes" not in have:
        con.execute("ALTER TABLE food_item ADD COLUMN sizes TEXT")
    if "abv_pct" not in have:
        con.execute("ALTER TABLE food_item ADD COLUMN abv_pct REAL")
        # The three seeded alcoholic builtins predate the column; give them the
        # ABVs their unit figures in biocharge.ALCOHOL_UNITS_PER_DRINK assume.
        for nm, abv in (("beer", 5.0), ("wine", 12.0), ("cider", 4.5)):
            con.execute("UPDATE food_item SET abv_pct=? WHERE name_key=?", (abv, nm))
    if "pinned" not in {r[1] for r in con.execute("PRAGMA table_info(meal)")}:
        con.execute("ALTER TABLE meal ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    have_log = {r[1] for r in con.execute("PRAGMA table_info(food_log)")}
    for col in ("meal_id", "meal_name", "meal_slot"):
        if col not in have_log:
            con.execute(f"ALTER TABLE food_log ADD COLUMN {col} TEXT")
    _backfill_slots(con)
    # Indexed after the migration, not in SCHEMA: on a pre-meal database the
    # column does not exist yet when the script runs, and CREATE INDEX would
    # fail before the ALTER above ever had a chance to add it.
    con.execute("CREATE INDEX IF NOT EXISTS idx_food_log_meal ON food_log(meal_id)")


def _backfill_slots(con: sqlite3.Connection) -> int:
    """File rows logged before slots existed, using their own timestamp.

    Only ever touches NULLs, so it is idempotent and can never overwrite a slot
    the user corrected by hand. A legacy row from a gap time lands in
    "off_hours", same as a fresh log would.
    """
    rows = con.execute(
        "SELECT id, ts FROM food_log WHERE meal_slot IS NULL").fetchall()
    n = 0
    for r in rows:
        try:
            dt = datetime.datetime.fromisoformat(r[1])
        except (TypeError, ValueError):
            continue
        con.execute("UPDATE food_log SET meal_slot=? WHERE id=?",
                    (slot_for_time(dt), r[0]))
        n += 1
    return n


def set_slot(con: sqlite3.Connection, log_id: int, slot: str) -> None:
    """Move one logged entry to another meal."""
    if slot not in MEAL_SLOTS:
        raise ValueError(f"meal_slot must be one of {', '.join(MEAL_SLOTS)}")
    cur = con.execute("UPDATE food_log SET meal_slot=? WHERE id=?", (slot, log_id))
    if cur.rowcount == 0:
        raise ValueError("no such log entry")


def set_pinned(con: sqlite3.Connection, food_id: int, pinned: bool) -> None:
    """Pinned items get a one-tap button; everything else stays searchable.

    Explicit rather than usage-ranked: what you want one tap away is not the
    same as what you happen to log most, and only you know which is which.
    """
    con.execute("UPDATE food_item SET pinned=?, updated_at=? WHERE id=?",
                (1 if pinned else 0, _now_iso(), food_id))


def set_meal_pinned(con: sqlite3.Connection, meal_id: int, pinned: bool) -> None:
    """A saved dish gets a one-tap button too — same idea as food_item.pinned."""
    con.execute("UPDATE meal SET pinned=?, updated_at=? WHERE id=?",
                (1 if pinned else 0, _now_iso(), meal_id))


def norm_key(name: str) -> str:
    """Accent- and case-insensitive key, so 'Arroz' and 'arroz' are one food."""
    s = unicodedata.normalize("NFKD", (name or "").strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", s)).strip()


def _now_iso() -> str:
    return datetime.datetime.now(TZ).isoformat(timespec="seconds")


def seed_builtins(con: sqlite3.Connection) -> int:
    n = 0
    for name, kind, kcal, p, c, f, pg, label, abv in BUILTINS:
        key = norm_key(name)
        if con.execute("SELECT 1 FROM food_item WHERE name_key=?", (key,)).fetchone():
            continue
        con.execute(
            "INSERT INTO food_item (name,name_key,kind,kcal_100g,protein_g,carbs_g,"
            "fat_g,portion_g,portion_label,abv_pct,source,pinned,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'builtin',1,?,?)",
            (name, key, kind, kcal, p, c, f, pg, label, abv, _now_iso(), _now_iso()),
        )
        n += 1
    return n


# ── library ───────────────────────────────────────────────────────────────────

def _row(r) -> dict:
    return {k: r[k] for k in r.keys()}


def _item_row(r) -> dict:
    """Like _row, but decodes the sizes JSON — same pattern as _meal_row for
    components. NULL (the common case: no named sizes) stays None, not ''."""
    d = _row(r)
    d["sizes"] = json.loads(d["sizes"]) if d.get("sizes") else None
    return d


def search(con: sqlite3.Connection, q: str = "", kind: str | None = None,
           limit: int = 20) -> list[dict]:
    """Library search. Most-used first, so the foods you actually eat surface."""
    sql = "SELECT * FROM food_item WHERE 1=1"
    args: list = []
    if q:
        sql += " AND name_key LIKE ?"
        args.append(f"%{norm_key(q)}%")
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY times_used DESC, last_used DESC, name LIMIT ?"
    args.append(limit)
    return [_item_row(r) for r in con.execute(sql, args)]


def get_item(con: sqlite3.Connection, food_id: int) -> dict | None:
    r = con.execute("SELECT * FROM food_item WHERE id=?", (food_id,)).fetchone()
    return _item_row(r) if r else None


def upsert_item(con, name, kcal_100g, *, kind="food", protein_g=None, carbs_g=None,
                fat_g=None, portion_g=None, portion_label=None, sizes=None,
                abv_pct=None, source="manual") -> dict:
    """Create or update a library entry, keyed on the normalised name.

    sizes is [{"label": "Tall", "grams": 354}, ...] for a drink with named
    sizes (Starbucks); leave it unset (None) on an update and the item's
    existing sizes survive — an edit that isn't about sizes must not silently
    wipe them. Pass [] explicitly to clear them.

    abv_pct is alcohol by volume % (0 = non-alcoholic, None = not mentioned).
    Same COALESCE contract as sizes: None on an update keeps the stored value,
    0.0 writes through as "confirmed non-alcoholic".
    """
    if not (name or "").strip():
        raise ValueError("name is required")
    if not (0 <= float(kcal_100g) <= 900):
        raise ValueError("kcal_100g out of plausible range (0-900)")
    key = _validated_key(name)
    now = _now_iso()
    # None is the "not mentioned" sentinel COALESCE below leaves alone; an
    # explicit [] must still write through as a real (empty) value, or there
    # would be no way to ever clear a previously-set list of sizes.
    sizes_json = None if sizes is None else json.dumps(sizes, ensure_ascii=False)
    abv = None if abv_pct is None else float(abv_pct)
    existing = con.execute("SELECT id FROM food_item WHERE name_key=?", (key,)).fetchone()
    if existing:
        con.execute(
            "UPDATE food_item SET name=?,kind=?,kcal_100g=?,protein_g=?,carbs_g=?,"
            "fat_g=?,portion_g=?,portion_label=?,sizes=COALESCE(?,sizes),"
            "abv_pct=COALESCE(?,abv_pct),source=?,updated_at=? WHERE id=?",
            (name.strip(), kind, float(kcal_100g), protein_g, carbs_g, fat_g,
             portion_g, portion_label, sizes_json, abv, source, now, existing[0]),
        )
        return get_item(con, existing[0])
    cur = con.execute(
        "INSERT INTO food_item (name,name_key,kind,kcal_100g,protein_g,carbs_g,"
        "fat_g,portion_g,portion_label,sizes,abv_pct,source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name.strip(), key, kind, float(kcal_100g), protein_g, carbs_g, fat_g,
         portion_g, portion_label, sizes_json, abv, source, now, now),
    )
    return get_item(con, cur.lastrowid)


def _validated_key(name: str) -> str:
    key = norm_key(name)
    if not key:
        raise ValueError("name must contain letters or digits")
    return key


def delete_item(con: sqlite3.Connection, food_id: int) -> None:
    """Remove a library entry. Past log rows keep their own name and kcal."""
    con.execute("DELETE FROM food_item WHERE id=?", (food_id,))


# ── logging ───────────────────────────────────────────────────────────────────

def log_food(con, *, food_id=None, name=None, grams, ts=None, note=None,
             meal_id=None, meal_name=None, slot=None) -> dict:
    """Record an intake. Macros are scaled from the library entry's per-100 g values."""
    grams = float(grams)
    if not (0 < grams <= 5000):
        raise ValueError("grams out of plausible range (0-5000)")
    item = get_item(con, food_id) if food_id else None
    if item is None and name:
        r = con.execute("SELECT * FROM food_item WHERE name_key=?",
                        (norm_key(name),)).fetchone()
        item = _item_row(r) if r else None
    if item is None:
        raise ValueError("unknown food — add it to the library first")

    ts = ts or datetime.datetime.now(TZ)
    meal_slot = _clean_slot(slot, ts)
    f = grams / 100.0
    scale = lambda v: round(v * f, 1) if v is not None else None   # noqa: E731
    kcal = round(item["kcal_100g"] * f, 1)
    cur = con.execute(
        "INSERT INTO food_log (ts,measured_on,food_id,name,grams,kcal,protein_g,"
        "carbs_g,fat_g,meal_id,meal_name,meal_slot,note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts.isoformat(timespec="seconds"), ts.date().isoformat(), item["id"],
         item["name"], grams, kcal, scale(item["protein_g"]),
         scale(item["carbs_g"]), scale(item["fat_g"]), meal_id, meal_name,
         meal_slot, note),
    )
    con.execute(
        "UPDATE food_item SET times_used=times_used+1, last_used=? WHERE id=?",
        (_now_iso(), item["id"]),
    )
    return _row(con.execute("SELECT * FROM food_log WHERE id=?",
                            (cur.lastrowid,)).fetchone())


def log_food_adhoc(con, *, name, kcal_100g, grams, protein_g=None, ts=None,
                   note=None, slot=None) -> dict:
    """Log a one-off entry with hand-typed numbers, without a food_item row.

    For "hardcode the calories, don't remember this food" — a manual override
    the LLM library-first design isn't meant for (a weird restaurant portion,
    a number you just don't trust). food_id stays NULL; carbs_g/fat_g stay
    NULL since there's nowhere to source them from without a library entry.
    Never touches food_item, so it can't pollute search/autocomplete or the
    determinism the library exists for.
    """
    grams = float(grams)
    if not (0 < grams <= 5000):
        raise ValueError("grams out of plausible range (0-5000)")
    name = (name or "").strip()
    if not name:
        raise ValueError("name is required")
    kcal_100g = float(kcal_100g)
    if not (0 <= kcal_100g <= 900):
        raise ValueError("kcal_100g out of plausible range (0-900)")

    ts = ts or datetime.datetime.now(TZ)
    meal_slot = _clean_slot(slot, ts)
    f = grams / 100.0
    kcal = round(kcal_100g * f, 1)
    protein = round(float(protein_g) * f, 1) if protein_g is not None else None
    cur = con.execute(
        "INSERT INTO food_log (ts,measured_on,food_id,name,grams,kcal,protein_g,"
        "carbs_g,fat_g,meal_id,meal_name,meal_slot,note) "
        "VALUES (?,?,NULL,?,?,?,?,NULL,NULL,NULL,NULL,?,?)",
        (ts.isoformat(timespec="seconds"), ts.date().isoformat(), name, grams,
         kcal, protein, meal_slot, note),
    )
    return _row(con.execute("SELECT * FROM food_log WHERE id=?",
                            (cur.lastrowid,)).fetchone())


# ── meals (multi-component dishes) ────────────────────────────────────────────
#
# Why components rather than one composite figure: a mixed dish is a ratio, and
# the same ingredients in different amounts differ far more in protein than in
# calories, so one kcal_100g can't describe both. Each part is also resolved once
# and reused, which keeps the numbers reproducible.

def log_meal(con, *, name, components, ts=None, note=None, slot=None) -> dict:
    """Log a dish as one row per component, sharing a meal_id.

    One row per component (rather than one summed row) so every existing
    aggregate — day totals, macros, history, the energy balance — keeps working
    untouched: they all just sum rows. The grouping is presentation only.
    """
    if not components:
        raise ValueError("a meal needs at least one component")
    ts = ts or datetime.datetime.now(TZ)
    meal_id = ts.strftime("%Y%m%dT%H%M%S") + "-" + os.urandom(3).hex()
    meal_name = (name or "").strip() or "Meal"
    # Resolved once for the whole dish: its parts are one meal by definition, so
    # they must never end up split across two slots.
    meal_slot = _clean_slot(slot, ts)
    rows = []
    for c in components:
        rows.append(log_food(con, food_id=c.get("food_id"), name=c.get("name"),
                             grams=c["grams"], ts=ts, note=note,
                             meal_id=meal_id, meal_name=meal_name,
                             slot=meal_slot))
    return {"meal_id": meal_id, "meal_name": meal_name, "slot": meal_slot,
            "rows": rows,
            "kcal": round(sum(r["kcal"] for r in rows), 1),
            "protein_g": round(sum(r["protein_g"] or 0 for r in rows), 1)}


def delete_meal_log(con: sqlite3.Connection, meal_id: str) -> int:
    """Remove every row of one logged meal. Scoped to that meal_id only."""
    cur = con.execute("DELETE FROM food_log WHERE meal_id=?", (meal_id,))
    return cur.rowcount


# ── saved meal templates ──────────────────────────────────────────────────────

def save_meal(con, name: str, components: list[dict]) -> dict:
    """Store a dish so it can be re-logged in one tap with its usual amounts."""
    key = _validated_key(name)
    clean = []
    for c in components:
        if c.get("food_id") is None:
            raise ValueError("every meal component needs a food_id")
        clean.append({"food_id": int(c["food_id"]),
                      "name": (c.get("name") or "").strip(),
                      "grams": float(c["grams"])})
    if not clean:
        raise ValueError("a meal needs at least one component")
    blob, now = json.dumps(clean, ensure_ascii=False), _now_iso()
    existing = con.execute("SELECT id FROM meal WHERE name_key=?", (key,)).fetchone()
    if existing:
        con.execute("UPDATE meal SET name=?, components=?, updated_at=? WHERE id=?",
                    (name.strip(), blob, now, existing[0]))
        return get_meal(con, existing[0])
    cur = con.execute(
        "INSERT INTO meal (name,name_key,components,created_at,updated_at) "
        "VALUES (?,?,?,?,?)", (name.strip(), key, blob, now, now))
    return get_meal(con, cur.lastrowid)


def _meal_row(r) -> dict:
    d = {k: r[k] for k in r.keys()}
    d["components"] = json.loads(d["components"])
    return d


def get_meal(con: sqlite3.Connection, meal_id: int) -> dict | None:
    r = con.execute("SELECT * FROM meal WHERE id=?", (meal_id,)).fetchone()
    return _meal_row(r) if r else None


def list_meals(con: sqlite3.Connection) -> list[dict]:
    return [_meal_row(r) for r in con.execute(
        "SELECT * FROM meal ORDER BY times_used DESC, last_used DESC, name")]


def delete_meal(con: sqlite3.Connection, meal_id: int) -> None:
    """Remove a saved dish. Already-logged rows keep their own copies."""
    con.execute("DELETE FROM meal WHERE id=?", (meal_id,))


def log_saved_meal(con, meal_id: int, ts=None, slot=None) -> dict:
    """Re-log a saved dish. Components whose library entry has since been deleted
    are skipped rather than failing the whole meal."""
    m = get_meal(con, meal_id)
    if m is None:
        raise ValueError("no such meal")
    live = [c for c in m["components"] if get_item(con, c["food_id"]) is not None]
    if not live:
        raise ValueError("every food in this meal has been deleted from the library")
    out = log_meal(con, name=m["name"], components=live, ts=ts, slot=slot)
    con.execute("UPDATE meal SET times_used=times_used+1, last_used=? WHERE id=?",
                (_now_iso(), meal_id))
    out["skipped"] = len(m["components"]) - len(live)
    return out


def delete_log(con: sqlite3.Connection, log_id: int) -> None:
    con.execute("DELETE FROM food_log WHERE id=?", (log_id,))


def day_log(con: sqlite3.Connection, date_iso: str) -> list[dict]:
    return [_row(r) for r in con.execute(
        "SELECT * FROM food_log WHERE measured_on=? ORDER BY ts", (date_iso,))]


def day_by_slot(con: sqlite3.Connection, date_iso: str) -> list[dict]:
    """The day split into its meals, always all six and always in day order —
    an empty slot is information ("you skipped breakfast"), not a row to hide."""
    entries = day_log(con, date_iso)
    buckets = {s: [] for s in MEAL_SLOTS}
    for e in entries:
        buckets.get(e.get("meal_slot") or slot_for_time(
            datetime.datetime.fromisoformat(e["ts"])), buckets["off_hours"]).append(e)
    out = []
    for s in MEAL_SLOTS:
        rows = buckets[s]
        out.append({
            "slot": s, "label": SLOT_LABELS[s], "entries": rows,
            "kcal": round(sum(r["kcal"] or 0 for r in rows)),
            "protein_g": round(sum(r["protein_g"] or 0 for r in rows), 1),
            "carbs_g": round(sum(r["carbs_g"] or 0 for r in rows), 1),
            "fat_g": round(sum(r["fat_g"] or 0 for r in rows), 1),
        })
    return out


def day_totals(con: sqlite3.Connection, date_iso: str) -> dict:
    r = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(kcal),0) kcal, COALESCE(SUM(protein_g),0) p,"
        " COALESCE(SUM(carbs_g),0) c, COALESCE(SUM(fat_g),0) f "
        "FROM food_log WHERE measured_on=?", (date_iso,)).fetchone()
    return {"date": date_iso, "entries": r["n"], "kcal": round(r["kcal"]),
            "protein_g": round(r["p"], 1), "carbs_g": round(r["c"], 1),
            "fat_g": round(r["f"], 1)}


def history(con: sqlite3.Connection, days: int = 30) -> list[dict]:
    cutoff = (datetime.datetime.now(TZ).date()
              - datetime.timedelta(days=days)).isoformat()
    return [{"date": r["measured_on"], "kcal": round(r["kcal"]),
             "entries": r["n"]}
            for r in con.execute(
                "SELECT measured_on, SUM(kcal) kcal, COUNT(*) n FROM food_log "
                "WHERE measured_on >= ? GROUP BY measured_on ORDER BY measured_on",
                (cutoff,))]


# ── LLM resolution (first time a food is seen) ────────────────────────────────

def _api_key() -> str | None:
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    for path in ENV_FILES:
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("OPENROUTER_API_KEY="):
                        return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return None


PROMPT = """You estimate nutrition for foods and drinks. The user is Portuguese; \
descriptions may be in Portuguese or English.

Return ONLY a JSON object, no prose and no markdown fence.

SINGLE ITEM — for anything bought, cooked or served as one unit:
{"name": str, "kind": "food"|"drink", "kcal_100g": num, "protein_g": num,
 "carbs_g": num, "fat_g": num, "portion_g": num, "portion_label": str,
 "abv_pct": num, "confidence": "high"|"medium"|"low", "note": str}

MULTI-COMPONENT — for a plate whose parts are served in amounts that vary
independently of each other:
{"name": str, "kind": "food", "confidence": ..., "note": ...,
 "components": [{"name": str, "grams": num, "kcal_100g": num,
                 "protein_g": num, "carbs_g": num, "fat_g": num}, ...]}

Choosing between them — this is the important decision:
- Split into components when someone could reasonably plate the parts in
  different ratios: "frango com arroz", "bacalhau com batatas", "salada com
  atum". The ratio, not the dish, decides the protein, so each part needs its
  own amount.
- Keep as a single item when it is assembled, cooked or sold as one thing and
  you cannot vary the parts on your plate: "pão com ovo", "lasanha", "sopa de
  legumes", "hambúrguer", any packaged or branded product.
- Never split into more than 4 components, and never split out seasonings,
  sauces or oil — fold those into the component they are cooked with.
- Each component's grams is the amount of THAT part actually eaten.

Rules for all fields:
- abv_pct is alcohol by volume, as a percentage. Give a realistic figure for any
  alcoholic drink (beer ~5, wine ~12, sangria ~11, gin & tonic ~10, spirits neat
  ~40). Use 0 for water, coffee, juice, soft drinks and all food. Only present on
  a SINGLE ITEM, never on components.
- All macro values are per 100 g (or per 100 ml for drinks). kcal_100g must be
  consistent with the macros (4/4/9 kcal per g of protein/carb/fat).
- Macros describe the food AS EATEN (cooked rice, grilled chicken), not raw.
- portion_g is the typical amount actually eaten for THIS description, in grams
  or ml. If the description names a portion ("1 prato", "uma taça"), size
  portion_g to that. Otherwise use a normal single serving.
- portion_label describes that amount in the user's own words, with the number.
- name: a short canonical name for the library, in the language the user used.
  For components use the plain ingredient name ("arroz cozido", "frango
  grelhado") so it is reusable in other dishes.
- confidence "low" when the description is too vague to size sensibly; say why
  in note. Keep note under 12 words, and empty when confidence is high."""


def resolve(description: str, *, model: str = LLM_MODEL) -> dict:
    """Ask the LLM for one food's nutrition. Raises on failure — the caller
    surfaces that so the user can enter the numbers by hand instead."""
    import requests

    desc = (description or "").strip()
    if not desc:
        raise ValueError("description is required")
    key = _api_key()
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY (checked env and "
                           + ", ".join(ENV_FILES) + ")")

    resp = requests.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {key}",
                 "X-Title": "Acta Dashboard"},
        json={"model": model,
              "messages": [{"role": "system", "content": PROMPT},
                           {"role": "user", "content": desc}],
              "response_format": {"type": "json_object"},
              "temperature": 0.2, "max_tokens": 400},
        timeout=LLM_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:200]}")
    text = resp.json()["choices"][0]["message"]["content"]
    return _parse(text, desc)


def _parse(text: str, desc: str) -> dict:
    """Parse the model's JSON, tolerating a markdown fence, then sanity-check it."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise RuntimeError(f"model did not return JSON: {t[:150]}")
    d = json.loads(m.group(0))
    num = _num_reader(d)

    conf = d.get("confidence") if d.get("confidence") in ("high", "medium", "low") \
        else "medium"
    note = (d.get("note") or "")[:120] or None

    # Multi-component dish: validate each part the same way as a single item and
    # drop any that fails, rather than rejecting the whole meal over one bad row.
    raw_comps = d.get("components")
    if isinstance(raw_comps, list) and len(raw_comps) > 1:
        comps = []
        for c in raw_comps[:4]:
            if not isinstance(c, dict):
                continue
            sub = _num_reader(c)
            ckcal = sub("kcal_100g", 0, 900)
            grams = sub("grams", 1, 3000)
            cname = (c.get("name") or "").strip()
            if ckcal is None or grams is None or not cname:
                continue
            comps.append({
                "name": cname[:80],
                "grams": round(grams),
                "kcal_100g": round(ckcal, 1),
                "protein_g": sub("protein_g", 0, 100),
                "carbs_g": sub("carbs_g", 0, 100),
                "fat_g": sub("fat_g", 0, 100),
                "est_kcal": round(ckcal * grams / 100.0),
            })
        if len(comps) > 1:
            return {
                "name": (d.get("name") or desc)[:80],
                "kind": "food",
                "components": comps,
                "confidence": conf,
                "note": note,
                "source": "llm",
                "est_kcal": round(sum(c["est_kcal"] for c in comps)),
            }
        # One usable component is just a single food — fall through if possible.

    kcal = num("kcal_100g", 0, 900)
    if kcal is None:
        raise RuntimeError(f"implausible or missing kcal_100g: {d.get('kcal_100g')!r}")
    portion = num("portion_g", 1, 3000, 100.0)
    is_drink = d.get("kind") == "drink"
    return {
        "name": (d.get("name") or desc)[:80],
        "kind": "drink" if is_drink else "food",
        "kcal_100g": round(kcal, 1),
        "protein_g": num("protein_g", 0, 100),
        "carbs_g": num("carbs_g", 0, 100),
        "fat_g": num("fat_g", 0, 100),
        "portion_g": round(portion),
        "portion_label": (d.get("portion_label") or "")[:60] or None,
        # Only carried for drinks; a food never raises an alcohol event even if
        # the model volunteers a number. round to 1 dp — ABV is never that precise.
        "abv_pct": (round(num("abv_pct", 0, 96, 0.0), 1) if is_drink else None),
        "confidence": conf,
        "note": note,
        "components": None,
        "source": "llm",
        "est_kcal": round(kcal * portion / 100.0),
    }


def _num_reader(d: dict):
    """Range-checked numeric getter over one dict — same contract as _parse's."""
    def num(k, lo, hi, default=None):
        v = d.get(k)
        if v is None:
            return default
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return v if lo <= v <= hi else default
    return num


# ── CLI ───────────────────────────────────────────────────────────────────────

def _open():
    con = sqlite3.connect(config.ACTA_DB, timeout=30)
    con.row_factory = sqlite3.Row
    ensure(con)
    return con


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--search", metavar="Q")
    ap.add_argument("--resolve", metavar="DESC")
    ap.add_argument("--today", action="store_true")
    args = ap.parse_args()

    if args.resolve:
        print(json.dumps(resolve(args.resolve), indent=2, ensure_ascii=False))
        return

    con = _open()
    if args.seed:
        print(f"seeded {seed_builtins(con)} builtin items")
        con.commit()
    if args.search is not None:
        for it in search(con, args.search):
            print(f"  [{it['id']:>3}] {it['name']:<28} {it['kcal_100g']:>6.0f}/100g"
                  f"  portion {it['portion_g'] or '—'}  used {it['times_used']}×"
                  f"  ({it['source']})")
    if args.today:
        today = datetime.datetime.now(TZ).date().isoformat()
        for e in day_log(con, today):
            print(f"  {e['ts'][11:16]}  {e['name']:<28} {e['grams']:>6.0f} g"
                  f"  {e['kcal']:>6.0f} kcal")
        t = day_totals(con, today)
        print(f"  TOTAL {t['kcal']} kcal  ·  P {t['protein_g']}  C {t['carbs_g']}"
              f"  F {t['fat_g']}  ({t['entries']} entries)")
    con.close()


if __name__ == "__main__":
    _main()
