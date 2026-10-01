"""boards.py — native Kanban boards for the Productivity tab.

API-owned tables, same posture as the workout_* tables: created by the API at
start, never touched by ingest.py. Boards, lanes and cards all live in these
tables, with full CRUD here.

Card ordering is a plain integer `position` per lane, re-packed 0..n-1 on every
structural change so it never drifts.
"""

import datetime
import json
import re
import sqlite3

from acta import config

TZ = config.TZ

DEFAULT_LANES = ["Backlog", "This week", "Doing", "Done"]   # last one = done lane
MAX_BOARDS = 40
MAX_TEXT = 400
MAX_BODY = 8000


def _now() -> str:
    return datetime.datetime.now(TZ).isoformat(timespec="seconds")


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "board"


class KanbanError(Exception):
    """Raised for bad requests; the API maps this to a 400/404/409."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# ── schema ────────────────────────────────────────────────────────────────────

def ensure(con: sqlite3.Connection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS kanban_board (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slug        TEXT NOT NULL UNIQUE,
            name        TEXT NOT NULL,
            kind        TEXT NOT NULL DEFAULT 'native',   -- always 'native'; kept for existing DBs
            source_path TEXT,
            accent      TEXT,
            position    INTEGER NOT NULL DEFAULT 0,
            archived    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS kanban_lane (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id     INTEGER NOT NULL REFERENCES kanban_board(id),
            name         TEXT NOT NULL,
            position     INTEGER NOT NULL DEFAULT 0,
            is_done_lane INTEGER NOT NULL DEFAULT 0
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS kanban_card (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            board_id     INTEGER NOT NULL REFERENCES kanban_board(id),
            lane_id      INTEGER NOT NULL REFERENCES kanban_lane(id),
            text         TEXT NOT NULL,
            body         TEXT NOT NULL DEFAULT '',
            tags         TEXT NOT NULL DEFAULT '[]',
            checked      INTEGER NOT NULL DEFAULT 0,
            position     INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            completed_at TEXT
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_kanban_lane_board ON kanban_lane(board_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_kanban_card_lane  ON kanban_card(lane_id)")

    # seed one native board the first time so the tab isn't empty
    seeded = con.execute(
        "SELECT 1 FROM kanban_board WHERE kind='native' LIMIT 1"
    ).fetchone()
    if not seeded:
        _create_board_row(con, "Work", accent="mint", position=0)

    con.commit()


# ── board CRUD ────────────────────────────────────────────────────────────────

def _board_row(con, slug: str):
    row = con.execute("SELECT * FROM kanban_board WHERE slug=?", (slug,)).fetchone()
    if not row:
        raise KanbanError(f"board '{slug}' not found", 404)
    return row


def _create_board_row(con, name: str, accent=None, position=0) -> sqlite3.Row:
    base = _slugify(name)
    slug, n = base, 2
    while con.execute("SELECT 1 FROM kanban_board WHERE slug=?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    now = _now()
    cur = con.execute(
        "INSERT INTO kanban_board(slug,name,kind,accent,position,created_at,updated_at) "
        "VALUES(?,?,'native',?,?,?,?)",
        (slug, name.strip(), accent, position, now, now),
    )
    bid = cur.lastrowid
    for i, lane_name in enumerate(DEFAULT_LANES):
        con.execute(
            "INSERT INTO kanban_lane(board_id,name,position,is_done_lane) VALUES(?,?,?,?)",
            (bid, lane_name, i, 1 if i == len(DEFAULT_LANES) - 1 else 0),
        )
    return con.execute("SELECT * FROM kanban_board WHERE id=?", (bid,)).fetchone()


def list_boards(con: sqlite3.Connection) -> list[dict]:
    """Metadata for every non-archived board."""
    rows = con.execute(
        "SELECT * FROM kanban_board WHERE archived=0 AND kind='native' "
        "ORDER BY position, id"
    ).fetchall()
    out = []
    for r in rows:
        counts = con.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(CASE WHEN checked=0 THEN 1 ELSE 0 END),0) o "
            "FROM kanban_card WHERE board_id=?", (r["id"],)
        ).fetchone()
        out.append({
            "slug": r["slug"], "name": r["name"], "kind": r["kind"],
            "accent": r["accent"], "position": r["position"],
            "card_count": counts["c"], "open_count": counts["o"],
        })
    return out


def get_board(con: sqlite3.Connection, slug: str) -> dict:
    r = _board_row(con, slug)
    if r["kind"] != "native":
        raise KanbanError("not a native board", 400)
    lanes = con.execute(
        "SELECT * FROM kanban_lane WHERE board_id=? ORDER BY position, id", (r["id"],)
    ).fetchall()
    lane_out = []
    for ln in lanes:
        cards = con.execute(
            "SELECT * FROM kanban_card WHERE lane_id=? ORDER BY position, id", (ln["id"],)
        ).fetchall()
        lane_out.append({
            "id": ln["id"], "name": ln["name"], "position": ln["position"],
            "is_done_lane": bool(ln["is_done_lane"]),
            "cards": [_card_dict(c) for c in cards],
        })
    return {
        "slug": r["slug"], "name": r["name"], "kind": "native",
        "accent": r["accent"], "lanes": lane_out,
    }


def _card_dict(c: sqlite3.Row) -> dict:
    try:
        tags = json.loads(c["tags"]) if c["tags"] else []
    except Exception:
        tags = []
    return {
        "id": c["id"], "text": c["text"], "body": c["body"],
        "tags": tags, "checked": bool(c["checked"]),
        "position": c["position"], "completed_at": c["completed_at"],
    }


def create_board(con, name: str, accent=None) -> dict:
    name = (name or "").strip()
    if not name:
        raise KanbanError("board name required")
    if len(name) > 80:
        raise KanbanError("board name too long")
    n = con.execute("SELECT COUNT(*) c FROM kanban_board WHERE archived=0").fetchone()["c"]
    if n >= MAX_BOARDS:
        raise KanbanError("too many boards")
    maxpos = con.execute(
        "SELECT COALESCE(MAX(position),-1) m FROM kanban_board WHERE kind='native'"
    ).fetchone()["m"]
    row = _create_board_row(con, name, accent=accent, position=maxpos + 1)
    con.commit()
    return get_board(con, row["slug"])


def update_board(con, slug: str, *, name=None, accent=None, position=None,
                 archived=None) -> dict:
    r = _board_row(con, slug)
    sets, vals = [], []
    if name is not None:
        name = name.strip()
        if not name:
            raise KanbanError("board name required")
        sets.append("name=?")
        vals.append(name)
    if accent is not None:
        sets.append("accent=?")
        vals.append(accent or None)
    if position is not None:
        sets.append("position=?")
        vals.append(int(position))
    if archived is not None:
        sets.append("archived=?")
        vals.append(1 if archived else 0)
    if not sets:
        raise KanbanError("nothing to update")
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(r["id"])
    con.execute(f"UPDATE kanban_board SET {', '.join(sets)} WHERE id=?", vals)
    con.commit()
    return get_board(con, slug)


def delete_board(con, slug: str) -> None:
    r = _board_row(con, slug)
    # hard delete: native boards carry no historical record worth keeping
    con.execute("DELETE FROM kanban_card WHERE board_id=?", (r["id"],))
    con.execute("DELETE FROM kanban_lane WHERE board_id=?", (r["id"],))
    con.execute("DELETE FROM kanban_board WHERE id=?", (r["id"],))
    con.commit()


# ── lane CRUD ─────────────────────────────────────────────────────────────────

def _native_board(con, slug):
    r = _board_row(con, slug)
    if r["kind"] != "native":
        raise KanbanError("board is read-only", 400)
    return r


def _lane_row(con, lane_id):
    ln = con.execute("SELECT * FROM kanban_lane WHERE id=?", (lane_id,)).fetchone()
    if not ln:
        raise KanbanError("lane not found", 404)
    return ln


def create_lane(con, slug: str, name: str, position=None) -> dict:
    r = _native_board(con, slug)
    name = (name or "").strip()
    if not name:
        raise KanbanError("lane name required")
    if position is None:
        position = con.execute(
            "SELECT COALESCE(MAX(position),-1)+1 p FROM kanban_lane WHERE board_id=?",
            (r["id"],)).fetchone()["p"]
    con.execute(
        "INSERT INTO kanban_lane(board_id,name,position,is_done_lane) VALUES(?,?,?,0)",
        (r["id"], name, int(position)),
    )
    con.commit()
    return get_board(con, slug)


def update_lane(con, lane_id: int, *, name=None, position=None,
                is_done_lane=None) -> dict:
    ln = _lane_row(con, lane_id)
    sets, vals = [], []
    if name is not None:
        name = name.strip()
        if not name:
            raise KanbanError("lane name required")
        sets.append("name=?")
        vals.append(name)
    if position is not None:
        sets.append("position=?")
        vals.append(int(position))
    if is_done_lane is not None:
        sets.append("is_done_lane=?")
        vals.append(1 if is_done_lane else 0)
    if not sets:
        raise KanbanError("nothing to update")
    vals.append(lane_id)
    con.execute(f"UPDATE kanban_lane SET {', '.join(sets)} WHERE id=?", vals)
    con.commit()
    slug = con.execute("SELECT slug FROM kanban_board WHERE id=?", (ln["board_id"],)).fetchone()["slug"]
    return get_board(con, slug)


def delete_lane(con, lane_id: int, force: bool = False) -> dict:
    ln = _lane_row(con, lane_id)
    n = con.execute("SELECT COUNT(*) c FROM kanban_card WHERE lane_id=?", (lane_id,)).fetchone()["c"]
    nlanes = con.execute("SELECT COUNT(*) c FROM kanban_lane WHERE board_id=?", (ln["board_id"],)).fetchone()["c"]
    if nlanes <= 1:
        raise KanbanError("a board needs at least one lane", 400)
    if n and not force:
        raise KanbanError("lane not empty (pass force=1 to delete it and its cards)", 409)
    con.execute("DELETE FROM kanban_card WHERE lane_id=?", (lane_id,))
    con.execute("DELETE FROM kanban_lane WHERE id=?", (lane_id,))
    con.commit()
    slug = con.execute("SELECT slug FROM kanban_board WHERE id=?", (ln["board_id"],)).fetchone()["slug"]
    return get_board(con, slug)


# ── card CRUD ─────────────────────────────────────────────────────────────────

def _card_row(con, card_id):
    c = con.execute("SELECT * FROM kanban_card WHERE id=?", (card_id,)).fetchone()
    if not c:
        raise KanbanError("card not found", 404)
    return c


def _repack(con, lane_id, moved_id=None, at_index=None):
    """Renumber a lane's cards 0..n-1. If moved_id/at_index are given, that card
    is first pulled to position `at_index` in the ordering."""
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM kanban_card WHERE lane_id=? ORDER BY position, id", (lane_id,)
    ).fetchall()]
    if moved_id is not None:
        ids = [i for i in ids if i != moved_id]
        idx = max(0, min(int(at_index) if at_index is not None else len(ids), len(ids)))
        ids.insert(idx, moved_id)
    for i, cid in enumerate(ids):
        con.execute("UPDATE kanban_card SET position=? WHERE id=?", (i, cid))


def _clean_tags(tags) -> str:
    if not isinstance(tags, list):
        return "[]"
    out = [str(x).strip()[:24] for x in tags if str(x).strip()][:8]
    return json.dumps(out, ensure_ascii=False)


def create_card(con, slug: str, lane_id: int, text: str, body="", tags=None) -> dict:
    r = _native_board(con, slug)
    ln = _lane_row(con, lane_id)
    if ln["board_id"] != r["id"]:
        raise KanbanError("lane is not on this board", 400)
    text = (text or "").strip()
    if not text:
        raise KanbanError("card text required")
    if len(text) > MAX_TEXT:
        raise KanbanError("card text too long")
    body = (body or "")[:MAX_BODY]
    pos = con.execute(
        "SELECT COALESCE(MAX(position),-1)+1 p FROM kanban_card WHERE lane_id=?", (lane_id,)
    ).fetchone()["p"]
    now = _now()
    done = bool(ln["is_done_lane"])
    con.execute(
        "INSERT INTO kanban_card"
        "(board_id,lane_id,text,body,tags,checked,position,created_at,updated_at,completed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (r["id"], lane_id, text, body, _clean_tags(tags), 1 if done else 0,
         pos, now, now, now if done else None),
    )
    con.commit()
    return get_board(con, slug)


def update_card(con, card_id: int, *, text=None, body=None, tags=None,
                checked=None, lane_id=None, position=None) -> dict:
    c = _card_row(con, card_id)
    sets, vals = [], []
    now = _now()

    if text is not None:
        text = text.strip()
        if not text:
            raise KanbanError("card text required")
        if len(text) > MAX_TEXT:
            raise KanbanError("card text too long")
        sets.append("text=?")
        vals.append(text)
    if body is not None:
        sets.append("body=?")
        vals.append(body[:MAX_BODY])
    if tags is not None:
        sets.append("tags=?")
        vals.append(_clean_tags(tags))

    src_lane = c["lane_id"]
    target_lane = src_lane
    if lane_id is not None and lane_id != src_lane:
        ln = _lane_row(con, lane_id)
        if ln["board_id"] != c["board_id"]:
            raise KanbanError("target lane is on another board", 400)
        target_lane = lane_id
        sets.append("lane_id=?")
        vals.append(lane_id)
        # moving into / out of a done lane flips checked unless caller overrides
        if checked is None:
            checked = bool(ln["is_done_lane"])

    if checked is not None:
        sets.append("checked=?")
        vals.append(1 if checked else 0)
        sets.append("completed_at=?")
        vals.append(now if checked else None)

    if not sets and position is None:
        raise KanbanError("nothing to update")
    if sets:
        sets.append("updated_at=?")
        vals.append(now)
        con.execute(f"UPDATE kanban_card SET {', '.join(sets)} WHERE id=?",
                    vals + [card_id])

    if target_lane != src_lane:
        _repack(con, src_lane)
        _repack(con, target_lane, moved_id=card_id, at_index=position)
    elif position is not None:
        _repack(con, target_lane, moved_id=card_id, at_index=position)
    con.commit()

    slug = con.execute("SELECT slug FROM kanban_board WHERE id=?", (c["board_id"],)).fetchone()["slug"]
    return get_board(con, slug)


def delete_card(con, card_id: int) -> dict:
    c = _card_row(con, card_id)
    con.execute("DELETE FROM kanban_card WHERE id=?", (card_id,))
    _repack(con, c["lane_id"])
    con.commit()
    slug = con.execute("SELECT slug FROM kanban_board WHERE id=?", (c["board_id"],)).fetchone()["slug"]
    return get_board(con, slug)
