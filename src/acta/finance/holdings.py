"""
Holdings for investment accounts — what sits in each stock, not just a total.

Why this does not add a second source of truth
----------------------------------------------
The finance model's founding rule (2026-07-25) is that `finance_balance` rows
are the only truth for money; net worth, the history chart, the sparklines, the
30-day change and the projection all read from them. Storing holdings *beside*
a hand-entered balance would recreate exactly the drift bug that rule exists to
prevent, one level down.

So holdings are an *input*, not a parallel record: writing holdings or prices
recomputes the account's balance and upserts the ordinary `finance_balance` row
for that day. Everything downstream keeps working untouched because nothing
downstream changes. The balance is derived; the holdings are what you edit.

Two rules keep that honest:

  * An account with no active holdings is NOT worth zero — it is simply not
    holdings-backed yet, and its existing manual balance is left alone. This is
    what makes migration safe: adding the tables cannot move net worth.
  * If any holding has no price, the total is refused rather than written. A
    partial sum would land in `finance_balance` indistinguishable from a real
    balance and silently understate net worth.

v1 shows the split across holdings only. `avg_cost` is stored but unused, so
adding gain/loss later needs no migration.
"""
import datetime
import sqlite3

# Only these account kinds can be holdings-backed. A bank balance is a fact;
# an investment account's balance is a consequence of what it holds.
INVESTMENT_KINDS = ("stocks", "crypto")

# Crypto moves in hours, listed equities in days — a stale price is a wrong net
# worth, so the two get different patience. The existing finance tab already
# nudges at 35 days for balance snapshots; these are tighter on purpose.
STALE_DAYS = {"crypto": 2, "stocks": 14}

SCHEMA = """
CREATE TABLE IF NOT EXISTS finance_holding (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  symbol     TEXT    NOT NULL,          -- ticker, uppercased; price key
  name       TEXT,                      -- human label ("Vanguard FTSE All-World")
  quantity   REAL    NOT NULL,
  avg_cost   REAL,                      -- reserved for gain/loss; unused in v1
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT    NOT NULL,
  updated_at TEXT    NOT NULL,
  FOREIGN KEY (account_id) REFERENCES finance_account(id)
);
CREATE INDEX IF NOT EXISTS idx_finance_holding_acct
  ON finance_holding(account_id, active);

-- Snapshot grammar deliberately mirrors finance_balance: one row per symbol per
-- day, latest-at-or-before wins. Keyed on symbol, not account, so the same
-- ticker held in two accounts is priced once.
CREATE TABLE IF NOT EXISTS finance_holding_price (
  symbol     TEXT NOT NULL,
  as_of      TEXT NOT NULL,             -- YYYY-MM-DD
  price      REAL NOT NULL,             -- EUR per unit
  created_at TEXT NOT NULL,
  PRIMARY KEY (symbol, as_of)
);

-- Uninvested cash sitting in an investment account.
--
-- Once an account's balance is derived from its holdings, money parked there and
-- not yet spent would simply vanish from net worth. It is deliberately NOT a
-- holding with a price of 1.0: cash is not an allocation choice among
-- instruments, so it must not become a slice of the allocation chart. It is
-- tracked separately and added to the derived balance.
--
-- Same snapshot grammar as everything else here: one row per account per day,
-- latest-at-or-before wins, so a correction never rewrites history.
CREATE TABLE IF NOT EXISTS finance_holding_cash (
  account_id INTEGER NOT NULL,
  as_of      TEXT    NOT NULL,          -- YYYY-MM-DD
  amount_eur REAL    NOT NULL,
  note       TEXT,
  created_at TEXT    NOT NULL,
  PRIMARY KEY (account_id, as_of),
  FOREIGN KEY (account_id) REFERENCES finance_account(id)
);

-- What the broker itself says the account is worth, recorded next to what we
-- derived on the same day.
--
-- The derived figure is built from quantities we hold and prices from a feed,
-- so it can drift for reasons that are invisible from inside: a stale price, a
-- cross-listing proxied off another exchange, an FX rate, a dividend or fee that
-- never appears as a holding. None of those announce themselves. Storing the
-- broker's own number turns "is this still accurate?" from a memory exercise
-- into a series you can look at.
--
-- Deliberately NOT used to correct the balance: the derived figure stays the
-- one thing net worth reads, exactly as before. This is a measurement of the
-- error, not a patch over it — a patch would hide the drift it exists to show.
CREATE TABLE IF NOT EXISTS finance_broker_check (
  account_id   INTEGER NOT NULL,
  as_of        TEXT    NOT NULL,          -- YYYY-MM-DD
  reported_eur REAL    NOT NULL,          -- what the broker showed
  derived_eur  REAL,                      -- what Acta computed that day
  note         TEXT,
  created_at   TEXT    NOT NULL,
  PRIMARY KEY (account_id, as_of),
  FOREIGN KEY (account_id) REFERENCES finance_account(id)
);
"""

# Additive-only migration, same pattern as ingest.migrate()/nutrition.ensure().
_ADDED = {
    "finance_holding": [
        # The ticker a price feed needs is not the one you want on screen:
        # "VWCE" reads better than "VWCE.DE", and BTC is priced as "BTC-EUR".
        # Null means "same as symbol".
        ("feed_symbol", "TEXT"),
        # ETFs are quoted by ISIN, not ticker: justETF is the only source that
        # answers keylessly from this host, and it keys on ISIN. Null means
        # "not an ETF / no ISIN known", which sends it down the ticker path.
        ("isin", "TEXT"),
    ],
    "finance_holding_price": [
        # What the feed actually said, kept so a converted price can always be
        # audited back to its source instead of being taken on faith.
        ("native_price",    "REAL"),
        ("native_currency", "TEXT"),
        ("fx_rate",         "REAL"),
        ("fx_source",       "TEXT"),   # market rate or ECB reference
        ("source",          "TEXT"),
    ],
}


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    for table, cols in _ADDED.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def feed_symbol(holding: dict) -> str:
    """The ticker to ask a price feed for."""
    return (holding.get("feed_symbol") or holding["symbol"]).strip()


def isin(holding: dict) -> str | None:
    """The ISIN, if this holding has one. ETFs price by ISIN via justETF."""
    v = (holding.get("isin") or "").strip().upper()
    return v or None


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        raise ValueError("symbol is required")
    return s


# ── holdings ──────────────────────────────────────────────────────────────────

def upsert_holding(con, account_id: int, symbol: str, quantity: float, *,
                   name: str = None, avg_cost: float = None,
                   feed_symbol: str = None, isin: str = None) -> dict:
    """Add or update one holding. Keyed on (account_id, symbol) so re-adding a
    symbol edits it instead of creating a duplicate that would double-count."""
    sym = norm_symbol(symbol)
    quantity = float(quantity)
    if quantity < 0:
        raise ValueError("quantity cannot be negative")
    kind = con.execute("SELECT kind FROM finance_account WHERE id=?",
                       (account_id,)).fetchone()
    if kind is None:
        raise ValueError(f"unknown account {account_id}")
    if kind[0] not in INVESTMENT_KINDS:
        raise ValueError(f"account kind {kind[0]!r} does not hold investments "
                         f"(expected one of {', '.join(INVESTMENT_KINDS)})")
    now = _now()
    row = con.execute("SELECT id FROM finance_holding WHERE account_id=? AND symbol=?",
                      (account_id, sym)).fetchone()
    if row:
        # Reactivates on re-add: a symbol you sold and bought back is the same
        # holding, and a new row would be counted twice by holdings_value().
        con.execute("UPDATE finance_holding SET quantity=?, name=COALESCE(?, name), "
                    "avg_cost=COALESCE(?, avg_cost), feed_symbol=COALESCE(?, feed_symbol), "
                    "isin=COALESCE(?, isin), "
                    "active=1, updated_at=? WHERE id=?",
                    (quantity, name, avg_cost, feed_symbol, isin, now, row[0]))
        hid = row[0]
    else:
        cur = con.execute(
            "INSERT INTO finance_holding(account_id,symbol,name,quantity,avg_cost,"
            "feed_symbol,isin,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?)",
            (account_id, sym, name, quantity, avg_cost, feed_symbol, isin, now, now))
        hid = cur.lastrowid
    return get_holding(con, hid)


def get_holding(con, holding_id: int) -> dict | None:
    r = con.execute("SELECT * FROM finance_holding WHERE id=?", (holding_id,)).fetchone()
    return dict(r) if r else None


def deactivate_holding(con, holding_id: int) -> None:
    """Sold out. Kept as a row so its history and avg_cost survive — the finance
    model never deletes what it can deactivate."""
    con.execute("UPDATE finance_holding SET active=0, updated_at=? WHERE id=?",
                (_now(), holding_id))


def holdings(con, account_id: int, *, include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM finance_holding WHERE account_id=?"
    if not include_inactive:
        sql += " AND active=1"
    return [dict(r) for r in con.execute(sql + " ORDER BY symbol", (account_id,))]


def holdings_accounts(con) -> list[int]:
    """Active accounts that have at least one active holding, i.e. the accounts
    whose balance is derived rather than entered."""
    return [r[0] for r in con.execute(
        "SELECT DISTINCT h.account_id FROM finance_holding h "
        "JOIN finance_account a ON a.id = h.account_id AND a.active = 1 "
        "WHERE h.active = 1")]


def is_derived(con, account_id: int) -> bool:
    return account_id in holdings_accounts(con)


# ── prices ────────────────────────────────────────────────────────────────────

def set_price(con, symbol: str, price: float, as_of: str = None, *,
              native_price: float = None, native_currency: str = None,
              fx_rate: float = None, fx_source: str = None,
              source: str = "manual") -> dict:
    """Record one EUR price for one symbol on one day.

    `price` is always EUR — the finance model is EUR-only. When it came from a
    feed quoting another currency, the native figure and the rate used are kept
    so the conversion can be audited rather than trusted.
    """
    sym = norm_symbol(symbol)
    price = float(price)
    if price < 0:
        raise ValueError("price cannot be negative")
    as_of = as_of or datetime.date.today().isoformat()
    datetime.date.fromisoformat(as_of)      # raises on a malformed date
    con.execute("INSERT INTO finance_holding_price(symbol,as_of,price,native_price,"
                "native_currency,fx_rate,fx_source,source,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,as_of) DO UPDATE SET "
                "price=excluded.price, native_price=excluded.native_price, "
                "native_currency=excluded.native_currency, fx_rate=excluded.fx_rate, "
                "fx_source=excluded.fx_source, source=excluded.source, "
                "created_at=excluded.created_at",
                (sym, as_of, price, native_price, native_currency, fx_rate,
                 fx_source, source, _now()))
    return {"symbol": sym, "as_of": as_of, "price": price, "source": source}


def price_row(con, symbol: str, as_of: str = None) -> dict | None:
    """The whole latest price row, including where the figure came from. Kept
    separate from price_at() so that function's tuple contract stays stable."""
    sym = norm_symbol(symbol)
    as_of = as_of or datetime.date.today().isoformat()
    r = con.execute("SELECT price, as_of, source, native_price, native_currency, fx_rate "
                    "FROM finance_holding_price WHERE symbol=? AND as_of<=? "
                    "ORDER BY as_of DESC LIMIT 1", (sym, as_of)).fetchone()
    return dict(r) if r else None


def price_at(con, symbol: str, as_of: str = None) -> tuple[float, str] | None:
    """Latest price at or before `as_of` — the same carry-forward rule
    finance_balance history uses, so a day without a price is not a gap."""
    sym = norm_symbol(symbol)
    as_of = as_of or datetime.date.today().isoformat()
    r = con.execute("SELECT price, as_of FROM finance_holding_price "
                    "WHERE symbol=? AND as_of<=? ORDER BY as_of DESC LIMIT 1",
                    (sym, as_of)).fetchone()
    return (r[0], r[1]) if r else None


# ── derivation ────────────────────────────────────────────────────────────────

def set_cash(con, account_id: int, amount_eur: float, as_of: str = None, *,
             note: str = None) -> dict:
    """Record uninvested cash in an investment account for one day."""
    amount = float(amount_eur)
    if amount < 0:
        raise ValueError("cash cannot be negative")
    kind = con.execute("SELECT kind FROM finance_account WHERE id=?",
                       (account_id,)).fetchone()
    if kind is None:
        raise ValueError(f"unknown account {account_id}")
    if kind[0] not in INVESTMENT_KINDS:
        raise ValueError(f"account kind {kind[0]!r} is not an investment account — "
                         f"its balance is entered directly, cash included")
    as_of = as_of or datetime.date.today().isoformat()
    datetime.date.fromisoformat(as_of)
    con.execute("INSERT INTO finance_holding_cash(account_id,as_of,amount_eur,note,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(account_id,as_of) DO UPDATE SET "
                "amount_eur=excluded.amount_eur, note=excluded.note, "
                "created_at=excluded.created_at",
                (account_id, as_of, amount, note, _now()))
    return {"account_id": account_id, "as_of": as_of, "amount_eur": amount}


def cash_at(con, account_id: int, as_of: str = None) -> dict:
    """Latest cash figure at or before `as_of`.

    Absent cash reads as zero, not as "unknown" — unlike a missing price, which
    blocks derivation. An account with holdings and no cash row is fully
    described by its holdings, which is how every account started out.
    """
    as_of = as_of or datetime.date.today().isoformat()
    r = con.execute("SELECT amount_eur, as_of FROM finance_holding_cash "
                    "WHERE account_id=? AND as_of<=? ORDER BY as_of DESC LIMIT 1",
                    (account_id, as_of)).fetchone()
    if not r:
        return {"amount_eur": 0.0, "as_of": None, "set": False}
    return {"amount_eur": r[0], "as_of": r[1], "set": True}


def holdings_value(con, account_id: int, as_of: str = None) -> dict:
    """What the account is worth from its holdings.

    `value` is None when nothing can be valued yet — an account with no
    holdings is "not holdings-backed", which is not the same as worth zero, and
    conflating the two would zero out a real balance.
    """
    as_of = as_of or datetime.date.today().isoformat()
    rows = holdings(con, account_id)
    if not rows:
        return {"value": None, "as_of": as_of, "lines": [], "missing": [],
                "priced_as_of": None}
    lines, missing, total, oldest = [], [], 0.0, None
    for h in rows:
        p = price_row(con, h["symbol"], as_of)
        if p is None:
            missing.append(h["symbol"])
            lines.append({**h, "price": None, "price_as_of": None, "value": None,
                          "price_source": None})
            continue
        price, p_as_of = p["price"], p["as_of"]
        val = h["quantity"] * price
        total += val
        if oldest is None or p_as_of < oldest:
            oldest = p_as_of
        # price_source travels with the line so the UI can show which figures are
        # feed-derived, parsed from prose, or vouched for by hand.
        lines.append({**h, "price": price, "price_as_of": p_as_of,
                      "price_source": p["source"], "value": round(val, 2)})
    for ln in lines:
        ln["pct"] = (round(100 * ln["value"] / total, 1)
                     if (total and ln["value"] is not None) else None)
    return {"value": None if missing else round(total, 2), "as_of": as_of,
            "lines": lines, "missing": missing, "priced_as_of": oldest}


def sync_balance(con, account_id: int, as_of: str = None) -> dict | None:
    """Push the derived value into finance_balance, which every downstream
    figure already reads. Returns None when the account is not holdings-backed.

    Refuses to write a partial total: an understated balance is worse than no
    new balance, because once in finance_balance it is indistinguishable from a
    figure the user vouched for.
    """
    as_of = as_of or datetime.date.today().isoformat()
    v = holdings_value(con, account_id, as_of)
    if v["value"] is None:
        if v["missing"]:
            raise ValueError("no price for " + ", ".join(sorted(set(v["missing"])))
                             + " — priced every holding before the balance can be derived")
        return None
    # The balance is what the account is worth, which is what it holds PLUS what
    # is sitting in it uninvested. The allocation chart shows only the invested
    # part, so these two figures are deliberately different.
    cash = cash_at(con, account_id, as_of)["amount_eur"]
    total = round(v["value"] + cash, 2)
    con.execute(
        "INSERT INTO finance_balance(account_id,as_of,amount,amount_eur,note,created_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,as_of) DO UPDATE SET "
        "amount=excluded.amount, amount_eur=excluded.amount_eur, "
        "note=excluded.note, created_at=excluded.created_at",
        (account_id, as_of, total, total, "derived from holdings", _now()))
    return {"account_id": account_id, "as_of": as_of, "amount_eur": total,
            "invested_eur": v["value"], "cash_eur": cash}


def record_broker_check(con, account_id: int, reported_eur: float,
                        as_of: str = None, *, note: str = None) -> dict:
    """Record what the broker says, alongside what we derived the same day."""
    as_of = as_of or datetime.date.today().isoformat()
    reported = float(reported_eur)
    if reported < 0:
        raise ValueError("reported total cannot be negative")
    v = holdings_value(con, account_id, as_of)
    derived = (None if v["value"] is None
               else round(v["value"] + cash_at(con, account_id, as_of)["amount_eur"], 2))
    con.execute(
        "INSERT INTO finance_broker_check(account_id,as_of,reported_eur,derived_eur,note,created_at) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(account_id,as_of) DO UPDATE SET "
        "reported_eur=excluded.reported_eur, derived_eur=excluded.derived_eur, "
        "note=excluded.note, created_at=excluded.created_at",
        (account_id, as_of, round(reported, 2), derived, note, _now()))
    return broker_check(con, account_id)


def broker_check(con, account_id: int) -> dict | None:
    """The latest reconciliation for an account, with the gap worked out.

    `gap_pct` is reported against the broker's figure, not ours — theirs is the
    reference being checked against, so a percentage of our own number would be
    measuring the error against the thing suspected of being wrong.
    """
    r = con.execute(
        "SELECT * FROM finance_broker_check WHERE account_id=? "
        "ORDER BY as_of DESC LIMIT 1", (account_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if d["derived_eur"] is not None and d["reported_eur"]:
        d["gap_eur"] = round(d["derived_eur"] - d["reported_eur"], 2)
        d["gap_pct"] = round(100 * d["gap_eur"] / d["reported_eur"], 2)
    else:
        d["gap_eur"] = None
        d["gap_pct"] = None
    d["days_ago"] = (datetime.date.today()
                     - datetime.date.fromisoformat(d["as_of"])).days
    return d


def broker_check_history(con, account_id: int, limit: int = 12) -> list[dict]:
    rows = con.execute(
        "SELECT as_of, reported_eur, derived_eur FROM finance_broker_check "
        "WHERE account_id=? ORDER BY as_of DESC LIMIT ?",
        (account_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["gap_eur"] = (None if d["derived_eur"] is None
                        else round(d["derived_eur"] - d["reported_eur"], 2))
        out.append(d)
    return list(reversed(out))


def stale_days(con, account_id: int, as_of: str = None) -> int | None:
    """Age of the oldest price behind this account's value, in days."""
    v = holdings_value(con, account_id, as_of)
    if not v["priced_as_of"]:
        return None
    ref = datetime.date.fromisoformat(v["as_of"])
    return (ref - datetime.date.fromisoformat(v["priced_as_of"])).days


def is_stale(con, account_id: int, as_of: str = None) -> bool:
    d = stale_days(con, account_id, as_of)
    if d is None:
        return False
    kind = con.execute("SELECT kind FROM finance_account WHERE id=?",
                       (account_id,)).fetchone()
    return d > STALE_DAYS.get(kind[0] if kind else "", 14)
