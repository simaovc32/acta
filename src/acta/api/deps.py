"""Shared plumbing for the routers: database handles, freshness, the ingest trigger."""

import datetime
import sqlite3
import subprocess
import sys
from typing import Optional

from acta import config
from acta.config import TZ

INGEST_CMD = [sys.executable, "-m", "acta.pipeline.ingest"]


def open_db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def open_db_rw() -> sqlite3.Connection:
    # timeout=30 (vs sqlite3's 5s default): acta-ingest.timer briefly holds a write
    # lock every ~5 min, so an API write waits instead of failing with "database is
    # locked". Paired with WAL mode (set once on the DB file), which shortens that window.
    con = sqlite3.connect(config.ACTA_DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def open_gb() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{config.GADGETBRIDGE_DB}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def last_updated(con: sqlite3.Connection) -> Optional[str]:
    ts1 = con.execute("SELECT MAX(computed_at) FROM biocharge").fetchone()[0]
    ts2 = con.execute("SELECT MAX(computed_at) FROM sleep_score").fetchone()[0]
    row = con.execute("SELECT started_at FROM ingest_run ORDER BY id DESC LIMIT 1").fetchone()
    ts3 = row[0] if row else None
    candidates = [t for t in (ts1, ts2, ts3) if t]
    if not candidates:
        return None
    best = max(candidates)
    dt = datetime.datetime.fromisoformat(best).astimezone(TZ)
    return dt.strftime("%H:%M")


# One timeout for every ingest call site, sized for a full run (biocharge replay
# plus calories and PAI), including /api/refresh where a person is waiting.
INGEST_TIMEOUT_S = 180


def trigger_ingest():
    """Recompute biocharge after an events.json change. Failures are logged by
    the caller's task runner; the event is already durably written by then."""
    return subprocess.run(
        INGEST_CMD,
        capture_output=True, text=True, timeout=INGEST_TIMEOUT_S
    )


def run_ingest_quietly() -> None:
    """Recompute after events.json changed (the same call append_modifier_event makes)."""
    try:
        subprocess.run(INGEST_CMD,
                       capture_output=True, text=True, timeout=INGEST_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"ingest after capture failed: {e}")
