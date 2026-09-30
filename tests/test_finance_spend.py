"""Tests for finance.spend and the /api/finance spending endpoints, against
throwaway databases built from scratch with synthetic rows."""
import datetime
import os
import sqlite3
import tempfile

from acta import config
from conftest import check

# ── part 1: the module, against a database that predates the ledger columns ──
BASE_DDL = """
CREATE TABLE finance_account (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'EUR', active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL);
CREATE TABLE finance_tx (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, amount_eur REAL NOT NULL,
    description TEXT, occurred_on TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
    applied_at TEXT, transfer_id TEXT, target TEXT NOT NULL DEFAULT 'balance');
"""


def build_db(path):
    from acta.finance import spend as fs
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(BASE_DDL)
    for name, kind in (("Card", "bank"), ("Cash", "cash"), ("Broker", "stocks")):
        con.execute("INSERT INTO finance_account(name, kind, created_at) VALUES(?,?,'x')", (name, kind))

    def tx(acct, amt, desc, day, status="pending", transfer=None):
        con.execute("INSERT INTO finance_tx(account_id, amount_eur, description, occurred_on, status, created_at, transfer_id) "
                    "VALUES(?,?,?,?,?, 'x', ?)", (acct, amt, desc, day, status, transfer))
    # a row that existed before the ledger columns were added
    tx(1, -4.50, "Old coffee", "2026-08-03", "applied")
    con.commit()
    fs.ensure(con)
    fs.ensure(con)          # idempotent
    con.commit()
    return con, tx


def test_spend_module():
    from acta.finance import spend as fs
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    try:
        con, tx = build_db(path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(finance_tx)")}
        check("ensure adds category/source/note", {"category", "source", "note"} <= cols)
        old = con.execute("SELECT * FROM finance_tx WHERE description='Old coffee'").fetchone()
        check("existing row keeps its values, source defaults to manual",
              old["amount_eur"] == -4.50 and old["status"] == "applied" and old["source"] == "manual" and old["category"] is None)

        def add(acct, amt, desc, day, cat=None, status="pending", transfer=None):
            con.execute("INSERT INTO finance_tx(account_id, amount_eur, description, occurred_on, status, created_at, category, transfer_id) "
                        "VALUES(?,?,?,?,?,'x',?,?)", (acct, amt, desc, day, status, cat, transfer))
        # August (full month) and September up to the 19th
        add(1, -10.00, "Lidl", "2026-08-10", "groc", "applied")
        add(1, -30.00, "Lidl", "2026-08-25", "groc", "applied")          # after day 19: excluded from the like-for-like comparison
        add(1, -4.80, "Starbucks", "2026-09-04", "coffee")
        add(1, -4.50, "Starbucks", "2026-09-07", "coffee")
        add(2, -6.80, "Kiosk", "2026-09-10")                             # uncategorised expense
        add(1, -40.00, "Shoes", "2026-09-12", "shop")
        add(1, 12.90, "Refund", "2026-09-16", "shop")                    # refund: lowers shopping
        add(2, 35.50, "Friend paid back", "2026-09-18")                  # income: not spending
        add(1, -300.00, "To savings", "2026-09-15", None, "pending", "T1")   # transfer legs: not spending
        add(3, 300.00, "From card", "2026-09-15", None, "pending", "T1")
        add(1, -99.00, "Mistake", "2026-09-19", "other", "dismissed")   # dismissed: hidden
        con.commit()

        today = datetime.date(2026, 9, 19)
        s = fs.summary(con, "2026-09", today, derived_ids=[3], recurring={"monthly_out": 148.47, "count": 7})
        # Sept: 4.80 + 4.50 + 6.80 + 40.00 - 12.90 = 43.20
        check("spent is net of refunds, excludes income, transfers and dismissed", s["spent"] == 43.20, s["spent"])
        check("daily average over the days elapsed", s["days_elapsed"] == 19 and s["daily_avg"] == round(43.20 / 19, 2), s["daily_avg"])
        # August through day 19: old coffee 4.50 + 10.00 = 14.50 (the 30.00 on the 25th is after day 19)
        check("compares with the same days of last month", s["compare"] and s["compare"]["spent"] == 14.50 and s["compare"]["same_days"], s["compare"])
        check("delta is this month minus last month's same days", s["compare"]["delta"] == round(43.20 - 14.50, 2))
        cats = {c["category"]: c["amount"] for c in s["by_category"]}
        check("category split nets the refund", cats.get("shop") == 27.10 and cats.get("coffee") == 9.30, cats)
        check("uncategorised spend is reported as 'none'", cats.get("none") == 6.80)
        check("plain income never appears in the split", all(c["amount"] > 0 for c in s["by_category"]) and 35.50 not in cats.values())
        check("largest category first", s["by_category"][0]["category"] == "shop")
        check("needs_category counts expenses only", s["needs_category"] == 1, s["needs_category"])
        check("transfers reported apart", s["transfers"] == {"count": 1, "total": 300.0}, s["transfers"])
        check("rows exclude transfers and dismissed", all(r["description"] not in ("To savings", "From card", "Mistake") for r in s["rows"]))
        check("rows are newest first", [r["occurred_on"] for r in s["rows"]] == sorted((r["occurred_on"] for r in s["rows"]), reverse=True))
        check("picker offers no holdings-derived account", [a["name"] for a in s["accounts"]] == ["Card", "Cash"], s["accounts"])
        check("default account is the last one used", s["default_account_id"] in (1, 2))
        m = {x["name"]: x for x in s["merchants"]}
        check("merchant memory keeps the last category", m["Starbucks"]["category"] == "coffee" and m["Starbucks"]["n"] == 2, m.get("Starbucks"))
        check("months run from the first entry to now", s["months"] == ["2026-08", "2026-09"], s["months"])
        s8 = fs.summary(con, "2026-08", today)
        check("a finished month uses all its days", s8["days_elapsed"] == 31 and s8["spent"] == 44.50, (s8["days_elapsed"], s8["spent"]))
        check("no prior month means no comparison", s8["compare"] is None)

        for bad in ("nope", "Coffee"):
            try:
                fs.clean_category(bad); ok = False
            except ValueError:
                ok = True
            check(f"unknown category '{bad}' rejected", ok)
        check("empty category clears", fs.clean_category("") is None and fs.clean_category(None) is None)
        try:
            fs.clean_source("email"); ok = False
        except ValueError:
            ok = True
        check("unknown source rejected", ok)
        con.close()
    finally:
        os.unlink(path)


# ── part 2: the endpoints, through the real app pointed at a throwaway db ──
def test_spending_api(client, data_dir):
    path = config.ACTA_DB
    r = client.post("/api/finance/setup-pin", json={"pin": "1234"})
    r = client.post("/api/finance/unlock", json={"pin": "1234"})
    H = {"X-Finance-Token": r.json()["token"]}
    check("unlocked with the test pin", r.status_code == 200)
    check("spending is PIN-gated", client.get("/api/finance/spending").status_code == 401)

    a1 = client.post("/api/finance/accounts", json={"name": "Card", "kind": "bank"}, headers=H).json()
    a2 = client.post("/api/finance/accounts", json={"name": "Cash", "kind": "cash"}, headers=H).json()
    id1 = a1.get("id") or a1.get("account", {}).get("id")
    id2 = a2.get("id") or a2.get("account", {}).get("id")
    check("two accounts created", id1 and id2, (a1, a2))
    today = datetime.datetime.now(config.TZ).date().isoformat()

    r = client.post("/api/finance/transaction", headers=H, json={
        "account_id": id1, "amount_eur": -4.5, "description": "  Starbucks ", "category": "coffee", "source": "whatsapp", "note": "with a friend"})
    j = r.json()
    check("log with category and source", r.status_code == 200 and j["row"]["category"] == "coffee" and j["row"]["source"] == "whatsapp" and j["row"]["description"] == "Starbucks", j)
    tx_id = j["id"]
    check("unknown category refused", client.post("/api/finance/transaction", headers=H, json={"account_id": id1, "amount_eur": -1, "category": "nope"}).status_code == 400)
    check("unknown source refused", client.post("/api/finance/transaction", headers=H, json={"account_id": id1, "amount_eur": -1, "source": "email"}).status_code == 400)
    check("zero amount refused", client.post("/api/finance/transaction", headers=H, json={"account_id": id1, "amount_eur": 0}).status_code == 400)
    check("future date refused", client.post("/api/finance/transaction", headers=H, json={"account_id": id1, "amount_eur": -1, "occurred_on": "2999-01-01"}).status_code == 400)
    client.post("/api/finance/transaction", headers=H, json={"account_id": id2, "amount_eur": -3.2, "description": "Kiosk"})

    s = client.get("/api/finance/spending", headers=H).json()
    check("month view totals", s["spent"] == 7.7 and s["needs_category"] == 1 and s["month"] == today[:7], (s["spent"], s["needs_category"]))
    check("month view lists rows, accounts, categories, merchants",
          len(s["rows"]) == 2 and len(s["accounts"]) == 2 and len(s["categories"]) == 9 and any(m["name"] == "Starbucks" for m in s["merchants"]))
    check("bad month refused", client.get("/api/finance/spending?month=2026-13", headers=H).status_code == 400)

    r = client.patch(f"/api/finance/transaction/{tx_id}", headers=H, json={"amount_eur": -5.0, "category": "eat", "description": "Lunch"})
    check("edit amount, category and merchant", r.status_code == 200 and r.json()["row"]["amount_eur"] == -5.0 and r.json()["row"]["category"] == "eat", r.text)
    check("clearing the category with an empty string", client.patch(f"/api/finance/transaction/{tx_id}", headers=H, json={"category": ""}).json()["row"]["category"] is None)
    check("edit with nothing to change refused", client.patch(f"/api/finance/transaction/{tx_id}", headers=H, json={}).status_code == 400)
    check("edit to a future date refused", client.patch(f"/api/finance/transaction/{tx_id}", headers=H, json={"occurred_on": "2999-01-01"}).status_code == 400)
    check("edit to an unknown account refused", client.patch(f"/api/finance/transaction/{tx_id}", headers=H, json={"account_id": 999}).status_code == 400)
    check("edit of a missing row is a 404", client.patch("/api/finance/transaction/99999", headers=H, json={"note": "x"}).status_code == 404)

    # dismiss hides it; restore brings it back as pending
    check("dismiss", client.delete(f"/api/finance/transaction/{tx_id}", headers=H).status_code == 200)
    check("dismissed row is hidden from the ledger", all(r["id"] != tx_id for r in client.get("/api/finance/spending", headers=H).json()["rows"]))
    check("dismissed row cannot be edited", client.patch(f"/api/finance/transaction/{tx_id}", headers=H, json={"note": "x"}).status_code == 404)
    r = client.post(f"/api/finance/transaction/{tx_id}/restore", headers=H)
    check("restore returns it as pending", r.status_code == 200 and r.json()["row"]["status"] == "pending", r.text)
    check("restore of a row that is not dismissed is a no-op", client.post(f"/api/finance/transaction/{tx_id}/restore", headers=H).json()["row"]["status"] == "pending")

    # an applied purchase restores as applied
    con = sqlite3.connect(path)
    con.execute("UPDATE finance_tx SET status='applied', applied_at='now' WHERE id=?", (tx_id,)); con.commit(); con.close()
    client.delete(f"/api/finance/transaction/{tx_id}", headers=H)
    check("an applied purchase restores as applied", client.post(f"/api/finance/transaction/{tx_id}/restore", headers=H).json()["row"]["status"] == "applied")

    # transfers stay out of the ledger and cannot be edited as purchases
    r = client.post("/api/finance/transfer", headers=H, json={"from_account_id": id1, "to_account_id": id2, "amount_eur": 50})
    check("transfer created", r.status_code == 200, r.text)
    s = client.get("/api/finance/spending", headers=H).json()
    check("transfer is not spending and is reported apart", s["spent"] == 8.2 and s["transfers"]["count"] == 1 and s["transfers"]["total"] == 50.0, (s["spent"], s["transfers"]))
    leg = client.get("/api/finance/transactions?status=all", headers=H).json()["transactions"]
    leg = [t for t in leg if t.get("transfer_id")][0]
    check("a transfer leg cannot be edited as a purchase", client.patch(f"/api/finance/transaction/{leg['id']}", headers=H, json={"note": "x"}).status_code == 409)
    check("the old list endpoint still works and carries the new columns", "category" in leg and "source" in leg)
