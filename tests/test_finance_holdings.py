"""Unit tests for finance.holdings, plus the reconciliation proof that matters:
introducing holdings must not move net worth until holdings actually price the
account, and must move it correctly and exactly once when they do."""
import datetime

from acta.finance import holdings as H
from conftest import check
from ledger_fixture import synthetic_ledger

TODAY = datetime.date.today().isoformat()


def net_worth(con):
    """The same carry-forward total /api/finance/networth computes: latest
    balance per active account. Recomputed here rather than imported so the
    test does not depend on the API being importable or running."""
    total = 0.0
    for (aid,) in con.execute("SELECT id FROM finance_account WHERE active=1"):
        r = con.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? "
                        "ORDER BY as_of DESC LIMIT 1", (aid,)).fetchone()
        if r:
            total += r[0]
    return round(total, 2)


def acct(con, kind):
    return con.execute("SELECT id, name FROM finance_account WHERE kind=? AND active=1",
                       (kind,)).fetchone()


def test_holdings():
    # ── migration is inert ────────────────────────────────────────────────────
    con = synthetic_ledger()
    before = net_worth(con)
    check("creating the tables does not move net worth", net_worth(con) == before)
    check("no account is holdings-backed yet", H.holdings_accounts(con) == [])

    stocks = acct(con, "stocks")
    crypto  = acct(con, "crypto")
    check("found the stocks account", stocks is not None, "no active stocks account")
    check("found the crypto account", crypto is not None, "no active crypto account")

    # An account with no holdings is not worth zero.
    v = H.holdings_value(con, stocks["id"])
    check("no holdings -> value is None, not 0.0", v["value"] is None, str(v["value"]))
    check("syncing a holdings-free account is a no-op",
          H.sync_balance(con, stocks["id"]) is None)
    check("net worth still untouched after a no-op sync", net_worth(con) == before)

    # ── holdings without prices must not write a partial balance ──────────────
    H.upsert_holding(con, stocks["id"], "vwce", 40, name="Vanguard FTSE All-World")
    H.upsert_holding(con, stocks["id"], "AAPL", 10)
    v = H.holdings_value(con, stocks["id"])
    check("unpriced holdings -> value None", v["value"] is None)
    check("unpriced symbols are named", sorted(v["missing"]) == ["AAPL", "VWCE"],
          str(v["missing"]))
    try:
        H.sync_balance(con, stocks["id"])
        check("sync refuses to write a partial total", False, "it wrote one")
    except ValueError as e:
        check("sync refuses to write a partial total", "AAPL" in str(e), str(e))
    check("net worth unchanged while any holding is unpriced",
          net_worth(con) == before)

    # ── priced holdings derive the balance ───────────────────────────────────
    H.set_price(con, "VWCE", 125.50)
    H.set_price(con, "AAPL", 210.00)
    v = H.holdings_value(con, stocks["id"])
    expect = round(40 * 125.50 + 10 * 210.00, 2)      # 5020 + 2100 = 7120
    check("value = sum of quantity x price", v["value"] == expect,
          f"{v['value']} != {expect}")
    check("per-holding split adds to 100%",
          abs(sum(l["pct"] for l in v["lines"]) - 100.0) < 0.2,
          str([l["pct"] for l in v["lines"]]))
    check("account is now reported as derived", H.is_derived(con, stocks["id"]))

    old_xtb = con.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? "
                          "ORDER BY as_of DESC LIMIT 1", (stocks["id"],)).fetchone()[0]
    H.sync_balance(con, stocks["id"])
    check("derived balance landed in finance_balance",
          con.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? AND as_of=?",
                      (stocks["id"], TODAY)).fetchone()[0] == expect)
    check("the derived row is marked as derived",
          con.execute("SELECT note FROM finance_balance WHERE account_id=? AND as_of=?",
                      (stocks["id"], TODAY)).fetchone()[0] == "derived from holdings")
    check("net worth moved by exactly the difference",
          net_worth(con) == round(before - old_xtb + expect, 2),
          f"{net_worth(con)} vs {round(before - old_xtb + expect, 2)}")

    # Idempotence: the same sync twice must not double-count.
    nw = net_worth(con)
    H.sync_balance(con, stocks["id"])
    H.sync_balance(con, stocks["id"])
    check("re-syncing is idempotent", net_worth(con) == nw,
          f"{net_worth(con)} != {nw}")

    # History is preserved: the old snapshot is still there, untouched.
    rows = con.execute("SELECT as_of, amount_eur FROM finance_balance "
                       "WHERE account_id=? ORDER BY as_of", (stocks["id"],)).fetchall()
    check("the pre-existing snapshot survives deriving",
          any(r["as_of"] != TODAY and r["amount_eur"] == old_xtb for r in rows)
          or len(rows) == 1,
          str([tuple(r) for r in rows]))

    # ── a quantity change flows through ──────────────────────────────────────
    H.upsert_holding(con, stocks["id"], "AAPL", 20)      # bought 10 more
    H.sync_balance(con, stocks["id"])
    expect2 = round(40 * 125.50 + 20 * 210.00, 2)
    check("editing a quantity re-derives the balance",
          con.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? AND as_of=?",
                      (stocks["id"], TODAY)).fetchone()[0] == expect2)
    check("re-adding a symbol edits it instead of duplicating",
          len(H.holdings(con, stocks["id"])) == 2, str(H.holdings(con, stocks["id"])))

    # ── selling out ──────────────────────────────────────────────────────────
    aapl = next(h for h in H.holdings(con, stocks["id"]) if h["symbol"] == "AAPL")
    H.deactivate_holding(con, aapl["id"])
    H.sync_balance(con, stocks["id"])
    check("a sold holding leaves the value",
          con.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? AND as_of=?",
                      (stocks["id"], TODAY)).fetchone()[0] == round(40 * 125.50, 2))
    check("a sold holding is kept, not deleted",
          len(H.holdings(con, stocks["id"], include_inactive=True)) == 2)

    # ── cash sits beside the holdings, not inside them ───────────────────────
    con2 = synthetic_ledger()
    stocks2 = acct(con2, "stocks")["id"]
    H.upsert_holding(con2, stocks2, "VWCE", 10)
    H.set_price(con2, "VWCE", 100.0)
    H.sync_balance(con2, stocks2)
    check("no cash row reads as zero, not unknown",
          H.cash_at(con2, stocks2) == {"amount_eur": 0.0, "as_of": None, "set": False},
          str(H.cash_at(con2, stocks2)))
    check("balance without cash is just the holdings",
          con2.execute("SELECT amount_eur FROM finance_balance WHERE account_id=? "
                       "ORDER BY as_of DESC LIMIT 1", (stocks2,)).fetchone()[0] == 1000.0)

    H.set_cash(con2, stocks2, 250.0)
    r = H.sync_balance(con2, stocks2)
    check("cash is added to the derived balance", r["amount_eur"] == 1250.0, str(r))
    check("the split is reported separately",
          r["invested_eur"] == 1000.0 and r["cash_eur"] == 250.0, str(r))
    check("cash never becomes a holding",
          [h["symbol"] for h in H.holdings(con2, stocks2)] == ["VWCE"],
          str(H.holdings(con2, stocks2)))
    check("allocation value stays invested-only",
          H.holdings_value(con2, stocks2)["value"] == 1000.0)
    check("cash is not a line in the allocation",
          all("CASH" not in (l["symbol"] or "") for l in H.holdings_value(con2, stocks2)["lines"]))

    H.set_cash(con2, stocks2, 0.0)
    r = H.sync_balance(con2, stocks2)
    check("spending the cash removes it from the balance", r["amount_eur"] == 1000.0, str(r))
    check("zero cash is still 'set'", H.cash_at(con2, stocks2)["set"] is True)

    H.set_cash(con2, stocks2, 500.0, as_of="2026-08-01")
    check("cash carries forward from an earlier day",
          H.cash_at(con2, stocks2, "2026-08-03")["amount_eur"] == 500.0,
          str(H.cash_at(con2, stocks2, "2026-08-03")))
    check("a later correction still wins today",
          H.cash_at(con2, stocks2)["amount_eur"] == 0.0)

    try:
        H.set_cash(con2, stocks2, -5)
        check("negative cash rejected", False, "accepted")
    except ValueError:
        check("negative cash rejected", True)
    bank2 = con2.execute("SELECT id FROM finance_account WHERE kind='bank' AND active=1"
                         ).fetchone()["id"]
    try:
        H.set_cash(con2, bank2, 100)
        check("a bank account cannot carry separate cash", False, "accepted")
    except ValueError:
        check("a bank account cannot carry separate cash", True)
    con2.close()

    # ── prices carry forward, and staleness is per kind ──────────────────────
    H.set_price(con, "BTC", 60000.0, as_of="2026-08-01")
    p = H.price_at(con, "BTC", "2026-08-09")
    check("price carries forward to a later day", p == (60000.0, "2026-08-01"), str(p))
    check("no price before its first day",
          H.price_at(con, "BTC", "2026-07-31") is None)

    H.upsert_holding(con, crypto["id"], "BTC", 0.03)
    check("crypto goes stale in days, not weeks",
          H.stale_days(con, crypto["id"], "2026-08-09") == 8
          and H.is_stale(con, crypto["id"], "2026-08-09"),
          f"days={H.stale_days(con, crypto['id'], '2026-08-09')}")
    check("the same 8-day-old price is not stale for stocks",
          not H.is_stale(con, stocks["id"], "2026-08-09"))

    # ── guard rails ──────────────────────────────────────────────────────────
    bank = con.execute("SELECT id FROM finance_account WHERE kind='bank' AND active=1"
                       ).fetchone()
    try:
        H.upsert_holding(con, bank["id"], "VWCE", 1)
        check("a bank account cannot hold holdings", False, "it accepted one")
    except ValueError as e:
        check("a bank account cannot hold holdings", "does not hold" in str(e), str(e))
    for bad, label in ((-1, "negative quantity"), ):
        try:
            H.upsert_holding(con, stocks["id"], "TEST", bad)
            check(f"{label} rejected", False, "accepted")
        except ValueError:
            check(f"{label} rejected", True)
    try:
        H.set_price(con, "VWCE", -5)
        check("negative price rejected", False, "accepted")
    except ValueError:
        check("negative price rejected", True)
    try:
        H.upsert_holding(con, stocks["id"], "   ", 1)
        check("blank symbol rejected", False, "accepted")
    except ValueError:
        check("blank symbol rejected", True)
    check("symbols are normalised to upper case",
          [h["symbol"] for h in H.holdings(con, stocks["id"])] == ["VWCE"],
          str([h["symbol"] for h in H.holdings(con, stocks["id"])]))

    con.close()

