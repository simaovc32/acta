"""Finance: accounts, balance snapshots, subscriptions, transactions, net worth."""

import datetime
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from acta.api.deps import open_db, open_db_rw
from acta.api.finance_auth import require_finance_token
from acta.config import TZ
from acta.finance import holdings as fh
from acta.finance import ledger
from acta.finance import spend as fs

router = APIRouter()


FINANCE_KINDS         = {"bank", "cash", "stocks", "crypto", "other"}


class FinanceAccountIn(BaseModel):
    name: str
    kind: str
    currency: str = "EUR"


@router.get("/api/finance/accounts", dependencies=[Depends(require_finance_token)])
def finance_accounts():
    con = open_db()
    try:
        rows = con.execute("""
            SELECT a.id, a.name, a.kind, a.currency, a.sort_order,
                   b.amount, b.amount_eur, b.as_of
            FROM finance_account a
            LEFT JOIN finance_balance b
                ON b.account_id = a.id
               AND b.as_of = (SELECT MAX(as_of) FROM finance_balance
                              WHERE account_id = a.id)
            WHERE a.active = 1
            ORDER BY a.sort_order, a.id
        """).fetchall()
    finally:
        con.close()
    return {"accounts": [dict(r) for r in rows]}


@router.post("/api/finance/accounts", dependencies=[Depends(require_finance_token)])
def finance_create_account(body: FinanceAccountIn):
    if body.kind not in FINANCE_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(FINANCE_KINDS)}")
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    # Only EUR accounts are accepted: there is no FX conversion, so a non-EUR
    # balance would be added to the EUR net worth at face value.
    if body.currency.upper() != "EUR":
        raise HTTPException(400, "only EUR accounts are supported for now")
    con = open_db_rw()
    try:
        max_sort = con.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM finance_account").fetchone()[0]
        cur = con.execute(
            "INSERT INTO finance_account(name, kind, currency, active, sort_order, created_at) "
            "VALUES(?,?,?,1,?,?)",
            (name, body.kind, body.currency.upper(), max_sort + 1,
             datetime.datetime.now(TZ).isoformat()))
        con.commit()
        return {"id": cur.lastrowid}
    finally:
        con.close()


class FinanceAccountPatch(BaseModel):
    name: Optional[str] = None
    active: Optional[bool] = None


@router.patch("/api/finance/accounts/{account_id}", dependencies=[Depends(require_finance_token)])
def finance_patch_account(account_id: int, body: FinanceAccountPatch):
    """Rename and/or remove an account. Removal is active=0, never a DELETE:
    finance_balance rows reference this id and are the historical record —
    hard-deleting would orphan them and silently rewrite past net worth."""
    fields, values = [], []
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name cannot be blank")
        fields.append("name = ?")
        values.append(name)
    if body.active is not None:
        fields.append("active = ?")
        values.append(1 if body.active else 0)
    if not fields:
        raise HTTPException(400, "no fields to update")
    con = open_db_rw()
    try:
        if not con.execute("SELECT 1 FROM finance_account WHERE id = ?", (account_id,)).fetchone():
            raise HTTPException(404, "account not found")
        values.append(account_id)
        con.execute(f"UPDATE finance_account SET {', '.join(fields)} WHERE id = ?", values)
        # Subs pointing at a removed account keep working (they are not tied
        # to it for scheduling) but lose the stale account label.
        if body.active is False:
            con.execute("UPDATE finance_sub SET account_id = NULL WHERE account_id = ?",
                        (account_id,))
        con.commit()
    finally:
        con.close()
    return {"status": "ok"}


class FinanceSubIn(BaseModel):
    name: str
    amount: float
    direction: str            # 'in' | 'out'
    day_of_month: int         # 1-31
    account_id: Optional[int] = None
    note: Optional[str] = None


class FinanceSubPatch(BaseModel):
    name: Optional[str] = None
    amount: Optional[float] = None
    direction: Optional[str] = None
    day_of_month: Optional[int] = None
    account_id: Optional[int] = None
    active: Optional[bool] = None
    note: Optional[str] = None


def _validate_sub_fields(direction: Optional[str], day_of_month: Optional[int],
                          amount: Optional[float]) -> None:
    if direction is not None and direction not in ("in", "out"):
        raise HTTPException(400, "direction must be 'in' or 'out'")
    if day_of_month is not None and not 1 <= day_of_month <= 31:
        raise HTTPException(400, "day_of_month must be 1-31")
    if amount is not None and amount <= 0:
        raise HTTPException(400, "amount must be positive — direction carries the sign")


@router.get("/api/finance/subs", dependencies=[Depends(require_finance_token)])
def finance_subs():
    con = open_db()
    try:
        rows = con.execute("""
            SELECT s.id, s.name, s.amount, s.direction, s.day_of_month, s.note,
                   s.account_id, a.name AS account_name
            FROM finance_sub s
            LEFT JOIN finance_account a ON a.id = s.account_id
            WHERE s.active = 1
            ORDER BY s.day_of_month, s.name
        """).fetchall()
        totals = ledger.monthly_totals(con=con)
    finally:
        con.close()

    today = datetime.datetime.now(TZ).date()
    subs = []
    for r in rows:
        nd = ledger.next_occurrence(r["day_of_month"], today)
        subs.append({
            "id": r["id"], "name": r["name"], "amount": r["amount"],
            "direction": r["direction"], "day_of_month": r["day_of_month"],
            "account_id": r["account_id"], "account_name": r["account_name"],
            "note": r["note"], "next_date": nd.isoformat(),
            "days_until": (nd - today).days,
        })
    subs.sort(key=lambda s: s["days_until"])
    return dict(totals, subs=subs)


@router.post("/api/finance/subs", dependencies=[Depends(require_finance_token)])
def finance_create_sub(body: FinanceSubIn):
    _validate_sub_fields(body.direction, body.day_of_month, body.amount)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    con = open_db_rw()
    try:
        if body.account_id is not None:
            exists = con.execute(
                "SELECT 1 FROM finance_account WHERE id = ? AND active = 1",
                (body.account_id,)).fetchone()
            if not exists:
                raise HTTPException(400, f"unknown account id {body.account_id}")
        cur = con.execute(
            "INSERT INTO finance_sub(name, amount, direction, day_of_month, "
            "account_id, active, note, created_at) VALUES(?,?,?,?,?,1,?,?)",
            (name, body.amount, body.direction, body.day_of_month, body.account_id,
             (body.note or "").strip() or None, datetime.datetime.now(TZ).isoformat()))
        con.commit()
        return {"id": cur.lastrowid}
    finally:
        con.close()


@router.patch("/api/finance/subs/{sub_id}", dependencies=[Depends(require_finance_token)])
def finance_patch_sub(sub_id: int, body: FinanceSubPatch):
    _validate_sub_fields(body.direction, body.day_of_month, body.amount)
    # Keyed off which fields were SENT, not non-None values: `account_id: null`
    # means "detach this sub from its account" (same for clearing `note`).
    sent = body.model_fields_set
    fields, values = [], []
    for col in ("name", "amount", "direction", "day_of_month", "account_id", "note"):
        if col not in sent:
            continue
        val = getattr(body, col)
        if col == "name":
            val = (val or "").strip()
            if not val:
                raise HTTPException(400, "name cannot be blank")
        fields.append(f"{col} = ?")
        values.append(val)
    if "active" in sent and body.active is not None:
        fields.append("active = ?")
        values.append(1 if body.active else 0)
    if not fields:
        raise HTTPException(400, "no fields to update")
    con = open_db_rw()
    try:
        existing = con.execute("SELECT 1 FROM finance_sub WHERE id = ?", (sub_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "sub not found")
        if body.account_id is not None and "account_id" in sent:
            ok = con.execute("SELECT 1 FROM finance_account WHERE id = ? AND active = 1",
                             (body.account_id,)).fetchone()
            if not ok:
                raise HTTPException(400, f"unknown account id {body.account_id}")
        values.append(sub_id)
        con.execute(f"UPDATE finance_sub SET {', '.join(fields)} WHERE id = ?", values)
        con.commit()
    finally:
        con.close()
    return {"status": "ok"}


class FinanceTxIn(BaseModel):
    account_id:  int
    amount_eur:  float               # signed: negative = spent, positive = received
    description: Optional[str] = None  # the merchant, or what it was
    occurred_on: Optional[str] = None  # defaults to today; YYYY-MM-DD
    category:    Optional[str] = None  # a key from finance_spend.CATEGORIES
    source:      Optional[str] = None  # manual | whatsapp | sync | import
    note:        Optional[str] = None


@router.post("/api/finance/transaction", dependencies=[Depends(require_finance_token)])
def finance_log_transaction(body: FinanceTxIn):
    """Log one ad-hoc transaction. Proposes a balance; never writes one."""
    amount = float(body.amount_eur)
    if amount == 0:
        raise HTTPException(400, "amount cannot be zero")
    if abs(amount) > 1_000_000:
        raise HTTPException(400, "amount looks implausible")
    on = body.occurred_on or datetime.datetime.now(TZ).date().isoformat()
    try:
        on_d = datetime.date.fromisoformat(on)
    except ValueError:
        raise HTTPException(400, "occurred_on must be YYYY-MM-DD") from None
    if on_d > datetime.datetime.now(TZ).date():
        raise HTTPException(400, "occurred_on cannot be in the future")

    con = open_db_rw()
    try:
        ok = con.execute("SELECT 1 FROM finance_account WHERE id=? AND active=1",
                         (body.account_id,)).fetchone()
        if not ok:
            raise HTTPException(400, f"unknown account id {body.account_id}")
        # A holdings-derived account's value comes from what it holds, so a
        # transaction there would be a buy/sell — a holdings edit, not a
        # balance nudge. Refused rather than silently ignored.
        if body.account_id in set(fh.holdings_accounts(con)):
            raise HTTPException(
                409, "that account is valued from its holdings — record a buy or "
                     "sell against the holdings instead of a transaction")
        try:
            category = fs.clean_category(body.category)
            source = fs.clean_source(body.source)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        cur = con.execute(
            "INSERT INTO finance_tx(account_id, amount_eur, description, occurred_on, "
            "status, created_at, category, source, note) VALUES(?,?,?,?,'pending',?,?,?,?)",
            (body.account_id, round(amount, 2), fs.clean_text(body.description, 80),
             on, datetime.datetime.now(TZ).isoformat(), category, source,
             fs.clean_text(body.note, 200)))
        con.commit()
        tx_id = cur.lastrowid
        row = fs.get_row(con, tx_id)
    finally:
        con.close()
    return {"status": "ok", "id": tx_id, "row": row}


# ── spending ledger ────────────────────────────────────────────────────────
@router.get("/api/finance/spending", dependencies=[Depends(require_finance_token)])
def finance_spending(month: Optional[str] = None):
    """The month view of the ledger: rows, totals, category split, comparison
    with the same days of last month, and what the add sheet needs (accounts,
    categories, known merchants). One response so the numbers on screen always
    come from one place."""
    today = datetime.datetime.now(TZ).date()
    try:
        ym = fs.parse_month(month, today)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    con = open_db()
    try:
        return fs.summary(con, ym, today, derived_ids=fh.holdings_accounts(con),
                          recurring=_recurring_summary(con))
    finally:
        con.close()


def _recurring_summary(con) -> dict:
    subs = [s for s in ledger.active_subs(con) if s["direction"] == "out"]
    return {"monthly_out": round(sum(s["amount"] for s in subs), 2), "count": len(subs)}


class FinanceTxPatch(BaseModel):
    account_id:  Optional[int] = None
    amount_eur:  Optional[float] = None
    description: Optional[str] = None
    occurred_on: Optional[str] = None
    category:    Optional[str] = None   # "" clears it
    note:        Optional[str] = None


@router.patch("/api/finance/transaction/{tx_id}", dependencies=[Depends(require_finance_token)])
def finance_edit_transaction(tx_id: int, body: FinanceTxPatch):
    """Edit a purchase. Transfer legs are not editable here (they are a pair).
    An applied purchase can be corrected too: balances are snapshots, so this only
    changes the ledger, never a balance."""
    fields = body.model_dump(exclude_unset=True) if hasattr(body, "model_dump") else body.dict(exclude_unset=True)
    con = open_db_rw()
    try:
        row = con.execute("SELECT * FROM finance_tx WHERE id = ?", (tx_id,)).fetchone()
        if not row or row["status"] == "dismissed":
            raise HTTPException(404, "transaction not found")
        if row["transfer_id"]:
            raise HTTPException(409, "a transfer leg cannot be edited here")
        sets, vals = [], []
        if "amount_eur" in fields:
            amount = float(fields["amount_eur"] or 0)
            if amount == 0 or abs(amount) > 1_000_000:
                raise HTTPException(400, "amount must be non-zero and plausible")
            sets.append("amount_eur = ?")
            vals.append(round(amount, 2))
        if "account_id" in fields:
            ok = con.execute("SELECT 1 FROM finance_account WHERE id=? AND active=1",
                             (fields["account_id"],)).fetchone()
            if not ok:
                raise HTTPException(400, f"unknown account id {fields['account_id']}")
            if fields["account_id"] in set(fh.holdings_accounts(con)):
                raise HTTPException(409, "that account is valued from its holdings")
            sets.append("account_id = ?")
            vals.append(fields["account_id"])
        if "occurred_on" in fields:
            try:
                d = datetime.date.fromisoformat(fields["occurred_on"])
            except (TypeError, ValueError):
                raise HTTPException(400, "occurred_on must be YYYY-MM-DD") from None
            if d > datetime.datetime.now(TZ).date():
                raise HTTPException(400, "occurred_on cannot be in the future")
            sets.append("occurred_on = ?")
            vals.append(d.isoformat())
        if "description" in fields:
            sets.append("description = ?")
            vals.append(fs.clean_text(fields["description"], 80))
        if "note" in fields:
            sets.append("note = ?")
            vals.append(fs.clean_text(fields["note"], 200))
        if "category" in fields:
            try:
                sets.append("category = ?")
                vals.append(fs.clean_category(fields["category"]))
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
        if not sets:
            raise HTTPException(400, "nothing to change")
        con.execute(f"UPDATE finance_tx SET {', '.join(sets)} WHERE id = ?", vals + [tx_id])
        con.commit()
        out = fs.get_row(con, tx_id)
    finally:
        con.close()
    return {"status": "ok", "row": out}


@router.post("/api/finance/transaction/{tx_id}/restore", dependencies=[Depends(require_finance_token)])
def finance_restore_transaction(tx_id: int):
    """Undo a dismiss. Comes back as applied if it had already been absorbed
    into a confirmed balance, otherwise as pending."""
    con = open_db_rw()
    try:
        row = con.execute("SELECT status, applied_at, transfer_id FROM finance_tx WHERE id=?",
                          (tx_id,)).fetchone()
        if not row:
            raise HTTPException(404, "transaction not found")
        if row["transfer_id"]:
            raise HTTPException(409, "a transfer leg cannot be restored here")
        if row["status"] != "dismissed":
            return {"status": "ok", "row": fs.get_row(con, tx_id)}
        con.execute("UPDATE finance_tx SET status = ? WHERE id = ?",
                    ("applied" if row["applied_at"] else "pending", tx_id))
        con.commit()
        out = fs.get_row(con, tx_id)
    finally:
        con.close()
    return {"status": "ok", "row": out}


class FinanceTransferIn(BaseModel):
    from_account_id: int
    to_account_id:   int
    amount_eur:      float             # always positive; direction is the two ids
    description:     Optional[str] = None
    occurred_on:     Optional[str] = None


@router.post("/api/finance/transfer", dependencies=[Depends(require_finance_token)])
def finance_log_transfer(body: FinanceTransferIn):
    """Move money between two accounts as two linked, pending legs.

    Net worth does not change when you move your own money, and this keeps that
    true by construction: the legs are equal and opposite, so once both are
    confirmed they cancel in `unaccounted` instead of reading as a mystery
    outflow from one account and a windfall into the other.

    A leg against a holdings-derived account targets its CASH rather than its
    balance — money that has landed at a broker but bought nothing yet is
    uninvested cash by definition, and its balance stays derived from holdings.
    """
    amount = round(float(body.amount_eur), 2)
    if amount <= 0:
        raise HTTPException(400, "amount must be positive")
    if abs(amount) > 1_000_000:
        raise HTTPException(400, "amount looks implausible")
    if body.from_account_id == body.to_account_id:
        raise HTTPException(400, "pick two different accounts")
    on = body.occurred_on or datetime.datetime.now(TZ).date().isoformat()
    try:
        on_d = datetime.date.fromisoformat(on)
    except ValueError:
        raise HTTPException(400, "occurred_on must be YYYY-MM-DD") from None
    if on_d > datetime.datetime.now(TZ).date():
        raise HTTPException(400, "occurred_on cannot be in the future")

    con = open_db_rw()
    try:
        fh.ensure(con)
        derived_ids = set(fh.holdings_accounts(con))
        names = {}
        for aid in (body.from_account_id, body.to_account_id):
            r = con.execute("SELECT name FROM finance_account WHERE id=? AND active=1",
                            (aid,)).fetchone()
            if not r:
                raise HTTPException(400, f"unknown account id {aid}")
            names[aid] = r["name"]

        tid = uuid.uuid4().hex
        now_iso = datetime.datetime.now(TZ).isoformat()
        note = (body.description or "").strip() or None
        legs = []
        for aid, signed, other in (
                (body.from_account_id, -amount, body.to_account_id),
                (body.to_account_id,    amount, body.from_account_id)):
            target = "cash" if aid in derived_ids else "balance"
            desc = note or ("To " + names[other] if signed < 0
                            else "From " + names[other])
            cur = con.execute(
                "INSERT INTO finance_tx(account_id, amount_eur, description, occurred_on, "
                "status, created_at, transfer_id, target) VALUES(?,?,?,?,'pending',?,?,?)",
                (aid, signed, desc, on, now_iso, tid, target))
            legs.append({"id": cur.lastrowid, "account_id": aid, "amount_eur": signed,
                         "target": target})
        con.commit()
    finally:
        con.close()
    return {"status": "ok", "transfer_id": tid, "legs": legs}


@router.get("/api/finance/transactions", dependencies=[Depends(require_finance_token)])
def finance_list_transactions(status: str = "pending", limit: int = 50):
    if status not in ("pending", "applied", "dismissed", "all"):
        raise HTTPException(400, "bad status filter")
    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be 1-500")
    con = open_db()
    try:
        sql = ("SELECT t.*, a.name AS account_name FROM finance_tx t "
               "JOIN finance_account a ON a.id = t.account_id ")
        args: list = []
        if status != "all":
            sql += "WHERE t.status = ? "
            args.append(status)
        sql += "ORDER BY t.occurred_on DESC, t.id DESC LIMIT ?"
        args.append(limit)
        return {"transactions": [dict(r) for r in con.execute(sql, args)]}
    finally:
        con.close()


@router.delete("/api/finance/transaction/{tx_id}", dependencies=[Depends(require_finance_token)])
def finance_dismiss_transaction(tx_id: int):
    """Drop a mistyped transaction. Marked dismissed, never deleted — same rule
    the rest of the finance model follows for anything that has been recorded."""
    con = open_db_rw()
    try:
        row = con.execute("SELECT status FROM finance_tx WHERE id=?", (tx_id,)).fetchone()
        if not row:
            raise HTTPException(404, "transaction not found")
        con.execute("UPDATE finance_tx SET status='dismissed' WHERE id=?", (tx_id,))
        con.commit()
    finally:
        con.close()
    return {"status": "ok"}


class FinanceBalanceIn(BaseModel):
    as_of: Optional[str] = None     # defaults to today; YYYY-MM-DD
    balances: dict                  # {account_id: amount}, native currency
    note: Optional[str] = None


@router.post("/api/finance/balance", dependencies=[Depends(require_finance_token)])
def finance_save_balance(body: FinanceBalanceIn):
    if not body.balances:
        raise HTTPException(400, "at least one balance required")
    as_of = body.as_of or datetime.datetime.now(TZ).date().isoformat()
    try:
        datetime.date.fromisoformat(as_of)
    except ValueError:
        raise HTTPException(400, "as_of must be YYYY-MM-DD") from None

    con = open_db_rw()
    try:
        valid_ids = {r[0] for r in con.execute(
            "SELECT id FROM finance_account WHERE active = 1")}
        # A holdings-derived account can't also take a typed balance (two sources of
        # truth). Scoped to accounts with holdings, so a new stocks account still can.
        derived_ids = set(fh.holdings_accounts(con))
        now_iso = datetime.datetime.now(TZ).isoformat()
        note = (body.note or "").strip() or None
        absorbed = 0
        for acct_id, amount in body.balances.items():
            try:
                aid = int(acct_id)
            except (TypeError, ValueError):
                raise HTTPException(400, f"invalid account id {acct_id!r}") from None
            if aid not in valid_ids:
                raise HTTPException(400, f"unknown account id {aid}")
            if aid in derived_ids:
                raise HTTPException(
                    409, f"account {aid} is valued from its holdings — update the "
                         f"holdings or their prices instead of the balance")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                raise HTTPException(400, f"amount for account {aid} must be a number") from None
            # EUR-only for now, so amount_eur == amount. Kept as a separate
            # column so adding FX later never has to touch historical rows.
            amount_eur = amount
            con.execute(
                "INSERT INTO finance_balance(account_id, as_of, amount, amount_eur, note, created_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(account_id, as_of) DO UPDATE SET "
                "amount=excluded.amount, amount_eur=excluded.amount_eur, "
                "note=excluded.note, created_at=excluded.created_at",
                (aid, as_of, amount, amount_eur, note, now_iso))
            # Confirming a balance is the user vouching for a number that
            # already reflects everything up to that date, so every pending
            # transaction on or before it has now been absorbed. Marked applied
            # rather than deleted, so the ledger of what was logged survives,
            # and so a later balance can't double-count the same purchase.
            absorbed += con.execute(
                "UPDATE finance_tx SET status='applied', applied_at=? "
                "WHERE account_id=? AND status='pending' AND target='balance' "
                "AND occurred_on<=?",
                (now_iso, aid, as_of)).rowcount
        con.commit()
    finally:
        con.close()
    return {"status": "ok", "as_of": as_of, "transactions_applied": absorbed}


@router.get("/api/finance/networth", dependencies=[Depends(require_finance_token)])
def finance_networth(days: int = 365):
    """Net worth now, per-account breakdown, and a carry-forward history series.

    Carry-forward (not "sum of rows dated exactly D"): for each date any
    account was snapshotted, total = latest known balance per account at or
    before that date. Without this, editing a single account between full
    snapshots (an explicitly requested feature) would make every other
    account silently drop out of that day's total."""
    if not 1 <= days <= 3650:
        raise HTTPException(400, "days must be 1-3650")
    con = open_db()
    try:
        accounts = con.execute(
            "SELECT id, name, kind, currency, sort_order FROM finance_account "
            "WHERE active = 1 ORDER BY sort_order, id").fetchall()

        # Active accounts only, matching the headline total, so a removed account
        # doesn't keep feeding the chart while missing from the number above it.
        all_rows = con.execute(
            "SELECT b.account_id, b.as_of, b.amount_eur FROM finance_balance b "
            "JOIN finance_account a ON a.id = b.account_id AND a.active = 1 "
            "ORDER BY b.account_id, b.as_of").fetchall()
    finally:
        con.close()

    by_acct: dict = {}
    for r in all_rows:
        by_acct.setdefault(r["account_id"], []).append((r["as_of"], r["amount_eur"]))

    # An account with no balance row is "not snapshotted", not "€0". The total sums
    # the accounts that have at least one snapshot (adding a 2nd account mustn't blank
    # the 1st), and as_of is null only when nothing has ever been saved.
    acct_out, total, latest_date, any_snapshot = [], 0.0, None, False
    for a in accounts:
        series = by_acct.get(a["id"], [])
        latest = series[-1] if series else None
        amt_eur = latest[1] if latest else None
        if amt_eur is not None:
            total += amt_eur
            any_snapshot = True
        if latest and (latest_date is None or latest[0] > latest_date):
            latest_date = latest[0]
        acct_out.append({
            "id":         a["id"],
            "name":       a["name"],
            "kind":       a["kind"],
            "currency":   a["currency"],
            "amount":     latest[1] if latest else None,   # EUR-only: amount == amount_eur
            "amount_eur": amt_eur,
            "as_of":      latest[0] if latest else None,
            "sparkline":  [v for _, v in series[-12:]],
        })
    for a in acct_out:
        a["pct"] = round(100 * a["amount_eur"] / total, 1) if (total and a["amount_eur"] is not None) else None

    # ---- expected balance per account ------------------------------------
    # What the balance should be now, given the subs that landed since its snapshot.
    # Never written to finance_balance, so `unaccounted` keeps measuring real drift.
    # Derived accounts are skipped: theirs is recomputed from holdings daily.
    today_d = datetime.datetime.now(TZ).date()
    con3 = ledger.open_ro()
    try:
        derived_ids = set(fh.holdings_accounts(con3))
        for a in acct_out:
            a["expected_eur"] = None
            a["expected_delta_eur"] = None
            a["subs_since"] = []
            a["tx_since"] = []
            # Exposed so the UI can keep transactions off a holdings-derived
            # account instead of offering an action the API will 409.
            a["derived"] = a["id"] in derived_ids
            if a["amount_eur"] is None or not a["as_of"] or a["id"] in derived_ids:
                continue
            snap_d = datetime.date.fromisoformat(a["as_of"])

            # Subs land on a schedule, so they are read off the calendar between
            # the snapshot and today.
            events = (ledger.account_events_between(a["id"], snap_d, today_d, con=con3)
                      if snap_d < today_d else [])

            # Transactions count by `status`, not date: a row stays `pending` until a
            # confirmed balance absorbs it, so one logged on the snapshot day still shows.
            # target='cash' legs belong to a derived account's Cash card, not a balance.
            tx_rows = con3.execute(
                "SELECT id, amount_eur, description, occurred_on FROM finance_tx "
                "WHERE account_id = ? AND status = 'pending' AND target = 'balance' "
                "AND occurred_on <= ? ORDER BY occurred_on, id",
                (a["id"], today_d.isoformat())).fetchall()
            txs = [{"id": r["id"], "amount": r["amount_eur"],
                    "description": r["description"], "as_of": r["occurred_on"],
                    "delta": r["amount_eur"]} for r in tx_rows]

            if not events and not txs:
                continue
            delta = round(sum(e["delta"] for e in events) + sum(t["delta"] for t in txs), 2)
            a["subs_since"] = events
            a["tx_since"] = txs
            a["expected_delta_eur"] = delta
            a["expected_eur"] = round(a["amount_eur"] + delta, 2)
    finally:
        con3.close()

    since = (datetime.datetime.now(TZ).date() - datetime.timedelta(days=days)).isoformat()
    all_dates = sorted({d for series in by_acct.values() for d, _ in series})
    hist_dates = [d for d in all_dates if d >= since]
    if latest_date and latest_date not in hist_dates:
        hist_dates.append(latest_date)   # always include the latest point

    history = []
    for d in hist_dates:
        tot = 0.0
        for series in by_acct.values():
            val = None
            for as_of, amt in series:
                if as_of <= d:
                    val = amt
                else:
                    break
            if val is not None:
                tot += val
        history.append({"as_of": d, "total_eur": round(tot, 2)})

    change = None
    if latest_date and history:
        target = (datetime.date.fromisoformat(latest_date)
                  - datetime.timedelta(days=30)).isoformat()
        # The LAST history point at or before the 30-day mark (history is ascending,
        # so next() would return the oldest and widen the window to the whole history).
        earlier = [h for h in history if h["as_of"] <= target]
        past = earlier[-1] if earlier else history[0]
        if past["as_of"] != latest_date:
            change = {
                "amount_eur": round(total - past["total_eur"], 2),
                "days": (datetime.date.fromisoformat(latest_date)
                        - datetime.date.fromisoformat(past["as_of"])).days,
            }

    saved_per_month_eur = None
    if change and change["days"] > 0:
        saved_per_month_eur = round(change["amount_eur"] / change["days"] * 30.44, 2)

    # ---- subs-driven figures --------------------------------------------
    con2 = ledger.open_ro()
    try:
        sub_totals = ledger.monthly_totals(con=con2)

        # Unaccounted = actual change between an account's last two snapshots minus what
        # the subs schedule predicts for that window. Positive = unlogged income;
        # negative = unlogged spending. Needs >=2 snapshots to compare.
        # Holdings-derived accounts are excluded: their nightly price-driven balance is
        # neither income nor spending, so market movement is reported as its own figure.
        # Each account uses its OWN last two balance rows, not the combined history,
        # which gains a point every night from the price feed.
        subs_all = ledger.active_subs(con2)
        unaccounted_eur = None
        market_move_eur = None
        unacc_days = None

        for a in acct_out:
            series = by_acct.get(a["id"], [])
            if len(series) < 2:
                continue
            (prev_as_of, prev_amt), (cur_as_of, cur_amt) = series[-2], series[-1]
            actual = cur_amt - prev_amt

            if a["id"] in derived_ids:
                # A derived balance moves only because prices moved.
                market_move_eur = round((market_move_eur or 0.0) + actual, 2)
                continue

            prev_d = datetime.date.fromisoformat(prev_as_of)
            cur_d  = datetime.date.fromisoformat(cur_as_of)
            expected = 0.0
            for s in subs_all:
                if s["account_id"] != a["id"]:
                    continue
                n = len(ledger.occurrences_between(s["day_of_month"], prev_d, cur_d))
                expected += n * s["amount"] * (1 if s["direction"] == "in" else -1)
            # Applied transactions are explained spending, like a sub occurrence, so logging
            # a purchase doesn't raise `unaccounted`. Pending ones haven't moved a balance yet.
            expected += con2.execute(
                "SELECT COALESCE(SUM(amount_eur), 0) FROM finance_tx "
                "WHERE account_id = ? AND status = 'applied' AND target = 'balance' "
                "AND occurred_on > ? AND occurred_on <= ?",
                (a["id"], prev_as_of, cur_as_of)).fetchone()[0]

            unaccounted_eur = round((unaccounted_eur or 0.0) + (actual - expected), 2)
            gap = (cur_d - prev_d).days
            unacc_days = gap if unacc_days is None else max(unacc_days, gap)

        # Forward projection for the trend chart's dashed continuation: start
        # at today's actual total, step forward only at the dates subs
        # actually land on (not every day — a sparse polyline is enough and
        # keeps this cheap), out to 30 days.
        projection = []
        if latest_date and any_snapshot:
            start_d = datetime.date.fromisoformat(latest_date)
            end_d = start_d + datetime.timedelta(days=30)
            running = total
            projection.append({"as_of": start_d.isoformat(), "total_eur": round(running, 2)})
            events = []
            for s in ledger.active_subs(con2):
                for d in ledger.occurrences_between(s["day_of_month"], start_d, end_d):
                    events.append((d, s["amount"] * (1 if s["direction"] == "in" else -1)))
            events.sort(key=lambda e: e[0])
            for d, delta in events:
                running += delta
                projection.append({"as_of": d.isoformat(), "total_eur": round(running, 2)})
            if not events or events[-1][0] != end_d:
                projection.append({"as_of": end_d.isoformat(), "total_eur": round(running, 2)})
    finally:
        con2.close()

    return {
        "total_eur":           round(total, 2) if any_snapshot else None,
        "as_of":               latest_date,
        "change":              change,
        "saved_per_month_eur": saved_per_month_eur,
        "subs_monthly_out_eur": sub_totals["monthly_out"],
        "subs_monthly_in_eur":  sub_totals["monthly_in"],
        "unaccounted_eur":     unaccounted_eur,
        "unaccounted_days":    unacc_days,
        "market_move_eur":     market_move_eur,
        "accounts":            acct_out,
        "history":             history,
        "projection":          projection,
    }
