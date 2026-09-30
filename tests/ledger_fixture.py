"""A small synthetic ledger for the holdings / price-feed tests."""
import sqlite3
import tempfile

from acta import config
from acta.api import schema
from acta.finance import holdings as H

ACCOUNTS = (("Everyday", "bank", 2400.0), ("Wallet", "cash", 80.0),
            ("Broker", "stocks", 4100.0), ("Crypto", "crypto", 350.0))


def synthetic_ledger() -> sqlite3.Connection:
    """A fresh database with one account of each kind and one old balance snapshot each."""
    path = tempfile.mkstemp(suffix=".db", prefix="ledger-")[1]
    real, config.ACTA_DB = config.ACTA_DB, path
    try:
        schema.init_finance_tables()          # the API's own DDL, not a copy of it
    finally:
        config.ACTA_DB = real
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    H.ensure(con)
    for name, kind, amount in ACCOUNTS:
        cur = con.execute("INSERT INTO finance_account(name, kind, created_at) VALUES(?,?,'2026-07-01')", (name, kind))
        con.execute("INSERT INTO finance_balance(account_id, as_of, amount, amount_eur, created_at) "
                    "VALUES(?, '2026-08-01', ?, ?, '2026-08-01')", (cur.lastrowid, amount, amount))
    con.commit()
    return con
