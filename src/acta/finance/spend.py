"""Spending ledger on top of finance_tx.

A purchase is a `finance_tx` row. Nothing new is stored about balances: a
purchase still only *proposes* an expected balance (status `pending`) until I
confirm the real one (`applied`), exactly as before. This module adds
what a ledger needs on top of that: a category, a source (where the entry came
from), a note, and the queries that turn rows into a month view.

Rules the queries encode:
  * The ledger is every non-dismissed row that is not a transfer leg. Transfers
    between my own accounts are not spending.
  * Spending is the net of purchases and refunds: a negative amount is spend; a
    positive amount that carries a category is a refund and lowers that
    category; a positive amount with no category is income and is not counted.
  * Dismissed rows are hidden, never deleted (same rule as the rest of finance).
  * Subscriptions are not in finance_tx; they are reported separately
    ("recurring") so a charge is never counted twice.

Everything here takes an open sqlite connection so it can be tested against a
throwaway database without importing the API.
"""
import calendar
import datetime
from typing import Optional

# Fixed on purpose: a short list is what keeps logging a five-second job. The
# API is the single source; the dashboard reads this list from the response.
CATEGORIES = [
    ("coffee", "Coffee & snacks"),
    ("eat", "Eating out"),
    ("groc", "Groceries"),
    ("move", "Transport"),
    ("shop", "Shopping"),
    ("health", "Health"),
    ("fun", "Fun"),
    ("bills", "Bills"),
    ("other", "Other"),
]
CATEGORY_KEYS = {k for k, _ in CATEGORIES}
SOURCES = ("manual", "whatsapp", "sync", "import")

LEDGER = "t.status != 'dismissed' AND t.transfer_id IS NULL"


def ensure(con) -> None:
    """Add the ledger columns to finance_tx. Purely additive: existing rows keep
    every value they had and get source='manual', category NULL."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(finance_tx)")}
    # food_link: JSON {log_id, event_dt, event_drink} when a purchase also created a
    # food entry (and maybe a coffee event) through the assistant capture path.
    for col, decl in (("category", "TEXT"),
                      ("source", "TEXT NOT NULL DEFAULT 'manual'"),
                      ("note", "TEXT"),
                      ("food_link", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE finance_tx ADD COLUMN {col} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_finance_tx_ledger "
                "ON finance_tx(occurred_on, status)")


# ── validation ─────────────────────────────────────────────────────────────
def clean_text(s: Optional[str], limit: int) -> Optional[str]:
    s = (s or "").strip()
    return s[:limit] if s else None


def clean_category(c: Optional[str]) -> Optional[str]:
    """None / '' clear the category; anything else must be a known key."""
    c = (c or "").strip()
    if not c:
        return None
    if c not in CATEGORY_KEYS:
        raise ValueError(f"unknown category '{c}'")
    return c


def clean_source(s: Optional[str]) -> str:
    s = (s or "manual").strip()
    if s not in SOURCES:
        raise ValueError(f"unknown source '{s}'")
    return s


# ── month arithmetic ───────────────────────────────────────────────────────
def parse_month(ym: Optional[str], today: datetime.date) -> str:
    if not ym:
        return today.strftime("%Y-%m")
    try:
        datetime.datetime.strptime(ym, "%Y-%m")
    except ValueError:
        raise ValueError("month must be YYYY-MM") from None
    return ym


def _bounds(ym: str) -> tuple:
    y, m = int(ym[:4]), int(ym[5:])
    last = calendar.monthrange(y, m)[1]
    return f"{ym}-01", f"{ym}-{last:02d}", last


def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def net(rows) -> float:
    """Spending net of refunds; plain income (positive, no category) excluded."""
    return round(sum(-r["amount_eur"] for r in rows
                     if r["amount_eur"] < 0 or r["category"]), 2)


# ── rows ───────────────────────────────────────────────────────────────────
def _row(r) -> dict:
    return {
        "id": r["id"], "account_id": r["account_id"], "account": r["account_name"],
        "amount_eur": r["amount_eur"], "description": r["description"],
        "category": r["category"], "occurred_on": r["occurred_on"],
        "status": r["status"], "source": r["source"], "note": r["note"],
        "logged_at": r["created_at"], "in_food": bool(r["food_link"]),
    }


def _month_rows(con, ym: str, up_to_day: Optional[int] = None) -> list:
    start, end, _ = _bounds(ym)
    if up_to_day is not None:
        end = f"{ym}-{up_to_day:02d}"
    return con.execute(
        "SELECT t.*, a.name AS account_name FROM finance_tx t "
        "JOIN finance_account a ON a.id = t.account_id "
        f"WHERE {LEDGER} AND t.occurred_on BETWEEN ? AND ? "
        "ORDER BY t.occurred_on DESC, t.id DESC", (start, end)).fetchall()


def get_row(con, tx_id: int) -> Optional[dict]:
    r = con.execute(
        "SELECT t.*, a.name AS account_name FROM finance_tx t "
        "JOIN finance_account a ON a.id = t.account_id WHERE t.id = ?", (tx_id,)).fetchone()
    return _row(r) if r else None


# ── the month view ─────────────────────────────────────────────────────────
def merchants(con, limit: int = 40) -> list:
    """Most-used merchants with the category last given to each, so the sheet
    can suggest a category the moment a known merchant is typed."""
    out = []
    for r in con.execute(
            "SELECT MIN(TRIM(t.description)) AS name, COUNT(*) AS n "
            f"FROM finance_tx t WHERE {LEDGER} AND t.description IS NOT NULL "
            "AND TRIM(t.description) != '' GROUP BY LOWER(TRIM(t.description)) "
            "ORDER BY n DESC, MAX(t.id) DESC LIMIT ?", (limit,)):
        cat = con.execute(
            "SELECT category FROM finance_tx t "
            f"WHERE {LEDGER} AND LOWER(TRIM(t.description)) = LOWER(?) AND t.category IS NOT NULL "
            "ORDER BY t.occurred_on DESC, t.id DESC LIMIT 1", (r["name"],)).fetchone()
        out.append({"name": r["name"], "n": r["n"], "category": cat["category"] if cat else None})
    return out


def picker_accounts(con, derived_ids) -> list:
    """Accounts a purchase can be logged against: active and not valued from
    holdings (a spend there is a sell, which is a holdings edit)."""
    derived = set(derived_ids)
    return [{"id": r["id"], "name": r["name"], "kind": r["kind"]}
            for r in con.execute(
                "SELECT id, name, kind FROM finance_account WHERE active = 1 "
                "ORDER BY sort_order, id") if r["id"] not in derived]


def default_account_id(con, accounts) -> Optional[int]:
    """The account last used for a purchase, if it can still take one; else the first."""
    last = con.execute(
        f"SELECT t.account_id FROM finance_tx t WHERE {LEDGER} ORDER BY t.id DESC LIMIT 1").fetchone()
    if last and any(a["id"] == last["account_id"] for a in accounts):
        return last["account_id"]
    return accounts[0]["id"] if accounts else None


def summary(con, ym: str, today: datetime.date, derived_ids=(), recurring: Optional[dict] = None) -> dict:
    rows = _month_rows(con, ym)
    _, _, days_in_month = _bounds(ym)
    is_current = ym == today.strftime("%Y-%m")
    elapsed = today.day if is_current else days_in_month
    spent = net(rows)

    # Compare like with like: the current month against the same days of last month.
    pm = prev_month(ym)
    prev_rows = _month_rows(con, pm, min(elapsed, _bounds(pm)[2]) if is_current else None)
    prev_any = con.execute(
        f"SELECT 1 FROM finance_tx t WHERE {LEDGER} AND t.occurred_on BETWEEN ? AND ? LIMIT 1",
        (_bounds(pm)[0], _bounds(pm)[1])).fetchone()
    compare = None
    if prev_any:
        prev_spent = net(prev_rows)
        compare = {"month": pm, "spent": prev_spent, "delta": round(spent - prev_spent, 2),
                   "same_days": is_current}

    by_cat = {}
    for r in rows:
        if r["amount_eur"] > 0 and not r["category"]:
            continue
        key = r["category"] or "none"
        by_cat[key] = by_cat.get(key, 0.0) - r["amount_eur"]
    by_category = sorted(({"category": k, "amount": round(v, 2)} for k, v in by_cat.items() if v > 0),
                         key=lambda x: -x["amount"])

    transfers = con.execute(
        "SELECT COUNT(DISTINCT transfer_id) AS n, "
        "COALESCE(SUM(CASE WHEN amount_eur < 0 THEN -amount_eur ELSE 0 END), 0) AS total "
        "FROM finance_tx WHERE status != 'dismissed' AND transfer_id IS NOT NULL "
        "AND occurred_on BETWEEN ? AND ?", _bounds(ym)[:2]).fetchone()

    first = con.execute(f"SELECT MIN(t.occurred_on) AS d FROM finance_tx t WHERE {LEDGER}").fetchone()["d"]
    months, cur = [], today.strftime("%Y-%m")
    if first:
        y, m = int(first[:4]), int(first[5:7])
        while f"{y}-{m:02d}" <= cur:
            months.append(f"{y}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    if cur not in months:
        months.append(cur)

    accounts = picker_accounts(con, derived_ids)
    default_account = default_account_id(con, accounts)

    return {
        "month": ym, "today": today.isoformat(), "is_current": is_current, "days_elapsed": elapsed,
        "spent": spent, "daily_avg": round(spent / elapsed, 2) if elapsed else 0.0,
        "compare": compare, "by_category": by_category,
        "needs_category": sum(1 for r in rows if r["amount_eur"] < 0 and not r["category"]),
        "pending_count": sum(1 for r in rows if r["status"] == "pending"),
        "recurring": recurring or {"monthly_out": 0.0, "count": 0},
        "transfers": {"count": transfers["n"], "total": round(transfers["total"], 2)},
        "months": months, "categories": [{"key": k, "name": n} for k, n in CATEGORIES],
        "accounts": accounts, "default_account_id": default_account,
        "merchants": merchants(con),
        "rows": [_row(r) for r in rows],
    }
