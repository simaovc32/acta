"""Write-only capture API for an external assistant (purchases, food).

A separate key that can only append, never read: a leaked key cannot expose data.
"""

import collections
import datetime
import json
import time
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel

from acta.api.deps import open_db, open_db_rw, run_ingest_quietly
from acta.api.event_log import (
    alcohol_units_for_log,
    append_events_atomic,
    coffee_dupe,
    is_caffeinated_drink,
    remove_event_atomic,
)
from acta.config import TZ
from acta.finance import capture as fc
from acta.finance import holdings as fh
from acta.finance import spend as fs
from acta.tracking import nutrition

router = APIRouter()


# ── assistant capture (write-only) ─────────────────────────────────────────────
# The chat assistant never holds the finance PIN and never reads finance data. It has a
# separate capture key that can add a capped expense and undo its own recent one.
# Only a hash of the key is stored (finance_config.capture_key_hash); the plaintext
# lives in a file only the server's user can read (see tools/capture_key.py).
_capture_fail: "collections.deque" = collections.deque(maxlen=20)   # times of wrong-key attempts


def require_capture_key(x_capture_key: Optional[str] = Header(None)):
    now = time.time()
    while _capture_fail and now - _capture_fail[0] > 60:
        _capture_fail.popleft()
    if len(_capture_fail) >= 10:
        raise HTTPException(429, "too many wrong keys, try again in a minute")
    con = open_db()
    try:
        r = con.execute("SELECT value FROM finance_config WHERE key='capture_key_hash'").fetchone()
    finally:
        con.close()
    if not r:
        raise HTTPException(503, "capture is not set up")
    if not fc.key_ok(x_capture_key, r["value"]):
        _capture_fail.append(now)
        raise HTTPException(401, "wrong capture key")


class CaptureIn(BaseModel):
    amount_eur: float                 # what was paid, positive
    merchant:   str                   # where, or what it was
    item:       Optional[str] = None  # what was bought, if it was eaten now ("mocha")
    size:       Optional[str] = None  # "tall" / "grande" / "venti" ...
    category:   Optional[str] = None  # one of the nine keys; else the merchant's last one
    account:    Optional[str] = None  # a name, or "cash"; else the last-used account
    date:       Optional[str] = None  # YYYY-MM-DD, today unless said
    time:       Optional[str] = None  # HH:MM, for the food entry
    note:       Optional[str] = None


def _eur2(n: float) -> str:
    return f"€{abs(n):.2f}"


@router.post("/api/finance/capture", dependencies=[Depends(require_capture_key)])
def finance_capture(body: CaptureIn, background: BackgroundTasks):
    """Log one purchase from the assistant. Expense-only, capped, always pending. Also logs the
    food when the purchase is a coffee or a meal eaten now AND the item is already in
    the food library (see finance_capture.py). Returns only what it just wrote."""
    amount = round(float(body.amount_eur), 2)
    if not (0 < amount <= fc.MAX_EUR):
        raise HTTPException(400, f"amount must be between €0.01 and €{fc.MAX_EUR:.0f}; "
                                 "larger ones are added on the dashboard")
    merchant = fs.clean_text(body.merchant, 80)
    if not merchant:
        raise HTTPException(400, "merchant is required (where, or what it was)")
    now = datetime.datetime.now(TZ)
    today = now.date()
    try:
        on_d = datetime.date.fromisoformat(body.date) if body.date else today
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD") from None
    if on_d > today:
        raise HTTPException(400, "date cannot be in the future")
    if (today - on_d).days > fc.MAX_AGE_DAYS:
        raise HTTPException(400, f"date is more than {fc.MAX_AGE_DAYS} days ago; add it on the dashboard")
    ts = now
    if body.time:
        try:
            hh, mm = (int(x) for x in body.time.split(":"))
            t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except (ValueError, TypeError):
            raise HTTPException(400, "time must be HH:MM") from None
        ts = t if t <= now else now          # a time later than now cannot have happened

    con = open_db_rw()
    try:
        if fc.captured_today(con, today.isoformat()) >= fc.MAX_PER_DAY:
            raise HTTPException(429, f"capture limit for today reached ({fc.MAX_PER_DAY}); use the dashboard")
        account_id, why = fc.resolve_account(con, body.account, set(fh.holdings_accounts(con)))
        if why:
            raise HTTPException(400, why)
        try:
            category = fs.clean_category(body.category) or fc.guess_category(con, merchant)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        cur = con.execute(
            "INSERT INTO finance_tx(account_id, amount_eur, description, occurred_on, status, "
            "created_at, category, source, note) VALUES(?,?,?,?,'pending',?,?,'whatsapp',?)",
            (account_id, -amount, merchant, on_d.isoformat(), now.isoformat(), category,
             fs.clean_text(body.note or body.item, 200)))
        tx_id = cur.lastrowid
        nutrition.ensure(con)
        plan = fc.plan_food(con, merchant=merchant, item=body.item, size=body.size,
                            category=category, is_today=(on_d == today), ts=ts,
                            is_alcoholic=lambda f: alcohol_units_for_log(f, 100.0) > 0)
        food, event = {"status": "none"}, None
        if plan["action"] == "skip":
            food = {"status": "skipped", "reason": plan["reason"]}
        elif plan["action"] == "log":
            f = plan["item"]
            entry = nutrition.log_food(con, food_id=f["id"], grams=plan["grams"], ts=ts,
                                       note=f"via purchase #{tx_id}")
            con.execute("UPDATE finance_tx SET food_link=? WHERE id=?", (json.dumps(
                {"log_id": entry["id"], "event_dt": None, "event_drink": None}), tx_id))
            food = {"status": "logged", "name": f["name"], "grams": plan["grams"],
                    "portion": plan["label"], "assumed_size": plan["assumed"],
                    "kcal": entry["kcal"], "counted_as_coffee": False}
            if is_caffeinated_drink(f):
                event = {"datetime": ts.strftime("%Y-%m-%d %H:%M"), "type": "coffee", "drink": f["name"]}
        con.commit()
        row = fs.get_row(con, tx_id)
    finally:
        con.close()

    if event is not None:
        new_dt = datetime.datetime.strptime(event["datetime"], "%Y-%m-%d %H:%M")
        try:
            wrote = append_events_atomic([event], skip_if=lambda evs: coffee_dupe(evs, new_dt))
        except (OSError, ValueError) as e:                     # noqa: BLE001
            print(f"capture coffee event failed: {e}")
            wrote = False
        if wrote:
            food["counted_as_coffee"] = True
            con = open_db_rw()
            try:
                con.execute("UPDATE finance_tx SET food_link=? WHERE id=?", (json.dumps(
                    {"log_id": entry["id"], "event_dt": event["datetime"],
                     "event_drink": event["drink"]}), tx_id))
                con.commit()
            finally:
                con.close()
            background.add_task(run_ingest_quietly)

    cat_name = dict(fs.CATEGORIES).get(row["category"]) or "no category"
    msg = f"Logged {_eur2(amount)} · {merchant} · {cat_name} · {row['account']} (#{tx_id})."
    if food["status"] == "logged":
        msg += (f" Also added {food['name']}, {food['portion']}"
                f"{' (size assumed)' if food['assumed_size'] else ''}, to today's food: "
                f"about {round(food['kcal'])} kcal."
                f"{' Counted as a coffee.' if food['counted_as_coffee'] else ''}")
    elif food["status"] == "skipped":
        msg += f" Not added to food: {food['reason']}."
    return {"status": "ok", "id": tx_id, "row": row, "food": food, "message": msg}


class CaptureUndoIn(BaseModel):
    id: int


@router.post("/api/finance/capture/undo", dependencies=[Depends(require_capture_key)])
def finance_capture_undo(body: CaptureUndoIn, background: BackgroundTasks):
    """Undo one of the assistant's own recent, still-pending entries: hides the purchase, removes
    the food entry it created and the coffee event it counted. Nothing else can be
    undone from chat."""
    con = open_db_rw()
    try:
        row = con.execute("SELECT * FROM finance_tx WHERE id=?", (body.id,)).fetchone()
        if not row or row["source"] != "whatsapp":
            raise HTTPException(404, "no such entry logged through the assistant")
        if row["status"] == "dismissed":
            return {"status": "ok", "already": True, "message": f"#{body.id} was already removed."}
        if row["status"] != "pending":
            raise HTTPException(409, "it is already counted in a confirmed balance; remove it on the dashboard")
        age = datetime.datetime.now(TZ) - datetime.datetime.fromisoformat(row["created_at"])
        if age > datetime.timedelta(hours=fc.UNDO_WINDOW_H):
            raise HTTPException(409, f"older than {fc.UNDO_WINDOW_H} hours; remove it on the dashboard")
        con.execute("UPDATE finance_tx SET status='dismissed' WHERE id=?", (body.id,))
        food_removed = event_removed = False
        link = json.loads(row["food_link"]) if row["food_link"] else None
        if link:
            nutrition.ensure(con)
            gone = con.execute("DELETE FROM food_log WHERE id=? AND note=?",
                               (link["log_id"], f"via purchase #{body.id}"))
            food_removed = gone.rowcount > 0
        con.commit()
    finally:
        con.close()
    if link and link.get("event_dt"):
        try:
            event_removed = remove_event_atomic(
                lambda e: e.get("type") == "coffee" and e.get("datetime") == link["event_dt"]
                and e.get("drink") == link.get("event_drink"))
        except (OSError, ValueError) as e:                     # noqa: BLE001
            print(f"capture undo: coffee event not removed: {e}")
        if event_removed:
            background.add_task(run_ingest_quietly)
    msg = f"Removed #{body.id} ({_eur2(row['amount_eur'])} {row['description'] or ''})".rstrip() + "."
    if food_removed:
        msg += " Also removed its food entry" + (" and the coffee count." if event_removed else ".")
    return {"status": "ok", "food_removed": food_removed, "coffee_removed": event_removed, "message": msg}


class FoodCaptureIn(BaseModel):
    item:      Optional[str] = None   # what was eaten/drunk, must match the food library
    merchant:  Optional[str] = None   # breaks ties when several foods share a name
    size:      Optional[str] = None   # "tall" / "grande" / "venti" ...
    time:      Optional[str] = None   # HH:MM, now unless said


@router.post("/api/food/capture", dependencies=[Depends(require_capture_key)])
def food_capture(body: FoodCaptureIn, background: BackgroundTasks):
    """Log one food/drink from the assistant with no purchase attached — for when there is no
    price to give. Same library-only rule as finance_capture's food bridge (never
    invents calories, refuses an item that is not already there); never touches
    finance_tx. Returns only what it just wrote."""
    item = fs.clean_text(body.item, 80)
    if not item:
        raise HTTPException(400, "item is required")
    now = datetime.datetime.now(TZ)
    ts = now
    if body.time:
        try:
            hh, mm = (int(x) for x in body.time.split(":"))
            t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except (ValueError, TypeError):
            raise HTTPException(400, "time must be HH:MM") from None
        ts = t if t <= now else now          # a time later than now cannot have happened

    con = open_db_rw()
    try:
        if fc.captured_food_today(con, now.date().isoformat()) >= fc.MAX_PER_DAY:
            raise HTTPException(429, f"capture limit for today reached ({fc.MAX_PER_DAY}); use the dashboard")
        nutrition.ensure(con)
        plan = fc.plan_food_direct(con, merchant=body.merchant, item=item, size=body.size, ts=ts,
                                   is_alcoholic=lambda f: alcohol_units_for_log(f, 100.0) > 0)
        if plan["action"] == "skip":
            raise HTTPException(400, plan["reason"])
        f = plan["item"]
        entry = nutrition.log_food(con, food_id=f["id"], grams=plan["grams"], ts=ts,
                                   note=fc.ASSISTANT_FOOD_NOTE)
        con.commit()
    finally:
        con.close()

    event = None
    counted_as_coffee = False
    if is_caffeinated_drink(f):
        event = {"datetime": ts.strftime("%Y-%m-%d %H:%M"), "type": "coffee", "drink": f["name"]}
        new_dt = datetime.datetime.strptime(event["datetime"], "%Y-%m-%d %H:%M")
        try:
            wrote = append_events_atomic([event], skip_if=lambda evs: coffee_dupe(evs, new_dt))
        except (OSError, ValueError) as e:                     # noqa: BLE001
            print(f"food capture coffee event failed: {e}")
            wrote = False
        if wrote:
            counted_as_coffee = True
            background.add_task(run_ingest_quietly)

    msg = (f"Logged {f['name']}, {plan['label']}"
           f"{' (size assumed)' if plan['assumed'] else ''}, to today's food: "
           f"about {round(entry['kcal'])} kcal.")
    if counted_as_coffee:
        msg += " Counted as a coffee."
    return {"status": "ok", "log_id": entry["id"], "name": f["name"], "kcal": entry["kcal"],
            "grams": plan["grams"], "portion": plan["label"], "assumed_size": plan["assumed"],
            "counted_as_coffee": counted_as_coffee, "message": msg}


class FoodCaptureUndoIn(BaseModel):
    log_id: int


@router.post("/api/food/capture/undo", dependencies=[Depends(require_capture_key)])
def food_capture_undo(body: FoodCaptureUndoIn, background: BackgroundTasks):
    """Undo one of the assistant's own recent bare food captures: removes the food_log row
    and, if it counted as a coffee, the coffee event too. Only entries this route
    itself wrote (note == fc.ASSISTANT_FOOD_NOTE, so a row edited by hand is left alone,
    same rule as finance_capture_undo) and still within the undo window."""
    con = open_db_rw()
    try:
        row = con.execute("SELECT * FROM food_log WHERE id=?", (body.log_id,)).fetchone()
        if not row or row["note"] != fc.ASSISTANT_FOOD_NOTE:
            raise HTTPException(404, "no such entry logged through the assistant")
        ts = datetime.datetime.fromisoformat(row["ts"])
        age = datetime.datetime.now(TZ) - ts
        if age > datetime.timedelta(hours=fc.UNDO_WINDOW_H):
            raise HTTPException(409, f"older than {fc.UNDO_WINDOW_H} hours; remove it on the dashboard")
        item = con.execute("SELECT * FROM food_item WHERE id=?", (row["food_id"],)).fetchone()
        con.execute("DELETE FROM food_log WHERE id=?", (body.log_id,))
        con.commit()
    finally:
        con.close()

    event_removed = False
    if item and is_caffeinated_drink(dict(item)):
        event_dt = ts.strftime("%Y-%m-%d %H:%M")
        try:
            event_removed = remove_event_atomic(
                lambda e: e.get("type") == "coffee" and e.get("datetime") == event_dt
                and e.get("drink") == item["name"])
        except (OSError, ValueError) as e:                     # noqa: BLE001
            print(f"food capture undo: coffee event not removed: {e}")
        if event_removed:
            background.add_task(run_ingest_quietly)

    msg = f"Removed {row['name']} ({round(row['kcal'])} kcal)."
    if event_removed:
        msg += " Also removed the coffee count."
    return {"status": "ok", "coffee_removed": event_removed, "message": msg}
