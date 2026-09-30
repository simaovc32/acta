"""Investments: holdings, the price feed, broker reconciliation, watchlist."""

import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from acta.api.deps import open_db_rw
from acta.api.finance_auth import require_finance_token
from acta.config import TZ
from acta.finance import holdings as fh
from acta.finance import prices
from acta.finance import watchlist as fw

router = APIRouter()


def _pending_cash(con, account_id: int, cash_now: float) -> dict:
    """Cash legs awaiting confirmation on a derived account."""
    rows = con.execute(
        "SELECT id, amount_eur, description, occurred_on FROM finance_tx "
        "WHERE account_id = ? AND status = 'pending' AND target = 'cash' "
        "ORDER BY occurred_on, id", (account_id,)).fetchall()
    if not rows:
        return {"cash_pending": [], "cash_expected_eur": None, "cash_delta_eur": None}
    delta = round(sum(r["amount_eur"] for r in rows), 2)
    # Clamped at zero: cash cannot go negative, and a transfer out of more than
    # is parked there means some holdings were sold to fund it — a correction
    # the user makes by hand rather than a number to invent here.
    return {"cash_pending": [dict(r) for r in rows],
            "cash_delta_eur": delta,
            "cash_expected_eur": round(max(0.0, cash_now + delta), 2)}


@router.get("/api/finance/holdings", dependencies=[Depends(require_finance_token)])
def finance_holdings_get():
    """Per-account holdings with their current value and share of the account.

    `value` is null for an account whose holdings are not all priced — the same
    refusal sync_balance() makes, surfaced instead of silently understated."""
    con = open_db_rw()
    try:
        fh.ensure(con)
        out = []
        for a in con.execute("SELECT id, name, kind FROM finance_account "
                             "WHERE active = 1 AND kind IN (?, ?) ORDER BY sort_order, id",
                             fh.INVESTMENT_KINDS):
            v = fh.holdings_value(con, a["id"])
            cash = fh.cash_at(con, a["id"])
            out.append({"account_id": a["id"], "name": a["name"], "kind": a["kind"],
                        "derived": fh.is_derived(con, a["id"]),
                        # invested vs total: the chart shows the first, the
                        # account balance is the second.
                        "value_eur": v["value"], "lines": v["lines"],
                        "cash_eur": cash["amount_eur"], "cash_as_of": cash["as_of"],
                        "cash_set": cash["set"],
                        "total_eur": None if v["value"] is None
                                     else round(v["value"] + cash["amount_eur"], 2),
                        "missing_prices": v["missing"],
                        "priced_as_of": v["priced_as_of"],
                        "stale_days": fh.stale_days(con, a["id"]),
                        "stale": fh.is_stale(con, a["id"]),
                        "stale_after_days": fh.STALE_DAYS.get(a["kind"]),
                        "broker_check": fh.broker_check(con, a["id"]),
                        "broker_history": fh.broker_check_history(con, a["id"]),
                        # Money transferred in but not yet invested is proposed
                        # against cash, and stays pending until confirmed here —
                        # the same posture as a balance, on the figure that
                        # actually moved.
                        **_pending_cash(con, a["id"], cash["amount_eur"])})
        return {"accounts": out}
    finally:
        con.close()


class HoldingIn(BaseModel):
    account_id:  int
    symbol:      str
    quantity:    float
    name:        Optional[str] = None
    feed_symbol: Optional[str] = None
    isin:        Optional[str] = None
    avg_cost:    Optional[float] = None


@router.post("/api/finance/holdings", dependencies=[Depends(require_finance_token)])
def finance_holding_upsert(body: HoldingIn):
    """Add a holding, or change the quantity of one already held.

    Keyed on (account_id, symbol), so buying more of something you already own
    edits that line instead of creating a second one that would double-count.

    The balance is re-derived immediately, but a failure to re-derive is NOT
    fatal here: the holding itself is valid and saved, it simply has no price
    yet. Raising would leave the user unable to enter a newly bought position
    until the next nightly price run.
    """
    con = open_db_rw()
    try:
        fh.ensure(con)
        try:
            h = fh.upsert_holding(
                con, body.account_id, body.symbol, body.quantity,
                name=(body.name or "").strip() or None,
                feed_symbol=(body.feed_symbol or "").strip() or None,
                isin=(body.isin or "").strip() or None,
                avg_cost=body.avg_cost)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        synced, warn = None, None
        try:
            synced = fh.sync_balance(con, body.account_id)
        except ValueError as e:
            warn = str(e)
        con.commit()
        return {"status": "ok", "holding": h, "synced": synced, "warning": warn}
    finally:
        con.close()


@router.delete("/api/finance/holdings/{holding_id}",
            dependencies=[Depends(require_finance_token)])
def finance_holding_remove(holding_id: int):
    """Sold out of a position. Deactivated, never deleted — its price history
    and cost basis stay intact, same rule the rest of the finance model follows.

    A partial-price failure is surfaced as a warning rather than raised: the
    sale is real and must be recorded even if what remains cannot be valued yet.
    """
    con = open_db_rw()
    try:
        fh.ensure(con)
        h = fh.get_holding(con, holding_id)
        if not h or not h["active"]:
            raise HTTPException(404, "holding not found")
        fh.deactivate_holding(con, holding_id)
        synced, warn = None, None
        try:
            synced = fh.sync_balance(con, h["account_id"])
        except ValueError as e:
            warn = str(e)
        con.commit()
        return {"status": "ok", "synced": synced, "warning": warn}
    finally:
        con.close()


class WatchlistIn(BaseModel):
    symbol: str
    name: Optional[str] = None


@router.get("/api/portfolio/watchlist")
def portfolio_watchlist_get():
    """Companies watched but not held. Deliberately NOT behind
    require_finance_token like every /api/finance/* route above — this isn't
    money data, and Summary's !portfolio watch/unwatch needs to write here
    unattended, without a stored finance session token."""
    con = open_db_rw()
    try:
        fw.ensure(con)
        return {"watchlist": fw.list_watchlist(con)}
    finally:
        con.close()


@router.post("/api/portfolio/watchlist")
def portfolio_watchlist_add(body: WatchlistIn):
    con = open_db_rw()
    try:
        fw.ensure(con)
        try:
            entry = fw.add(con, body.symbol, (body.name or "").strip() or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        con.commit()
        return {"status": "ok", "entry": entry}
    finally:
        con.close()


@router.delete("/api/portfolio/watchlist/{symbol}")
def portfolio_watchlist_remove(symbol: str):
    con = open_db_rw()
    try:
        fw.ensure(con)
        removed = fw.remove(con, symbol)
        if not removed:
            raise HTTPException(404, "symbol not in watchlist")
        con.commit()
        return {"status": "ok"}
    finally:
        con.close()


class HoldingCashIn(BaseModel):
    amount_eur: float
    as_of: Optional[str] = None
    note: Optional[str] = None


@router.post("/api/finance/holdings/{account_id}/cash",
          dependencies=[Depends(require_finance_token)])
def finance_holdings_cash(account_id: int, body: HoldingCashIn):
    """Set uninvested cash for an investment account and re-derive its balance.

    Not a balance edit: the balance stays derived, this changes one of its two
    inputs. Accounts with no holdings are rejected — their balance is entered
    directly and already includes any cash."""
    con = open_db_rw()
    try:
        fh.ensure(con)
        if not fh.is_derived(con, account_id):
            raise HTTPException(
                409, "this account's balance is entered directly, so it already "
                     "includes cash — add holdings first to make it derived")
        try:
            fh.set_cash(con, account_id, body.amount_eur, body.as_of, note=body.note)
            synced = fh.sync_balance(con, account_id, body.as_of)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        # Vouching for the cash figure absorbs the transfer legs that proposed
        # it, exactly as confirming a balance absorbs the ones behind it.
        absorbed = con.execute(
            "UPDATE finance_tx SET status='applied', applied_at=? "
            "WHERE account_id=? AND status='pending' AND target='cash' AND occurred_on<=?",
            (datetime.datetime.now(TZ).isoformat(), account_id,
             body.as_of or datetime.datetime.now(TZ).date().isoformat())).rowcount
        con.commit()
        return {"status": "ok", "synced": synced, "transactions_applied": absorbed}
    finally:
        con.close()


class BrokerCheckIn(BaseModel):
    reported_eur: float
    as_of: Optional[str] = None
    note: Optional[str] = None


@router.post("/api/finance/holdings/{account_id}/reconcile",
          dependencies=[Depends(require_finance_token)])
def finance_holdings_reconcile(account_id: int, body: BrokerCheckIn):
    """Record the broker's own reported total for this account.

    Stored next to what Acta derived the same day so the gap becomes a series
    rather than a one-off check. Deliberately does not touch finance_balance:
    the derived figure stays the number net worth reads, and this measures its
    error instead of quietly papering over it.
    """
    con = open_db_rw()
    try:
        fh.ensure(con)
        if not fh.is_derived(con, account_id):
            raise HTTPException(
                409, "this account's balance is entered directly, so there is "
                     "nothing derived to reconcile against")
        try:
            check = fh.record_broker_check(con, account_id, body.reported_eur,
                                           body.as_of, note=body.note)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        con.commit()
        return {"status": "ok", "check": check}
    finally:
        con.close()


@router.post("/api/finance/prices/refresh", dependencies=[Depends(require_finance_token)])
def finance_prices_refresh(dry_run: bool = False):
    """Fetch every held symbol's price now and re-derive the balances.

    Runs synchronously — the caller is a button that wants the result, and one
    run is a handful of requests. Partial failure is reported, not raised: the
    symbols that priced are stored and the accounts that fully priced update,
    while the rest keep their previous balance."""
    con = open_db_rw()
    try:
        fh.ensure(con)
        return prices.refresh(con, dry_run=dry_run)
    finally:
        con.close()
