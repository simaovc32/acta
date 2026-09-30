"""finance_lib — shared read-only helpers for the subscriptions schedule.

Single source for "which calendar date does a sub land on" so api.py (CRUD +
projections), sensors.py (digest reminders) and monthly_report.py (monthly
section) can't drift into disagreeing about it. Schema ownership (CREATE
TABLE) stays in api.py's init_finance_tables() — this module only reads.
"""
import calendar
import datetime
import sqlite3

from acta import config


def open_ro() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _clamp_day(year: int, month: int, day: int) -> datetime.date:
    last = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, min(day, last))


def next_occurrence(day_of_month: int, on_or_after: datetime.date) -> datetime.date:
    """Next date (>= on_or_after) this sub lands on, clamped to the last day
    of short months (day_of_month=31 -> Feb 28/29)."""
    cand = _clamp_day(on_or_after.year, on_or_after.month, day_of_month)
    if cand < on_or_after:
        y, m = on_or_after.year, on_or_after.month + 1
        if m > 12:
            y, m = y + 1, 1
        cand = _clamp_day(y, m, day_of_month)
    return cand


def occurrences_between(day_of_month: int, start: datetime.date, end: datetime.date) -> list:
    """All dates this sub lands on in (start, end] — the window is exclusive of
    `start` so a sub logged the same day as the prior snapshot isn't double
    counted when snapshots happen to land on a renewal date."""
    out = []
    if start >= end:
        return out
    d = next_occurrence(day_of_month, start + datetime.timedelta(days=1))
    while d <= end:
        out.append(d)
        y, m = d.year, d.month + 1
        if m > 12:
            y, m = y + 1, 1
        d = _clamp_day(y, m, day_of_month)
    return out


def active_subs(con: sqlite3.Connection = None) -> list:
    """[{id, name, amount, direction, day_of_month, account_id, note}], active only."""
    owns = con is None
    con = con or open_ro()
    try:
        rows = con.execute(
            "SELECT id, name, amount, direction, day_of_month, account_id, note "
            "FROM finance_sub WHERE active = 1 ORDER BY day_of_month, name"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []   # pre-migration DB without the table yet
    finally:
        if owns:
            con.close()


def net_amount_between(start: datetime.date, end: datetime.date, con: sqlite3.Connection = None) -> float:
    """Net expected change (in - out) from all active subs landing in (start, end]."""
    subs = active_subs(con)
    total = 0.0
    for s in subs:
        n = len(occurrences_between(s["day_of_month"], start, end))
        if n:
            total += n * s["amount"] * (1 if s["direction"] == "in" else -1)
    return total


def account_events_between(account_id: int, start: datetime.date, end: datetime.date,
                           con: sqlite3.Connection = None) -> list:
    """Sub occurrences tied to ONE account landing in (start, end].

    Account-scoped because "what should this balance be now" is a per-account
    question: net_amount_between() sums every sub regardless of which account
    pays it, which is right for net worth and wrong for one row.
    """
    out = []
    for s in active_subs(con):
        if s["account_id"] != account_id:
            continue
        for d in occurrences_between(s["day_of_month"], start, end):
            out.append({
                "name": s["name"],
                "amount": s["amount"],
                "direction": s["direction"],
                "as_of": d.isoformat(),
                "delta": s["amount"] * (1 if s["direction"] == "in" else -1),
            })
    out.sort(key=lambda e: e["as_of"])
    return out


def monthly_totals(con: sqlite3.Connection = None) -> dict:
    """Sum of active subs by direction, assuming the standard monthly cadence
    (each sub lands once per calendar month) — no cadence field in phase 2."""
    subs = active_subs(con)
    out_total = sum(s["amount"] for s in subs if s["direction"] == "out")
    in_total = sum(s["amount"] for s in subs if s["direction"] == "in")
    return {
        "monthly_out": round(out_total, 2), "monthly_in": round(in_total, 2),
        "annual_out": round(out_total * 12, 2), "annual_in": round(in_total * 12, 2),
    }
