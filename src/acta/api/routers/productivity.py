"""Productivity tab: Kanban boards and the weekly schedule template."""

import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from acta.api.deps import open_db, open_db_rw
from acta.config import TZ
from acta.productivity import boards

router = APIRouter()


# The weekly schedule is a recurring template — a block added once shows every
# week until it is deleted (same idea as workout_plan's 7-day template). Flat
# single-colour chips (a deliberate flat look); hues sit in the panel-safe
# lightness band so they read in the dark theme.
SCHEDULE_CATEGORIES = [
    {"key": "deep",     "label": "Deep work",     "color": "oklch(0.78 0.11 175)"},
    {"key": "business", "label": "Business",      "color": "oklch(0.72 0.11 245)"},
    {"key": "class",    "label": "Class / study", "color": "oklch(0.72 0.12 300)"},
    {"key": "training", "label": "Training",      "color": "oklch(0.76 0.12 65)"},
    {"key": "personal", "label": "Personal",      "color": "oklch(0.72 0.11 15)"},
    {"key": "admin",    "label": "Admin",         "color": "oklch(0.64 0.02 250)"},
]


SCHEDULE_CAT_KEYS = {c["key"] for c in SCHEDULE_CATEGORIES}


class BoardIn(BaseModel):
    name: str
    accent: Optional[str] = None


class BoardPatch(BaseModel):
    name: Optional[str] = None
    accent: Optional[str] = None
    position: Optional[int] = None
    archived: Optional[bool] = None


class LaneIn(BaseModel):
    name: str
    position: Optional[int] = None


class LanePatch(BaseModel):
    name: Optional[str] = None
    position: Optional[int] = None
    is_done_lane: Optional[bool] = None


class CardIn(BaseModel):
    lane_id: int
    text: str
    body: Optional[str] = ""
    tags: Optional[list] = None


class CardPatch(BaseModel):
    text: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[list] = None
    checked: Optional[bool] = None
    lane_id: Optional[int] = None
    position: Optional[int] = None


def _kanban_call(fn, *a, **kw):
    """Run a kanban_store mutation on a rw connection, mapping KanbanError."""
    con = open_db_rw()
    try:
        return fn(con, *a, **kw)
    except boards.KanbanError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    finally:
        con.close()


@router.get("/api/kanban/boards")
def kanban_list_boards():
    con = open_db_rw()
    try:
        return {"boards": boards.list_boards(con)}
    finally:
        con.close()


@router.get("/api/kanban/boards/{slug}")
def kanban_get_board(slug: str):
    return _kanban_call(boards.get_board, slug)


@router.post("/api/kanban/boards")
def kanban_create_board(b: BoardIn):
    return _kanban_call(boards.create_board, b.name, accent=b.accent)


@router.patch("/api/kanban/boards/{slug}")
def kanban_update_board(slug: str, p: BoardPatch):
    return _kanban_call(boards.update_board, slug, name=p.name, accent=p.accent,
                        position=p.position, archived=p.archived)


@router.delete("/api/kanban/boards/{slug}")
def kanban_delete_board(slug: str):
    _kanban_call(boards.delete_board, slug)
    return {"status": "ok"}


@router.post("/api/kanban/boards/{slug}/lanes")
def kanban_create_lane(slug: str, ln: LaneIn):
    return _kanban_call(boards.create_lane, slug, ln.name, position=ln.position)


@router.patch("/api/kanban/lanes/{lane_id}")
def kanban_update_lane(lane_id: int, p: LanePatch):
    return _kanban_call(boards.update_lane, lane_id, name=p.name,
                        position=p.position, is_done_lane=p.is_done_lane)


@router.delete("/api/kanban/lanes/{lane_id}")
def kanban_delete_lane(lane_id: int, force: bool = Query(False)):
    return _kanban_call(boards.delete_lane, lane_id, force=force)


@router.post("/api/kanban/boards/{slug}/cards")
def kanban_create_card(slug: str, c: CardIn):
    return _kanban_call(boards.create_card, slug, c.lane_id, c.text,
                        body=c.body or "", tags=c.tags)


@router.patch("/api/kanban/cards/{card_id}")
def kanban_update_card(card_id: int, p: CardPatch):
    return _kanban_call(boards.update_card, card_id, text=p.text, body=p.body,
                        tags=p.tags, checked=p.checked, lane_id=p.lane_id,
                        position=p.position)


@router.delete("/api/kanban/cards/{card_id}")
def kanban_delete_card(card_id: int):
    return _kanban_call(boards.delete_card, card_id)


class ScheduleBlockIn(BaseModel):
    dow: int
    start_min: int
    end_min: int
    title: str
    category: str = "deep"
    note: Optional[str] = ""


class ScheduleBlockPatch(BaseModel):
    dow: Optional[int] = None
    start_min: Optional[int] = None
    end_min: Optional[int] = None
    title: Optional[str] = None
    category: Optional[str] = None
    note: Optional[str] = None


def _validate_block(dow, start_min, end_min, category):
    if not (0 <= dow <= 6):
        raise HTTPException(400, "dow must be 0-6 (Mon-Sun)")
    if not (0 <= start_min < end_min <= 24 * 60):
        raise HTTPException(400, "need 0 <= start_min < end_min <= 1440")
    if category not in SCHEDULE_CAT_KEYS:
        raise HTTPException(400, f"category must be one of {sorted(SCHEDULE_CAT_KEYS)}")


@router.get("/api/schedule/categories")
def schedule_categories():
    return {"categories": SCHEDULE_CATEGORIES}


@router.get("/api/schedule/blocks")
def schedule_get_blocks():
    con = open_db()
    try:
        rows = con.execute(
            "SELECT id, dow, start_min, end_min, title, category, note "
            "FROM schedule_block ORDER BY dow, start_min"
        ).fetchall()
    finally:
        con.close()
    return {"blocks": [dict(r) for r in rows]}


@router.post("/api/schedule/blocks")
def schedule_create_block(b: ScheduleBlockIn):
    title = (b.title or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    if len(title) > 120:
        raise HTTPException(400, "title too long")
    _validate_block(b.dow, b.start_min, b.end_min, b.category)
    now = datetime.datetime.now(TZ).isoformat(timespec="seconds")
    con = open_db_rw()
    try:
        cur = con.execute(
            "INSERT INTO schedule_block"
            "(dow,start_min,end_min,title,category,note,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (b.dow, b.start_min, b.end_min, title, b.category,
             (b.note or "").strip()[:2000], now, now),
        )
        con.commit()
        bid = cur.lastrowid
    finally:
        con.close()
    return {"status": "ok", "id": bid}


@router.patch("/api/schedule/blocks/{block_id}")
def schedule_update_block(block_id: int, p: ScheduleBlockPatch):
    con = open_db_rw()
    try:
        row = con.execute("SELECT * FROM schedule_block WHERE id=?", (block_id,)).fetchone()
        if not row:
            raise HTTPException(404, "block not found")
        dow = row["dow"] if p.dow is None else p.dow
        start_min = row["start_min"] if p.start_min is None else p.start_min
        end_min = row["end_min"] if p.end_min is None else p.end_min
        category = row["category"] if p.category is None else p.category
        _validate_block(dow, start_min, end_min, category)
        title = row["title"] if p.title is None else (p.title or "").strip()
        if not title:
            raise HTTPException(400, "title required")
        note = row["note"] if p.note is None else (p.note or "").strip()[:2000]
        now = datetime.datetime.now(TZ).isoformat(timespec="seconds")
        con.execute(
            "UPDATE schedule_block SET dow=?,start_min=?,end_min=?,title=?,"
            "category=?,note=?,updated_at=? WHERE id=?",
            (dow, start_min, end_min, title[:120], category, note, now, block_id),
        )
        con.commit()
    finally:
        con.close()
    return {"status": "ok"}


@router.delete("/api/schedule/blocks/{block_id}")
def schedule_delete_block(block_id: int):
    con = open_db_rw()
    try:
        con.execute("DELETE FROM schedule_block WHERE id=?", (block_id,))
        con.commit()
    finally:
        con.close()
    return {"status": "ok"}
