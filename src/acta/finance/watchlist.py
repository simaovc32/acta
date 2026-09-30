"""
Watchlist — companies the user is curious about but doesn't hold. Deliberately
separate from finance_holding: an entry here is not money, carries no
quantity or cost basis, and (see api.py) is NOT behind the finance PIN —
that's what lets Summary's !portfolio watch/unwatch write to it unattended,
the same table backing the dashboard's own "Watching" widget.
"""
import datetime
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS finance_watchlist (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol     TEXT    NOT NULL UNIQUE,
  name       TEXT,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT    NOT NULL,
  updated_at TEXT    NOT NULL
);
"""


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if not s:
        raise ValueError("symbol is required")
    return s


def add(con, symbol: str, name: str = None) -> dict:
    """Add or reactivate a watchlist entry. Keyed on symbol — re-adding one
    edits the existing row (and its name, if given) instead of duplicating it,
    same reactivate-on-re-add rule finance_holding uses."""
    sym = norm_symbol(symbol)
    now = _now()
    row = con.execute("SELECT id FROM finance_watchlist WHERE symbol=?", (sym,)).fetchone()
    if row:
        con.execute("UPDATE finance_watchlist SET name=COALESCE(?, name), "
                    "active=1, updated_at=? WHERE id=?", (name, now, row[0]))
        wid = row[0]
    else:
        cur = con.execute(
            "INSERT INTO finance_watchlist(symbol,name,active,created_at,updated_at) "
            "VALUES(?,?,1,?,?)", (sym, name, now, now))
        wid = cur.lastrowid
    return get(con, wid)


def get(con, watchlist_id: int) -> dict | None:
    r = con.execute("SELECT * FROM finance_watchlist WHERE id=?", (watchlist_id,)).fetchone()
    return dict(r) if r else None


def remove(con, symbol: str) -> bool:
    """Deactivated, never deleted — same convention the rest of the finance
    model follows, even though this isn't money."""
    sym = norm_symbol(symbol)
    row = con.execute("SELECT id FROM finance_watchlist WHERE symbol=? AND active=1",
                      (sym,)).fetchone()
    if not row:
        return False
    con.execute("UPDATE finance_watchlist SET active=0, updated_at=? WHERE id=?",
                (_now(), row[0]))
    return True


def list_watchlist(con, *, include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM finance_watchlist"
    if not include_inactive:
        sql += " WHERE active=1"
    return [dict(r) for r in con.execute(sql + " ORDER BY symbol")]
