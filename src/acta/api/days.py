"""Device-local day boundaries.

Days anchor to the zone the strap was on (recovered per night by ingest),
not the server's zone, so a night slept abroad lands on the right date.
"""

import bisect
import datetime
from typing import Optional

from fastapi import HTTPException

from acta.config import TZ


def tz_intervals(con) -> list[tuple[int, int]]:
    """(start_ms, utc_offset_min) for the device's zone, from sleep_score.

    Each night's offset takes effect at that day's *device-local* midnight, so a
    sample can be bucketed into the day the device was actually on rather than
    the server's zone. Empty list => fall back to the home zone.
    """
    rows = con.execute(
        "SELECT night_of, tz_offset_min FROM sleep_score "
        "WHERE tz_offset_min IS NOT NULL ORDER BY night_of"
    ).fetchall()
    out = []
    for r in rows:
        d  = datetime.date.fromisoformat(r["night_of"])
        tz = datetime.timezone(datetime.timedelta(minutes=r["tz_offset_min"]))
        start = int(datetime.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000)
        out.append((start, r["tz_offset_min"]))
    out.sort()
    return out


def device_tz(ts_ms: int, intervals: list[tuple[int, int]]):
    """Zone the device was on at ts_ms (carries the last known offset forward)."""
    if not intervals:
        return TZ
    i = bisect.bisect_right(intervals, (ts_ms, 10**9)) - 1
    if i < 0:
        return TZ
    return datetime.timezone(datetime.timedelta(minutes=intervals[i][1]))


def device_date(ts_ms: int, intervals: list[tuple[int, int]]) -> str:
    return datetime.datetime.fromtimestamp(
        ts_ms / 1000, device_tz(ts_ms, intervals)).date().isoformat()


def today_bounds_ms() -> tuple[int, int]:
    now   = datetime.datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + datetime.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def check_date(date_iso: str) -> datetime.date:
    """Validate a YYYY-MM-DD path/query date, 400 on anything else."""
    try:
        return datetime.date.fromisoformat(date_iso)
    except (ValueError, TypeError):
        raise HTTPException(400, "date must be YYYY-MM-DD") from None


def _day_offset_min(con, date_iso: str) -> Optional[int]:
    """UTC offset the device was on for a given calendar day.

    Exact when that night was scored; otherwise the most recent earlier offset
    is carried forward (same rule tz_intervals uses). None => caller falls back
    to the home zone.
    """
    row = con.execute(
        "SELECT tz_offset_min FROM sleep_score "
        "WHERE night_of <= ? AND tz_offset_min IS NOT NULL "
        "ORDER BY night_of DESC LIMIT 1",
        (date_iso,),
    ).fetchone()
    return row["tz_offset_min"] if row else None


def day_midnight_ms(con, date_iso: str) -> int:
    """Epoch ms of a calendar day's **device-local** midnight.

    Days must anchor to the zone the device was actually on, not the server's
    zone and not the viewer's — a night slept abroad otherwise lands on the
    wrong date.
    """
    d   = datetime.date.fromisoformat(date_iso)
    off = _day_offset_min(con, date_iso)
    tz  = TZ if off is None else datetime.timezone(datetime.timedelta(minutes=off))
    return int(datetime.datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000)


def day_bounds_ms(con, date_iso: str) -> tuple[int, int]:
    """(start_ms, end_ms) for one device-local calendar day.

    The end is the *next* day's own midnight rather than start+24h, so a
    timezone-transition day is its real 23h or 25h length instead of silently
    overlapping or gapping against its neighbour.
    """
    d    = datetime.date.fromisoformat(date_iso)
    nxt  = (d + datetime.timedelta(days=1)).isoformat()
    return day_midnight_ms(con, date_iso), day_midnight_ms(con, nxt)
