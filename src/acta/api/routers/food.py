"""Food and drink: the item library, meals, the intake log."""

import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from acta.api.days import check_date
from acta.api.deps import open_db_rw
from acta.api.event_log import ETHANOL_G_PER_ML, alcohol_units_for_log, append_modifier_event, is_caffeinated_drink
from acta.config import TZ
from acta.tracking import body, nutrition

router = APIRouter()


@router.get("/api/food/library")
def food_library(q: str = "", kind: Optional[str] = None,
                 limit: int = Query(20, ge=1, le=100)):
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        return {"items": nutrition.search(con, q, kind, limit)}
    finally:
        con.close()


class FoodResolveIn(BaseModel):
    description: str


@router.post("/api/food/resolve")
def food_resolve(body_in: FoodResolveIn):
    """Estimate a new food's nutrition. Never writes — the user confirms first."""
    try:
        return {"status": "ok", "result": nutrition.resolve(body_in.description)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:                                   # noqa: BLE001
        # Surface the failure rather than guessing a number: the user can still
        # type the calories by hand, which is the whole fallback path.
        raise HTTPException(502, f"estimate failed: {e}") from e


class FoodSizeIn(BaseModel):
    label: str
    grams: float


class FoodItemIn(BaseModel):
    name:          str
    kcal_100g:     float
    kind:          str = "food"
    protein_g:     Optional[float] = None
    carbs_g:       Optional[float] = None
    fat_g:         Optional[float] = None
    portion_g:     Optional[float] = None
    portion_label: Optional[str]   = None
    # Named sizes (Starbucks Tall/Grande/Venti, Solo/Doppio, ...). Omitted
    # (None) leaves whatever the item already has untouched; [] clears it.
    sizes:         Optional[list[FoodSizeIn]] = None
    # Alcohol by volume %, for drinks. None leaves it untouched on an update;
    # 0 marks it confirmed non-alcoholic. Drives the alcohol event on logging.
    abv_pct:       Optional[float] = None
    source:        str = "manual"


@router.post("/api/food/item")
def food_item_save(item: FoodItemIn):
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        try:
            saved = nutrition.upsert_item(
                con, item.name, item.kcal_100g, kind=item.kind,
                protein_g=item.protein_g, carbs_g=item.carbs_g, fat_g=item.fat_g,
                portion_g=item.portion_g, portion_label=item.portion_label,
                sizes=[s.model_dump() for s in item.sizes] if item.sizes is not None else None,
                abv_pct=item.abv_pct,
                source=item.source if item.source in ("manual", "llm") else "manual")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        con.commit()
        return {"status": "ok", "item": saved}
    finally:
        con.close()


class FoodPinIn(BaseModel):
    pinned: bool


@router.post("/api/food/item/{food_id}/pin")
def food_item_pin(food_id: int, body_in: FoodPinIn):
    """Pin/unpin a one-tap button. Stays in the library either way."""
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        if nutrition.get_item(con, food_id) is None:
            raise HTTPException(404, "no such food")
        nutrition.set_pinned(con, food_id, body_in.pinned)
        con.commit()
        return {"status": "ok", "pinned": body_in.pinned}
    finally:
        con.close()


@router.delete("/api/food/item/{food_id}")
def food_item_delete(food_id: int):
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        nutrition.delete_item(con, food_id)
        con.commit()
        return {"status": "ok"}
    finally:
        con.close()


class FoodLogIn(BaseModel):
    food_id:   Optional[int] = None
    name:      Optional[str] = None
    grams:     float
    time:      Optional[str] = None   # "HH:MM"; defaults to now
    slot:      Optional[str] = None   # meal slot; derived from the clock if unset
    note:      Optional[str] = None
    # Ad-hoc / "don't remember this food" path: hand-typed numbers logged once,
    # never written to food_item. Present together (no food_id) => adhoc log.
    kcal_100g: Optional[float] = None
    protein_g: Optional[float] = None
    # Explicit override for the coffee/alcohol event mirror, only meaningful on
    # an adhoc log — there's no food_item for is_caffeinated_drink()/ABV to read.
    caffeine:  Optional[bool]  = None
    abv_pct:   Optional[float] = None


def _parse_hhmm(hhmm: Optional[str]) -> datetime.datetime:
    """Today at HH:MM, or now when unset. Shared by the food and meal loggers."""
    ts = datetime.datetime.now(TZ)
    if not hhmm:
        return ts
    try:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return ts.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except (ValueError, TypeError):
        raise HTTPException(400, "time must be HH:MM") from None


@router.post("/api/food/log")
def food_log_add(entry: FoodLogIn, background: BackgroundTasks):
    ts = _parse_hhmm(entry.time)

    adhoc = entry.food_id is None and entry.kcal_100g is not None
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        try:
            if adhoc:
                row = nutrition.log_food_adhoc(
                    con, name=entry.name, kcal_100g=entry.kcal_100g,
                    grams=entry.grams, protein_g=entry.protein_g, ts=ts,
                    note=entry.note, slot=entry.slot)
            else:
                row = nutrition.log_food(con, food_id=entry.food_id, name=entry.name,
                                         grams=entry.grams, ts=ts, note=entry.note,
                                         slot=entry.slot)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        item = nutrition.get_item(con, row["food_id"]) if row["food_id"] else None
        con.commit()
        totals = nutrition.day_totals(con, ts.date().isoformat())
    finally:
        con.close()

    # Mirror caffeine/alcohol into events.json so the existing biocharge and
    # sleep-score paths see them. Runs in the background: appending triggers a
    # full ingest recompute, which must not block the log button. An adhoc log
    # has no food_item to read kind/abv_pct from, so it carries its own
    # caffeine/abv_pct override instead of being derived from `item`.
    dt_str = ts.strftime("%Y-%m-%d %H:%M")
    is_coffee = entry.caffeine if entry.caffeine is not None else is_caffeinated_drink(item)
    if is_coffee:
        background.add_task(append_modifier_event, dt_str, "coffee", None, None)
    else:
        if adhoc and entry.abv_pct:
            units = round(float(entry.grams) * (entry.abv_pct / 100.0)
                          * ETHANOL_G_PER_ML / 10.0, 3)
        else:
            units = alcohol_units_for_log(item, entry.grams)
        if units > 0:
            # amount=1 (this one serving), units carried explicitly so biocharge
            # doesn't have to recognise the drink by name.
            background.add_task(append_modifier_event, dt_str, "alcohol",
                                1, None, units, item["name"] if item else entry.name)

    return {"status": "ok", "entry": row, "totals": totals}


class MealComponentIn(BaseModel):
    """Either an existing library food (food_id) or a new one to be created."""
    food_id:   Optional[int] = None
    name:      Optional[str] = None
    grams:     float
    kcal_100g: Optional[float] = None
    protein_g: Optional[float] = None
    carbs_g:   Optional[float] = None
    fat_g:     Optional[float] = None


class MealLogIn(BaseModel):
    name:       str
    components: list[MealComponentIn]
    time:       Optional[str] = None    # "HH:MM"; defaults to now
    slot:       Optional[str] = None    # meal slot; derived from the clock if unset
    save:       bool = False            # also store it as a reusable meal


@router.post("/api/food/meal/log")
def food_meal_log(entry: MealLogIn):
    """Log a multi-component dish in one call.

    Components arrive either as an existing food_id or as a new food to add to
    the library first — so the ingredients of a dish become reusable on their
    own, and the same plate scores the same next time.
    """
    if not entry.components:
        raise HTTPException(400, "a meal needs at least one component")
    ts = _parse_hhmm(entry.time)

    con = open_db_rw()
    try:
        nutrition.ensure(con)
        resolved = []
        try:
            for c in entry.components:
                food_id = c.food_id
                if food_id is None:
                    if not (c.name or "").strip() or c.kcal_100g is None:
                        raise ValueError("a new component needs a name and kcal_100g")
                    item = nutrition.upsert_item(
                        con, c.name, c.kcal_100g, kind="food",
                        protein_g=c.protein_g, carbs_g=c.carbs_g, fat_g=c.fat_g,
                        portion_g=c.grams, source="llm")
                    food_id = item["id"]
                elif nutrition.get_item(con, food_id) is None:
                    raise ValueError(f"no such food: {food_id}")
                resolved.append({"food_id": food_id, "name": c.name,
                                 "grams": c.grams})
            out = nutrition.log_meal(con, name=entry.name, components=resolved,
                                     ts=ts, slot=entry.slot)
            saved = nutrition.save_meal(con, entry.name, resolved) if entry.save else None
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        con.commit()
        totals = nutrition.day_totals(con, ts.date().isoformat())
        return {"status": "ok", "meal": out, "saved": saved, "totals": totals}
    finally:
        con.close()


@router.get("/api/food/meals")
def food_meals():
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        return {"meals": nutrition.list_meals(con)}
    finally:
        con.close()


class MealRelogIn(BaseModel):
    time: Optional[str] = None
    slot: Optional[str] = None


@router.post("/api/food/meal/{meal_id}/log")
def food_meal_relog(meal_id: int, entry: Optional[MealRelogIn] = None):
    """Re-log a saved dish with its stored amounts — the one-tap path."""
    ts = _parse_hhmm(entry.time if entry else None)
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        try:
            out = nutrition.log_saved_meal(con, meal_id, ts=ts,
                                           slot=entry.slot if entry else None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        con.commit()
        return {"status": "ok", "meal": out,
                "totals": nutrition.day_totals(con, ts.date().isoformat())}
    finally:
        con.close()


@router.delete("/api/food/meal/{meal_id}")
def food_meal_delete(meal_id: int):
    """Forget a saved dish. Rows already logged from it keep their own values."""
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        nutrition.delete_meal(con, meal_id)
        con.commit()
        return {"status": "ok"}
    finally:
        con.close()


@router.post("/api/food/meal/{meal_id}/pin")
def food_meal_pin(meal_id: int, body_in: FoodPinIn):
    """Pin/unpin a saved dish's one-tap button. Stays a saved dish either way."""
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        if nutrition.get_meal(con, meal_id) is None:
            raise HTTPException(404, "no such dish")
        nutrition.set_meal_pinned(con, meal_id, body_in.pinned)
        con.commit()
        return {"status": "ok", "pinned": body_in.pinned}
    finally:
        con.close()


class LogSlotIn(BaseModel):
    slot: str


@router.patch("/api/food/log/{log_id}")
def food_log_move(log_id: int, entry: LogSlotIn):
    """Move a logged entry to another meal — the correction path for an item the
    clock filed in the wrong slot."""
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        try:
            nutrition.set_slot(con, log_id, entry.slot)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        con.commit()
        return {"status": "ok", "id": log_id, "slot": entry.slot}
    finally:
        con.close()


@router.patch("/api/food/log/meal/{meal_id}")
def food_log_meal_move(meal_id: str, entry: LogSlotIn):
    """Move every row of one logged dish together — its parts are one meal."""
    if entry.slot not in nutrition.MEAL_SLOTS:
        raise HTTPException(400, "unknown meal slot")
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        cur = con.execute("UPDATE food_log SET meal_slot=? WHERE meal_id=?",
                          (entry.slot, meal_id))
        con.commit()
        return {"status": "ok", "moved": cur.rowcount, "slot": entry.slot}
    finally:
        con.close()


@router.delete("/api/food/log/meal/{meal_id}")
def food_log_meal_delete(meal_id: str):
    """Remove every row of one logged meal, scoped to that meal_id alone."""
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        n = nutrition.delete_meal_log(con, meal_id)
        con.commit()
        return {"status": "ok", "removed": n}
    finally:
        con.close()


@router.delete("/api/food/log/{log_id}")
def food_log_delete(log_id: int):
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        nutrition.delete_log(con, log_id)
        con.commit()
        return {"status": "ok"}
    finally:
        con.close()


def _protein_progress(con, date_iso: str, logged_g: float) -> Optional[dict]:
    """Protein logged so far against the derived target, or None if no target.

    Progress is computed here rather than in the frontend so the rounding rule
    lives in exactly one place — the target is already rounded to 5 g, and a
    percentage recomputed from the unrounded figure would disagree with it.
    """
    target = body.protein_target(con, date_iso)
    if target is None:
        return None
    logged = round(float(logged_g or 0.0), 1)
    return {
        "logged_g": logged,
        "target_g": target["target_g"],
        "remaining_g": round(max(0.0, target["target_g"] - logged), 1),
        "pct": round(logged / target["target_g"] * 100) if target["target_g"] else None,
        "g_per_kg": target["g_per_kg"],
        "weight_kg": target["weight_kg"],
    }


@router.get("/api/food/today")
def food_today(date: Optional[str] = None):
    """Intake for today, or for `date` — plus the balance against energy out."""
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        body.ensure(con)
        if date:
            check_date(date)
            today = date
        else:
            today = datetime.datetime.now(TZ).date().isoformat()
        totals = nutrition.day_totals(con, today)
        entries = nutrition.day_log(con, today)
        row = con.execute(
            "SELECT total_kcal FROM daily_energy WHERE date=?", (today,)).fetchone()
        out_kcal = row["total_kcal"] if row else None
        return {
            "date": today, "entries": entries, "totals": totals,
            # Always all six meal columns, in day order — an empty one is information.
            "slots": nutrition.day_by_slot(con, today),
            "energy_out": out_kcal,
            # Positive = eaten more than burned so far today. Both sides are
            # partial until the day ends, so this is a running figure.
            "balance": (round(totals["kcal"] - out_kcal)
                        if out_kcal is not None else None),
            # None when no weight is logged — the frontend shows the running
            # total alone rather than inventing a target to compare against.
            "protein": _protein_progress(con, today, totals["protein_g"]),
        }
    finally:
        con.close()


@router.get("/api/food/history")
def food_history(days: int = Query(30, ge=2, le=180)):
    con = open_db_rw()
    try:
        nutrition.ensure(con)
        return {"series": nutrition.history(con, days)}
    finally:
        con.close()
