"""The demo person's event log (coffee, alcohol, workouts) in Acta's events.json format.

Written from the same plan as the synthetic strap data, so a logged workout
lines up with the heart-rate bump it caused. The Sunday football games are
deliberately left out: Acta's PAI detector is supposed to find them on its own.
"""

from __future__ import annotations

import datetime as dt
import json

WORKOUT_KIND = {  # plan kind -> (event kind, label, intensity)
    "strength": ("strength", "gym", "moderado"),
    "run": ("cardio", "run", "intenso"),
    "bike": ("cardio", "bike", "moderado"),
}


def _fmt(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M")


def build(days: list, end: dt.datetime) -> list[dict]:
    events = []
    for day in days:
        for c in day.coffees:
            if c <= end:
                events.append({"datetime": _fmt(c), "type": "coffee"})
        for s in day.sessions:
            if not s.logged or s.start + dt.timedelta(minutes=s.minutes) > end:
                continue
            kind, label, intensity = WORKOUT_KIND[s.kind]
            events.append({"datetime": _fmt(s.start), "type": "workout", "duration_min": s.minutes,
                           "kind": kind, "intensity": intensity, "label": label})
        if day.drinks_tonight:
            t = dt.datetime.combine(day.date, dt.time(21, 30), day.bed.tzinfo)
            if t <= end:
                events.append({"datetime": _fmt(t), "type": "alcohol", "kind": "beer",
                               "amount": int(day.drinks_tonight)})
    events.sort(key=lambda e: e["datetime"])
    return events


def write(path: str, days: list, end: dt.datetime) -> list[dict]:
    events = build(days, end)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=1)
    return events
