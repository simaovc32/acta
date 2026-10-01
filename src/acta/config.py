"""Runtime configuration: every path and setting Acta needs, in one place.

Everything is read from environment variables, with defaults that keep all
state under ./data so a fresh clone runs without any setup; `acta demo` fills
./data with a synthetic person.

    ACTA_DATA_DIR        where acta.db, events.json and backups live   (./data)
    ACTA_DB              the Acta database                             ($ACTA_DATA_DIR/acta.db)
    ACTA_GADGETBRIDGE_DB the Gadgetbridge export to ingest from        ($ACTA_DATA_DIR/Gadgetbridge.db)
    ACTA_EVENTS          the event log (coffee, alcohol, workouts)     ($ACTA_DATA_DIR/events.json)
    ACTA_TZ              home time zone (days fall back to it)         (this machine's zone)
    ACTA_HOST / ACTA_PORT  where the API listens                       (127.0.0.1:8000)

API keys (OPENROUTER_API_KEY for food lookups and Auspex narration,
TWELVEDATA_API_KEY / TAVILY_API_KEY for prices) are read from the environment
or from $ACTA_DATA_DIR/.env. Every feature that needs one degrades gracefully
without it.
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[2]


def _path(var: str, default: Path) -> str:
    return str(Path(os.environ.get(var) or default).expanduser())


DATA_DIR = _path("ACTA_DATA_DIR", ROOT / "data")
ACTA_DB = _path("ACTA_DB", Path(DATA_DIR) / "acta.db")
GADGETBRIDGE_DB = _path("ACTA_GADGETBRIDGE_DB", Path(DATA_DIR) / "Gadgetbridge.db")
EVENTS_PATH = _path("ACTA_EVENTS", Path(DATA_DIR) / "events.json")
BACKUP_DIR = str(Path(DATA_DIR) / "backups")
REPORT_DIR = str(Path(DATA_DIR) / "reports")
MEDIA_DIR = str(Path(DATA_DIR) / "exercise-videos")
ENV_FILE = str(Path(DATA_DIR) / ".env")
WEB_DIR = _path("ACTA_WEB_DIR", ROOT / "web")

def _local_zone() -> str:
    """The machine's IANA zone name (Linux, macOS and Windows alike). The dashboard
    renders in the browser's zone, so a fresh install should default to the zone
    it runs in."""
    try:
        from tzlocal import get_localzone_name
        return get_localzone_name() or "UTC"
    except Exception:
        return "UTC"


_zone = os.environ.get("ACTA_TZ") or _local_zone()
try:
    TZ = ZoneInfo(_zone)
except ZoneInfoNotFoundError:
    raise SystemExit(f"No time-zone data for {_zone!r}. On Windows, install it with: pip install tzdata") from None

HOST = os.environ.get("ACTA_HOST", "127.0.0.1")
PORT = int(os.environ.get("ACTA_PORT", "8000"))
