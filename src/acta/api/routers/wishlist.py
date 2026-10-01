"""Wish list: things to maybe buy, with want/need ratings that decay over time."""

import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from acta.api.deps import open_db, open_db_rw
from acta.api.finance_auth import require_finance_token
from acta.api.routers.finance import finance_networth
from acta.config import TZ

router = APIRouter()


WISH_STATUSES = {"active", "bought", "dropped"}


class FinanceWishIn(BaseModel):
    name: str
    price: float
    want: int
    need: int
    link: Optional[str] = None


class FinanceWishRateIn(BaseModel):
    want: int
    need: int


class FinanceWishStatusIn(BaseModel):
    status: str


def _validate_wish_rating(want: int, need: int) -> None:
    if not 1 <= want <= 10 or not 1 <= need <= 10:
        raise HTTPException(400, "want/need must be 1-10")


@router.get("/api/finance/wishlist", dependencies=[Depends(require_finance_token)])
def finance_wishlist(status: str = "active"):
    if status not in WISH_STATUSES and status != "all":
        raise HTTPException(400, f"status must be one of {sorted(WISH_STATUSES)} or 'all'")
    con = open_db()
    try:
        if status == "all":
            items = con.execute(
                "SELECT id, name, price, link, status, added_on, bought_on "
                "FROM finance_wish ORDER BY added_on DESC").fetchall()
        else:
            items = con.execute(
                "SELECT id, name, price, link, status, added_on, bought_on "
                "FROM finance_wish WHERE status = ? ORDER BY added_on DESC",
                (status,)).fetchall()
        wish_ids = [r["id"] for r in items]
        ratings_by_wish: dict = {}
        if wish_ids:
            qmarks = ",".join("?" * len(wish_ids))
            rows = con.execute(
                f"SELECT wish_id, ts, want, need FROM finance_wish_rating "
                f"WHERE wish_id IN ({qmarks}) ORDER BY wish_id, ts", wish_ids).fetchall()
            for r in rows:
                ratings_by_wish.setdefault(r["wish_id"], []).append(r)
    finally:
        con.close()

    # A long lookback (400d) so "affordable"/"% of net worth" use the true
    # latest total even if the user hasn't snapshotted in a while.
    nw = finance_networth(days=400)
    total_eur = nw["total_eur"] or 0.0
    saved_per_month = nw["saved_per_month_eur"]
    liquid_eur = sum(
        a["amount_eur"] for a in nw["accounts"]
        if a["kind"] in ("bank", "cash") and a["amount_eur"] is not None)

    today = datetime.datetime.now(TZ).date()
    out = []
    for it in items:
        ratings = ratings_by_wish.get(it["id"], [])
        first, last = (ratings[0], ratings[-1]) if ratings else (None, None)
        added = datetime.date.fromisoformat(it["added_on"])
        out.append({
            "id": it["id"], "name": it["name"], "price": it["price"], "link": it["link"],
            "status": it["status"], "added_on": it["added_on"], "bought_on": it["bought_on"],
            "days_on_list": (today - added).days,
            "want": last["want"] if last else None,
            "need": last["need"] if last else None,
            "want_first": first["want"] if first else None,
            "need_first": first["need"] if first else None,
            "rating_count": len(ratings),
            "pct_of_networth": round(it["price"] / total_eur * 100, 1) if total_eur else None,
            "weeks_of_savings": (round(it["price"] / (saved_per_month / 4.348), 1)
                                 if saved_per_month and saved_per_month > 0 else None),
            "affordable": liquid_eur >= it["price"],
        })
    return {"items": out, "liquid_eur": round(liquid_eur, 2)}


@router.post("/api/finance/wishlist", dependencies=[Depends(require_finance_token)])
def finance_create_wish(body: FinanceWishIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    if body.price <= 0:
        raise HTTPException(400, "price must be positive")
    _validate_wish_rating(body.want, body.need)
    now = datetime.datetime.now(TZ)
    con = open_db_rw()
    try:
        cur = con.execute(
            "INSERT INTO finance_wish(name, price, link, status, added_on, bought_on, created_at) "
            "VALUES(?,?,?,'active',?,NULL,?)",
            (name, body.price, (body.link or "").strip() or None,
             now.date().isoformat(), now.isoformat()))
        wish_id = cur.lastrowid
        con.execute(
            "INSERT INTO finance_wish_rating(wish_id, ts, want, need) VALUES(?,?,?,?)",
            (wish_id, int(now.timestamp() * 1000), body.want, body.need))
        con.commit()
        return {"id": wish_id}
    finally:
        con.close()


@router.post("/api/finance/wishlist/{wish_id}/rate", dependencies=[Depends(require_finance_token)])
def finance_rate_wish(wish_id: int, body: FinanceWishRateIn):
    _validate_wish_rating(body.want, body.need)
    con = open_db_rw()
    try:
        if not con.execute("SELECT 1 FROM finance_wish WHERE id = ?", (wish_id,)).fetchone():
            raise HTTPException(404, "item not found")
        con.execute(
            "INSERT INTO finance_wish_rating(wish_id, ts, want, need) VALUES(?,?,?,?)",
            (wish_id, int(datetime.datetime.now(TZ).timestamp() * 1000), body.want, body.need))
        con.commit()
    finally:
        con.close()
    return {"status": "ok"}


@router.patch("/api/finance/wishlist/{wish_id}", dependencies=[Depends(require_finance_token)])
def finance_patch_wish(wish_id: int, body: FinanceWishStatusIn):
    if body.status not in WISH_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(WISH_STATUSES)}")
    con = open_db_rw()
    try:
        if not con.execute("SELECT 1 FROM finance_wish WHERE id = ?", (wish_id,)).fetchone():
            raise HTTPException(404, "item not found")
        # Marking "bought" only closes out the wish-list entry; it does not
        # create a finance_tx transaction or touch any account.
        bought_on = datetime.datetime.now(TZ).date().isoformat() if body.status == "bought" else None
        con.execute(
            "UPDATE finance_wish SET status = ?, bought_on = ? WHERE id = ?",
            (body.status, bought_on, wish_id))
        con.commit()
    finally:
        con.close()
    return {"status": "ok"}
