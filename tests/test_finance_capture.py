"""The write-only capture path (finance.capture + /api/finance/capture[/undo], /api/food/capture).

Everything runs against a throwaway database, a throwaway events.json and a stubbed
ingest (see conftest.py).
"""
import datetime
import json
import sqlite3

from acta import config
from acta.api.routers import capture as capture_api
from acta.finance import capture as fc
from acta.tracking import nutrition
from conftest import check


def test_capture_path(client, data_dir, monkeypatch):
    ingests = []
    monkeypatch.setattr(capture_api, "run_ingest_quietly", lambda: ingests.append(1))
    monkeypatch.setattr(fc, "MAX_PER_DAY", fc.MAX_PER_DAY)
    capture_api._capture_fail.clear()
    TZ = config.TZ
    now = datetime.datetime.now(TZ)
    today = now.date().isoformat()

    def db():
        c = sqlite3.connect(config.ACTA_DB)
        c.row_factory = sqlite3.Row
        return c

    def events():
        return json.load(open(config.EVENTS_PATH))

    def food_rows():
        c = db(); r = [dict(x) for x in c.execute("SELECT * FROM food_log ORDER BY id")]; c.close(); return r

    # ── setup: pin + accounts (through the PIN routes), food library, capture key ──
    client.post("/api/finance/setup-pin", json={"pin": "1234"})
    tok = client.post("/api/finance/unlock", json={"pin": "1234"}).json()["token"]
    H = {"X-Finance-Token": tok}
    card = client.post("/api/finance/accounts", json={"name": "Card", "kind": "bank"}, headers=H).json()["id"]
    cash = client.post("/api/finance/accounts", json={"name": "Cash", "kind": "cash"}, headers=H).json()["id"]
    c = db(); nutrition.ensure(c)
    sizes = [{"label": "Tall", "grams": 354}, {"label": "Grande", "grams": 473}, {"label": "Venti", "grams": 591}]
    nutrition.upsert_item(c, "Starbucks Caffè Mocha", 83, kind="drink", portion_g=473, portion_label="grande (473 ml)", sizes=sizes)
    nutrition.upsert_item(c, "Starbucks Caffè Latte", 44, kind="drink", portion_g=473, portion_label="grande (473 ml)", sizes=sizes)
    nutrition.upsert_item(c, "Starbucks Egg Sandwich", 327.6, kind="food", portion_g=140, portion_label="1 sandwich (~140 g est.)")
    nutrition.upsert_item(c, "Mocha Cake", 400, kind="food", portion_g=100)
    nutrition.upsert_item(c, "Eggs", 143, kind="food", portion_g=50)
    nutrition.upsert_item(c, "Beer", 43, kind="drink", portion_g=330, abv_pct=5.0)
    nutrition.upsert_item(c, "Coffee", 2, kind="drink", portion_g=200, portion_label="1 cup")
    c.commit(); c.close()

    def cap(body, key="test-key"):
        return client.post("/api/finance/capture", json=body, headers={"X-Capture-Key": key} if key else {})

    def undo(i, key="test-key"):
        return client.post("/api/finance/capture/undo", json={"id": i}, headers={"X-Capture-Key": key})

    check("capture is refused until a key exists", cap({"amount_eur": 1, "merchant": "x"}).status_code == 503)
    c = db(); c.execute("INSERT INTO finance_config(key,value) VALUES('capture_key_hash',?)", (fc.key_hash("test-key"),)); c.commit(); c.close()
    check("no key is refused", cap({"amount_eur": 1, "merchant": "x"}, key=None).status_code == 401)
    check("a wrong key is refused", cap({"amount_eur": 1, "merchant": "x"}, key="nope").status_code == 401)
    check("the finance PIN token is not a capture key", client.post("/api/finance/capture", json={"amount_eur": 1, "merchant": "x"}, headers=H).status_code == 401)
    check("the capture key cannot read finance data", client.get("/api/finance/spending", headers={"X-Capture-Key": "test-key"}).status_code == 401)
    check("the capture key cannot edit a purchase", client.patch("/api/finance/transaction/1", json={"note": "x"}, headers={"X-Capture-Key": "test-key"}).status_code == 401)
    check("the stored value is a hash, not the key", "test-key" not in open(config.ACTA_DB, "rb").read().decode("latin1"))
    capture_api._capture_fail.clear()
    for _ in range(10):
        cap({"amount_eur": 1, "merchant": "x"}, key="bad")
    check("ten wrong keys in a minute lock the door", cap({"amount_eur": 1, "merchant": "x"}).status_code == 429)
    capture_api._capture_fail.clear()
    check("the right key works again once the window passes", cap({"amount_eur": 1, "merchant": "warm-up"}).status_code == 200)

    for label, body in (("zero", {"amount_eur": 0, "merchant": "x"}), ("negative", {"amount_eur": -3, "merchant": "x"}),
                        ("over the cap", {"amount_eur": 200.01, "merchant": "x"}), ("blank merchant", {"amount_eur": 3, "merchant": "  "}),
                        ("future date", {"amount_eur": 3, "merchant": "x", "date": "2999-01-01"}),
                        ("very old date", {"amount_eur": 3, "merchant": "x", "date": "2020-01-01"}),
                        ("bad date", {"amount_eur": 3, "merchant": "x", "date": "yesterday"}),
                        ("bad time", {"amount_eur": 3, "merchant": "x", "time": "noon"}),
                        ("unknown category", {"amount_eur": 3, "merchant": "x", "category": "snacks"}),
                        ("unknown account", {"amount_eur": 3, "merchant": "x", "account": "Nowhere"})):
        check(f"refused: {label}", cap(body).status_code == 400, cap(body).text)
    r = cap({"amount_eur": 3, "merchant": "x", "account": "Nowhere"}).json()
    check("an unknown account lists the options", "Card" in r["detail"] and "Cash" in r["detail"], r)
    check("the boundary amount (200.00) is allowed", cap({"amount_eur": 200, "merchant": "Big one"}).status_code == 200)

    r = cap({"amount_eur": 1.4, "merchant": "Delta Café", "account": "cash", "category": "coffee"}).json()
    check("'cash' means the cash account", r["row"]["account_id"] == cash, r["row"])
    r = cap({"amount_eur": 2, "merchant": "Padaria", "account": "car"}).json()
    check("a unique part of a name is enough", r["row"]["account_id"] == card)
    r = cap({"amount_eur": 3, "merchant": "Kiosk"}).json()
    check("no account means the last one used", r["row"]["account_id"] in (card, cash))
    r = cap({"amount_eur": 1.3, "merchant": "Delta Café"}).json()
    check("a known merchant brings its category", r["row"]["category"] == "coffee", r["row"])
    r = cap({"amount_eur": 4, "merchant": "Brand new place"}).json()
    check("an unknown merchant is left uncategorised", r["row"]["category"] is None)
    row = r["row"]
    check("captured rows are expense, pending, whatsapp", row["amount_eur"] == -4.0 and row["status"] == "pending" and row["source"] == "whatsapp", row)

    n0, e0 = len(food_rows()), len(events())
    r = cap({"amount_eur": 4.5, "merchant": "Starbucks", "category": "coffee"}).json()
    check("no item named: ledger only, food untouched", r["food"]["status"] == "none" and len(food_rows()) == n0 and len(events()) == e0, r["food"])

    ing0 = len(ingests)
    r = cap({"amount_eur": 4.10, "merchant": "Starbucks", "item": "mocha"}).json()
    mocha_id = r["id"]
    f = r["food"]
    check("Starbucks + mocha: the category comes from the merchant, the food from the library", r["row"]["category"] == "coffee" and f["status"] == "logged" and f["name"] == "Starbucks Caffè Mocha", r)
    check("the food entry is the library's kcal times its stored portion", f["grams"] == 473 and f["kcal"] == round(83 * 4.73, 1) and f["assumed_size"] is True, f)
    check("the food row links back to the purchase", food_rows()[-1]["note"] == f"via purchase #{mocha_id}" and food_rows()[-1]["kcal"] == f["kcal"])
    ev = events()[-1]
    check("a caffeinated drink is counted as a coffee", f["counted_as_coffee"] is True and ev["type"] == "coffee" and ev["drink"] == "Starbucks Caffè Mocha", events()[-1:])
    check("ingest is recomputed once for it", len(ingests) == ing0 + 1)
    check("the reply says what happened", "Also added Starbucks Caffè Mocha" in r["message"] and "Counted as a coffee" in r["message"] and f"#{mocha_id}" in r["message"], r["message"])
    check("the ledger row is marked as also in Food", any(x["id"] == mocha_id and x["in_food"] for x in
          client.get("/api/finance/spending", headers=H).json()["rows"]))

    r = cap({"amount_eur": 4.9, "merchant": "Starbucks", "item": "latte", "size": "tall"}).json()
    latte_id = r["id"]
    check("a named size is used, and is not 'assumed'", r["food"]["grams"] == 354 and r["food"]["assumed_size"] is False and r["food"]["kcal"] == round(44 * 3.54, 1), r["food"])
    r = cap({"amount_eur": 4.9, "merchant": "Starbucks", "item": "caffe latte", "size": "huge"}).json()
    check("the same latte within 30 minutes is not logged twice", r["food"]["status"] == "skipped" and "already logged" in r["food"]["reason"], r["food"])

    r = cap({"amount_eur": 1, "merchant": "Lidl", "item": "eggs", "category": "groc"}).json()
    check("groceries never touch food, whatever the wording", r["food"]["status"] == "skipped" and "coffee and eating-out" in r["food"]["reason"] and len([x for x in food_rows() if x["name"] == "Eggs"]) == 0, r["food"])
    r = cap({"amount_eur": 1, "merchant": "Lidl Sul", "item": "eggs"}).json()
    check("no category also means no food", r["food"]["status"] == "skipped" and "no category" in r["food"]["reason"], r["food"])
    r = cap({"amount_eur": 6, "merchant": "Pastelaria X", "item": "banoffee pie", "category": "eat"}).json()
    check("an item that is not in the library is not invented", r["food"]["status"] == "skipped" and "not in your food list" in r["food"]["reason"], r["food"])
    check("...but the purchase itself is still logged", r["row"]["amount_eur"] == -6.0 and r["id"] > 0)
    r = cap({"amount_eur": 4, "merchant": "Nicola", "item": "mocha", "category": "coffee"}).json()
    check("an ambiguous item is not guessed", r["food"]["status"] == "skipped" and "Mocha Cake" in r["food"]["reason"], r["food"])
    r = cap({"amount_eur": 3, "merchant": "Bar", "item": "beer", "category": "eat"}).json()
    check("alcohol is left to be logged by hand", r["food"]["status"] == "skipped" and "alcoholic" in r["food"]["reason"], r["food"])
    yday = (now.date() - datetime.timedelta(days=1)).isoformat()
    r = cap({"amount_eur": 4, "merchant": "Starbucks", "item": "espresso", "date": yday, "category": "coffee"}).json()
    check("a purchase from another day is not added to today's food", r["food"]["status"] == "skipped" and "not a purchase from today" in r["food"]["reason"] and r["row"]["occurred_on"] == yday, r["food"])
    n1, e1 = len(food_rows()), len(events())
    r = cap({"amount_eur": 5, "merchant": "Starbucks", "item": "egg sandwich", "category": "eat"}).json()
    check("food that is not a coffee is logged without a coffee event", r["food"]["status"] == "logged" and r["food"]["counted_as_coffee"] is False and len(events()) == e1 and len(food_rows()) == n1 + 1, r["food"])

    c = db(); c.execute("DELETE FROM food_log WHERE name='Coffee'"); c.commit(); c.close()
    near = (now - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    ev_all = events() + [{"datetime": near, "type": "coffee"}]
    ev_all.sort(key=lambda e: e["datetime"]); json.dump(ev_all, open(config.EVENTS_PATH, "w"))
    e2 = len(events())
    r = cap({"amount_eur": 1.2, "merchant": "Delta Café", "item": "coffee", "category": "coffee"}).json()
    check("a coffee reported minutes ago is not counted twice", r["food"]["status"] == "logged" and r["food"]["counted_as_coffee"] is False and len(events()) == e2, r["food"])
    dup_id = r["id"]

    ing1 = len(ingests)
    r = undo(mocha_id).json()
    check("undo removes the purchase, its food entry and its coffee count", r["status"] == "ok" and r["food_removed"] is True and r["coffee_removed"] is True, r)
    check("...from the ledger, the food log and events.json", all(x["id"] != mocha_id for x in client.get("/api/finance/spending", headers=H).json()["rows"])
          and not [x for x in food_rows() if x["note"] == f"via purchase #{mocha_id}"] and not [e for e in events() if e.get("drink") == "Starbucks Caffè Mocha"])
    check("ingest is recomputed after removing the coffee", len(ingests) == ing1 + 1)
    c = db(); st = c.execute("SELECT status FROM finance_tx WHERE id=?", (mocha_id,)).fetchone()[0]; c.close()
    check("the purchase is hidden, never deleted", st == "dismissed")
    check("undoing twice is harmless", undo(mocha_id).json().get("already") is True)
    r = undo(dup_id).json()
    check("undo leaves a coffee it did not count alone", r["food_removed"] is True and r["coffee_removed"] is False and any(e["datetime"] == near for e in events()), r)
    web = client.post("/api/finance/transaction", json={"account_id": card, "amount_eur": -9, "description": "Web"}, headers=H).json()["id"]
    check("a purchase added on the dashboard cannot be undone by the assistant", undo(web).status_code == 404)
    check("an unknown id is a 404", undo(99999).status_code == 404)
    c = db(); c.execute("UPDATE finance_tx SET status='applied' WHERE id=?", (latte_id,)); c.commit(); c.close()
    check("a purchase already counted in a confirmed balance is refused", undo(latte_id).status_code == 409)
    old = cap({"amount_eur": 2, "merchant": "Old"}).json()["id"]
    c = db(); c.execute("UPDATE finance_tx SET created_at=? WHERE id=?", ((now - datetime.timedelta(days=2)).isoformat(), old)); c.commit(); c.close()
    check("an entry older than a day is refused", undo(old).status_code == 409)
    sand = [x for x in food_rows() if x["name"] == "Starbucks Egg Sandwich"][-1]
    c = db(); sid = c.execute("SELECT id FROM finance_tx WHERE food_link LIKE ?", (f'%"log_id": {sand["id"]}%',)).fetchone()[0]
    c.execute("UPDATE food_log SET note='edited by hand' WHERE id=?", (sand["id"],)); c.commit(); c.close()
    r = undo(sid).json()
    check("a food entry that was changed by hand is left alone", r["food_removed"] is False and any(x["id"] == sand["id"] for x in food_rows()), r)


    def food(body, key="test-key"):
        return client.post("/api/food/capture", json=body, headers={"X-Capture-Key": key} if key else {})

    def food_undo(i, key="test-key"):
        return client.post("/api/food/capture/undo", json={"log_id": i}, headers={"X-Capture-Key": key})

    check("food capture needs the capture key too", food({"item": "eggs"}, key=None).status_code == 401)
    check("the finance PIN token is not a capture key here either", client.post("/api/food/capture", json={"item": "eggs"}, headers=H).status_code == 401)
    check("item is required", food({}).status_code == 400)
    check("blank item is refused", food({"item": "   "}).status_code == 400)
    check("bad time is refused", food({"item": "eggs", "time": "noon"}).status_code == 400)
    check("an item not in the library is not invented", "not in your food list" in food({"item": "banoffee pie"}).json()["detail"])
    check("an ambiguous item is not guessed", "Mocha Cake" in food({"item": "mocha"}).json()["detail"])
    check("alcohol is left to be logged by hand", "alcoholic" in food({"item": "beer"}).json()["detail"])

    json.dump([], open(config.EVENTS_PATH, "w"))   # clear leftover events from earlier sections
    c = db(); nutrition.upsert_item(c, "Test Frappuccino", 60, kind="drink", portion_g=400, portion_label="grande (400 ml)",
                                    sizes=[{"label": "Tall", "grams": 300}, {"label": "Grande", "grams": 400}])
    c.commit(); c.close()

    nf0 = len(food_rows())
    ntx0 = len(client.get("/api/finance/spending", headers=H).json()["rows"])
    r = food({"item": "caffe mocha", "merchant": "Starbucks"}).json()
    check("a plain food capture writes no finance_tx row", len(food_rows()) == nf0 + 1
          and len(client.get("/api/finance/spending", headers=H).json()["rows"]) == ntx0)
    check("named merchant breaks the mocha/Mocha-Cake tie", r["name"] == "Starbucks Caffè Mocha", r)
    check("no size given uses the stored typical portion, flagged assumed", r["assumed_size"] is True and r["grams"] == 473, r)
    check("kcal is the library's own number, not invented", r["kcal"] == round(83 * 4.73, 1), r)
    check("it is marked as a coffee", r["counted_as_coffee"] is True and events()[-1]["type"] == "coffee" and events()[-1]["drink"] == "Starbucks Caffè Mocha", events()[-1:])
    check("the food row is marked as the assistant's own, not a purchase note", food_rows()[-1]["note"] == "via assistant", food_rows()[-1])
    mocha_log_id = r["log_id"]

    r2 = food({"item": "caffe mocha", "merchant": "Starbucks"}).json()
    check("the same drink again within 30 minutes is refused, not silently dropped", "already logged" in r2.get("detail", ""), r2)

    r3 = food({"item": "frappuccino", "size": "tall"}).json()
    check("a named size is used and not flagged assumed", r3["grams"] == 300 and r3["assumed_size"] is False, r3)

    ing2 = len(ingests)
    ur = food_undo(mocha_log_id).json()
    check("undo removes the food row and its coffee count", ur["status"] == "ok" and ur["coffee_removed"] is True, ur)
    check("...gone from food_log and events.json", not [x for x in food_rows() if x["id"] == mocha_log_id]
          and not [e for e in events() if e.get("drink") == "Starbucks Caffè Mocha"])
    check("ingest is recomputed after removing the coffee", len(ingests) == ing2 + 1)
    check("undoing twice is a 404, not silently ok", food_undo(mocha_log_id).status_code == 404)
    check("undoing an ordinary dashboard food entry is refused (no assistant marker)", food_undo(sand["id"]).status_code == 404)
    old_food = food({"item": "eggs"}).json()
    c = db(); c.execute("UPDATE food_log SET ts=? WHERE id=?",
                        ((now - datetime.timedelta(hours=25)).isoformat(timespec="seconds"), old_food["log_id"])); c.commit(); c.close()
    check("a food capture older than the undo window is refused", food_undo(old_food["log_id"]).status_code == 409)
    c = db(); c.execute("UPDATE food_log SET note='edited by hand' WHERE id=?", (r3["log_id"],)); c.commit(); c.close()
    check("a food row edited by hand cannot be undone by the assistant", food_undo(r3["log_id"]).status_code == 404)

    c = db(); before = fc.captured_today(c, today); c.close()
    fc.MAX_PER_DAY = before + 2
    a1 = cap({"amount_eur": 1, "merchant": "c1"}); a2 = cap({"amount_eur": 1, "merchant": "c2"}); a3 = cap({"amount_eur": 1, "merchant": "c3"})
    check("the cap allows exactly the limit", a1.status_code == 200 and a2.status_code == 200 and a3.status_code == 429, (a1.status_code, a2.status_code, a3.status_code))
    undo(a1.json()["id"])
    check("undoing does not make room under the cap", cap({"amount_eur": 1, "merchant": "c4"}).status_code == 429)
    check("the app used the throwaway db and events file",
          config.ACTA_DB.startswith(str(data_dir)) and config.EVENTS_PATH.startswith(str(data_dir)))
