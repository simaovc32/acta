"""
Unit tests for nutrition.py — library, meals, and LLM response parsing.

Runs entirely against temporary in-memory databases and hand-written model
payloads. Never touches the production acta.db and never calls OpenRouter.
"""
import json
import sqlite3

from acta.tracking import nutrition as N
from conftest import check


def fresh():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    N.ensure(con)
    return con


def seed_two(con):
    """Cooked rice and grilled chicken — the canonical ratio problem."""
    rice = N.upsert_item(con, "Arroz cozido", 130, protein_g=2.7, carbs_g=28.0,
                         fat_g=0.3, portion_g=150)
    chick = N.upsert_item(con, "Frango grelhado", 165, protein_g=31.0, carbs_g=0.0,
                          fat_g=3.6, portion_g=120)
    return rice, chick


# ── migration ─────────────────────────────────────────────────────────────────

def test_migration():
    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    # A food_log from before meals existed, with a row already in it.
    old.execute("CREATE TABLE food_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts TEXT NOT NULL, measured_on TEXT NOT NULL, food_id INTEGER,"
                " name TEXT NOT NULL, grams REAL NOT NULL, kcal REAL NOT NULL,"
                " protein_g REAL, carbs_g REAL, fat_g REAL, note TEXT)")
    old.execute("INSERT INTO food_log (ts,measured_on,name,grams,kcal,protein_g)"
                " VALUES ('2026-08-01T12:00:00+01:00','2026-08-01','pão',110,292,9.9)")
    N.ensure(old)
    cols = {r[1] for r in old.execute("PRAGMA table_info(food_log)")}
    check("migration adds meal_id/meal_name",
          {"meal_id", "meal_name"} <= cols, sorted(cols))
    r = old.execute("SELECT * FROM food_log").fetchone()
    check("existing row survives with NULL meal_id",
          r["name"] == "pão" and r["kcal"] == 292 and r["meal_id"] is None, dict(r))
    check("meal table created", bool(old.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meal'").fetchone()))


def test_sizes():
    import datetime
    con = fresh()
    tall, grande, venti = ({"label": "Tall", "grams": 354},
                            {"label": "Grande", "grams": 473},
                            {"label": "Venti", "grams": 591})
    item = N.upsert_item(con, "Starbucks Test Mocha", 83, kind="drink",
                         protein_g=3.6, carbs_g=9.0, fat_g=3.5,
                         portion_g=473, portion_label="grande (473 ml)",
                         sizes=[tall, grande, venti])
    check("sizes round-trip as a list of dicts",
          item["sizes"] == [tall, grande, venti], item["sizes"])

    got = N.get_item(con, item["id"])
    check("get_item decodes sizes", got["sizes"] == [tall, grande, venti], got["sizes"])
    hit = N.search(con, "mocha")[0]
    check("search decodes sizes", hit["sizes"] == [tall, grande, venti], hit["sizes"])

    plain = N.upsert_item(con, "Arroz cozido", 130, portion_g=150)
    check("an item with no sizes decodes to None, not []", plain["sizes"] is None)

    # An update that doesn't mention sizes must not wipe them.
    bumped = N.upsert_item(con, "Starbucks Test Mocha", 84, kind="drink")
    check("editing other fields leaves sizes untouched",
          bumped["kcal_100g"] == 84.0 and bumped["sizes"] == [tall, grande, venti],
          bumped)

    # Logging at a named size's grams scales exactly like any other portion.
    r = N.log_food(con, food_id=item["id"], grams=venti["grams"],
                   ts=datetime.datetime(2026, 8, 9, 13, 0, tzinfo=N.TZ))
    check("logging the Venti grams scales kcal off the same kcal_100g",
          abs(r["kcal"] - 84.0 * venti["grams"] / 100) < 0.1, r["kcal"])

    cleared = N.upsert_item(con, "Starbucks Test Mocha", 84, kind="drink", sizes=[])
    check("an explicit [] clears sizes rather than being ignored",
          cleared["sizes"] == [], cleared["sizes"])


# ── logging a meal ────────────────────────────────────────────────────────────

def test_log_meal():
    import datetime
    con = fresh()
    rice, chick = seed_two(con)

    # Fixed inside a meal window (lunch) — logging without an explicit slot
    # must not depend on what time the test happens to run.
    out = N.log_meal(con, name="Frango com arroz",
                     ts=datetime.datetime(2026, 8, 9, 13, 0, tzinfo=N.TZ),
                     components=[
        {"food_id": rice["id"],  "grams": 150},
        {"food_id": chick["id"], "grams": 50},
    ])
    check("one row per component", len(out["rows"]) == 2, out["rows"])
    ids = {r["meal_id"] for r in out["rows"]}
    check("all rows share one meal_id", len(ids) == 1 and out["meal_id"] in ids, ids)
    check("meal name denormalised onto each row",
          all(r["meal_name"] == "Frango com arroz" for r in out["rows"]))

    # 150 g rice = 4.05 g protein, 50 g chicken = 15.5 g -> 19.55, rounded per row
    t = N.day_totals(con, out["rows"][0]["measured_on"])
    check("day_totals sums the components untouched",
          abs(t["protein_g"] - 19.6) < 0.15, t)
    check("kcal summed across components",
          abs(t["kcal"] - (195 + 82.5)) <= 1, t)

    # The whole point: the same dish name with the ratio flipped must differ.
    con2 = fresh()
    rice2, chick2 = seed_two(con2)
    out2 = N.log_meal(con2, name="Frango com arroz",
                      ts=datetime.datetime(2026, 8, 9, 13, 0, tzinfo=N.TZ),
                      components=[
        {"food_id": rice2["id"],  "grams": 50},
        {"food_id": chick2["id"], "grams": 150},
    ])
    t2 = N.day_totals(con2, out2["rows"][0]["measured_on"])
    check("flipping the ratio changes protein ~2.4x",
          t2["protein_g"] > t["protein_g"] * 2.3, (t["protein_g"], t2["protein_g"]))
    check("flipping the ratio barely changes calories",
          abs(t2["kcal"] - t["kcal"]) < t["kcal"] * 0.2, (t["kcal"], t2["kcal"]))

    try:
        N.log_meal(con, name="Empty", components=[])
        check("rejects an empty meal", False)
    except ValueError:
        check("rejects an empty meal", True)


def test_delete_meal_log():
    import datetime
    lunch = datetime.datetime(2026, 8, 9, 13, 0, tzinfo=N.TZ)
    con = fresh()
    rice, chick = seed_two(con)
    a = N.log_meal(con, name="Meal A", ts=lunch,
                   components=[{"food_id": rice["id"], "grams": 100}])
    b = N.log_meal(con, name="Meal B", ts=lunch, components=[
        {"food_id": rice["id"], "grams": 100}, {"food_id": chick["id"], "grams": 100}])
    standalone = N.log_food(con, food_id=rice["id"], grams=80, ts=lunch)

    n = N.delete_meal_log(con, b["meal_id"])
    check("removes exactly the rows of that meal", n == 2, n)
    left = {r["id"] for r in con.execute("SELECT id FROM food_log")}
    check("other meal untouched", a["rows"][0]["id"] in left, left)
    check("standalone log untouched", standalone["id"] in left, left)


# ── saved meal templates ──────────────────────────────────────────────────────

def test_saved_meals():
    import datetime
    lunch = datetime.datetime(2026, 8, 9, 13, 0, tzinfo=N.TZ)
    con = fresh()
    rice, chick = seed_two(con)
    comps = [{"food_id": rice["id"], "name": "Arroz cozido", "grams": 150},
             {"food_id": chick["id"], "name": "Frango grelhado", "grams": 120}]

    m = N.save_meal(con, "Frango com arroz", comps)
    check("template stores its components", len(m["components"]) == 2, m)
    check("components round-trip through JSON",
          m["components"][0]["grams"] == 150, m["components"])

    again = N.save_meal(con, "frango com arroz",
                        [{"food_id": rice["id"], "name": "Arroz", "grams": 200}])
    check("re-saving the same name updates rather than duplicating",
          len(N.list_meals(con)) == 1, N.list_meals(con))
    check("updated amounts take effect", again["components"][0]["grams"] == 200)

    N.save_meal(con, "Frango com arroz", comps)   # restore both components
    out = N.log_saved_meal(con, m["id"], ts=lunch)
    check("re-logging a saved meal logs every component", len(out["rows"]) == 2, out)
    check("times_used incremented", N.get_meal(con, m["id"])["times_used"] == 1)
    check("nothing skipped when the library is intact", out["skipped"] == 0)

    try:
        N.save_meal(con, "Bad", [{"name": "no id", "grams": 100}])
        check("template rejects a component with no food_id", False)
    except ValueError:
        check("template rejects a component with no food_id", True)

    # A deleted ingredient must degrade the meal, not break it.
    N.delete_item(con, chick["id"])
    out2 = N.log_saved_meal(con, m["id"], ts=lunch)
    check("deleted component is skipped, not fatal",
          len(out2["rows"]) == 1 and out2["skipped"] == 1, out2)

    N.delete_item(con, rice["id"])
    try:
        N.log_saved_meal(con, m["id"])
        check("meal with no surviving components raises", False)
    except ValueError:
        check("meal with no surviving components raises", True)

    check("deleting a template leaves logged rows alone",
          (N.delete_meal(con, m["id"]) or True)
          and con.execute("SELECT COUNT(*) FROM food_log").fetchone()[0] == 3)


# ── LLM response parsing ──────────────────────────────────────────────────────

def test_parse_components():
    payload = json.dumps({
        "name": "Frango com arroz", "kind": "food", "confidence": "medium",
        "note": "portions assumed",
        "components": [
            {"name": "Arroz cozido", "grams": 150, "kcal_100g": 130,
             "protein_g": 2.7, "carbs_g": 28, "fat_g": 0.3},
            {"name": "Frango grelhado", "grams": 120, "kcal_100g": 165,
             "protein_g": 31, "carbs_g": 0, "fat_g": 3.6},
        ]})
    r = N._parse(payload, "frango com arroz")
    check("components parsed", len(r["components"]) == 2, r)
    check("per-component grams kept", r["components"][0]["grams"] == 150)
    check("est_kcal is the sum of the parts",
          r["est_kcal"] == round(130 * 1.5 + 165 * 1.2), r["est_kcal"])
    check("confidence and note preserved",
          r["confidence"] == "medium" and r["note"] == "portions assumed")

    # A single item must still parse exactly as before.
    single = json.dumps({"name": "Pão com ovo", "kind": "food", "kcal_100g": 265,
                         "protein_g": 9, "carbs_g": 32, "fat_g": 11,
                         "portion_g": 110, "portion_label": "1 unidade",
                         "confidence": "high"})
    s = N._parse(single, "pão com ovo")
    check("single item still parses", s["kcal_100g"] == 265 and s["components"] is None, s)
    check("single item est_kcal unchanged", s["est_kcal"] == round(265 * 1.1))

    # Junk components are dropped rather than poisoning the meal.
    messy = json.dumps({
        "name": "Mistura", "kind": "food",
        "components": [
            {"name": "Bom", "grams": 100, "kcal_100g": 200, "protein_g": 10},
            {"name": "Sem kcal", "grams": 100},
            {"name": "", "grams": 50, "kcal_100g": 100},
            {"name": "Grams absurdos", "grams": 99999, "kcal_100g": 100},
            {"name": "Kcal absurdo", "grams": 100, "kcal_100g": 5000},
        ]})
    try:
        m = N._parse(messy, "mistura")
        check("all-but-one invalid falls back or raises",
              m.get("components") is None, m)
    except RuntimeError:
        # Only one valid component and no top-level kcal_100g -> correctly refuses.
        check("all-but-one invalid falls back or raises", True)

    # Two valid + junk -> keeps the two.
    mixed = json.dumps({
        "name": "Dois", "kind": "food",
        "components": [
            {"name": "A", "grams": 100, "kcal_100g": 200, "protein_g": 10},
            {"name": "B", "grams": 100, "kcal_100g": 150, "protein_g": 5},
            {"name": "Lixo", "grams": 100},
        ]})
    mx = N._parse(mixed, "dois")
    check("invalid component dropped, valid ones kept",
          len(mx["components"]) == 2, mx["components"])

    # Never more than 4 components.
    many = json.dumps({"name": "Muitos", "kind": "food", "components": [
        {"name": f"C{i}", "grams": 50, "kcal_100g": 100} for i in range(8)]})
    check("component list capped at 4", len(N._parse(many, "muitos")["components"]) == 4)


def test_meal_slots():
    import datetime
    TZ = N.TZ

    def at(h, m=0):
        return datetime.datetime(2026, 8, 9, h, m, tzinfo=TZ)

    # Windows are not contiguous — the gaps between them (want="off_hours")
    # are the point: a time that lands in one is not any of the 5 real meals.
    cases = [(6, 0, "breakfast"), (7, 30, "breakfast"), (9, 29, "breakfast"),
             (9, 30, "off_hours"), (9, 59, "off_hours"),
             (10, 0, "morning_snack"), (11, 29, "morning_snack"),
             (11, 30, "off_hours"), (11, 59, "off_hours"),
             (12, 0, "lunch"), (14, 29, "lunch"),
             (14, 30, "off_hours"), (15, 59, "off_hours"),
             (16, 0, "afternoon_snack"), (17, 59, "afternoon_snack"),
             (18, 0, "off_hours"), (19, 29, "off_hours"),
             (19, 30, "dinner"), (21, 59, "dinner"),
             (22, 0, "off_hours"), (23, 59, "off_hours"),
             (0, 0, "off_hours"), (5, 59, "off_hours")]
    for h, m, want in cases:
        got = N.slot_for_time(at(h, m))
        check(f"{h:02d}:{m:02d} -> {want}", got == want, got)

    con0 = fresh()
    r0, _ = seed_two(con0)
    r0a = N.log_food(con0, food_id=r0["id"], grams=100, ts=at(15, 0))
    check("logging in a gap without a slot lands in off_hours",
          r0a["meal_slot"] == "off_hours", r0a["meal_slot"])
    r0b = N.log_food(con0, food_id=r0["id"], grams=100, ts=at(15, 0), slot="lunch")
    check("an explicit slot still works inside a gap",
          r0b["meal_slot"] == "lunch", r0b["meal_slot"])

    con = fresh()
    rice, chick = seed_two(con)

    r = N.log_food(con, food_id=rice["id"], grams=100, ts=at(8, 0))
    check("slot stored from the clock", r["meal_slot"] == "breakfast", r["meal_slot"])

    r2 = N.log_food(con, food_id=rice["id"], grams=100, ts=at(8, 0), slot="dinner")
    check("explicit slot overrides the clock", r2["meal_slot"] == "dinner", r2["meal_slot"])

    try:
        N.log_food(con, food_id=rice["id"], grams=100, ts=at(8, 0), slot="brunch")
        check("rejects an unknown slot", False)
    except ValueError:
        check("rejects an unknown slot", True)

    # A dish must never be split across two meals.
    meal = N.log_meal(con, name="Frango com arroz", ts=at(13, 0), components=[
        {"food_id": rice["id"], "grams": 150}, {"food_id": chick["id"], "grams": 120}])
    check("every component of a dish shares one slot",
          {x["meal_slot"] for x in meal["rows"]} == {"lunch"}, meal["rows"])

    N.set_slot(con, r["id"], "afternoon_snack")
    moved = con.execute("SELECT meal_slot FROM food_log WHERE id=?", (r["id"],)).fetchone()[0]
    check("set_slot moves an entry", moved == "afternoon_snack", moved)
    try:
        N.set_slot(con, r["id"], "nope")
        check("set_slot validates", False)
    except ValueError:
        check("set_slot validates", True)

    # Grouped read.
    day = N.day_by_slot(con, "2026-08-09")
    check("always returns all six slots", len(day) == 6, [d["slot"] for d in day])
    check("slots come back in day order",
          [d["slot"] for d in day] == list(N.MEAL_SLOTS), [d["slot"] for d in day])
    lunch = next(d for d in day if d["slot"] == "lunch")
    check("dish lands in one slot with both parts", len(lunch["entries"]) == 2, lunch)
    check("slot totals add up",
          abs(lunch["protein_g"] - (2.7 * 1.5 + 31.0 * 1.2)) < 0.2, lunch["protein_g"])
    check("empty slot still present with zero totals",
          next(d for d in day if d["slot"] == "breakfast")["kcal"] == 0)
    # Slot totals must reconcile with the untouched day total.
    tot = N.day_totals(con, "2026-08-09")
    check("slot totals sum to the day total",
          abs(sum(d["protein_g"] for d in day) - tot["protein_g"]) < 0.2,
          (sum(d["protein_g"] for d in day), tot["protein_g"]))


def test_slot_backfill():
    old = sqlite3.connect(":memory:")
    old.row_factory = sqlite3.Row
    old.execute("CREATE TABLE food_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts TEXT NOT NULL, measured_on TEXT NOT NULL, food_id INTEGER,"
                " name TEXT NOT NULL, grams REAL NOT NULL, kcal REAL NOT NULL,"
                " protein_g REAL, carbs_g REAL, fat_g REAL, note TEXT)")
    for ts, nm in (("2026-08-09T08:15:00+01:00", "pequeno almoço"),
                   ("2026-08-09T13:40:00+01:00", "almoço"),
                   ("2026-08-09T21:10:00+01:00", "jantar")):
        old.execute("INSERT INTO food_log (ts,measured_on,name,grams,kcal)"
                    " VALUES (?,?,?,100,200)", (ts, ts[:10], nm))
    N.ensure(old)
    got = {r["name"]: r["meal_slot"] for r in old.execute("SELECT name, meal_slot FROM food_log")}
    check("existing rows filed from their own timestamp",
          got == {"pequeno almoço": "breakfast", "almoço": "lunch", "jantar": "dinner"}, got)

    # Idempotent, and never overwrites a correction.
    old.execute("UPDATE food_log SET meal_slot='morning_snack' WHERE name='almoço'")
    N.ensure(old)
    kept = old.execute("SELECT meal_slot FROM food_log WHERE name='almoço'").fetchone()[0]
    check("backfill never overwrites an existing slot", kept == "morning_snack", kept)
