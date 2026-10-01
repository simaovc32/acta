"""Workouts as markers on the biocharge chart.

The chart shows each logged workout as a gold band with an icon, so it has to know which day a workout
belongs to, when it started and ended, and what it was. Workouts already live in events.json (they feed
biocharge); this module only *reads* them and names them. Nothing here writes or recomputes anything.

Two pure helpers, both covered by test_activities.py:

  guided_kind(title)      what a session finished in the Workout tab was (strength / run / bike), from its
                          title ("<day type> . <variant>", e.g. "Upper Body Pull . Gym", "Steady run . Bike"),
                          so a run or a bike ride done in the app isn't shown or priced as strength.
  day_activities(...)     the workouts overlapping one device-local day, newest windows merged, each with an
                          icon key and a display name for the chart.
"""
import re
from collections.abc import Callable
from typing import Optional

# label slug (pai.ACTIVITY_LABELS) -> (icon key, display name)
LABELS = {
    "football": ("football", "Football"),
    "padel":    ("padel",    "Padel"),
    "run":      ("run",      "Run"),
    "bike":     ("bike",     "Bike"),
    "swim":     ("swim",     "Swim"),
    "gym":      ("gym",      "Gym"),
    "walk":     ("walk",     "Walk"),
    "coaching": ("coaching", "Coaching"),
    "other":    ("other",    "Workout"),
}
# an event with no label falls back on its canonical kind (events written by the Workout tab have none)
KIND_DEFAULT = {"strength": "gym", "cardio": "run", "walk": "walk", "sport": "other", "other": "other"}


def guided_kind(title: Optional[str]) -> tuple[str, Optional[str]]:
    """(kind, label) for a session finished in the Workout tab. Anything not clearly cardio stays strength."""
    t = (title or "").strip().lower()
    head, _, variant = t.partition(" · ")            # the middle dot the app joins type and variant with
    variant = variant.strip()
    if variant in ("bike", "cycling") or re.search(r"\b(bike|cycling|cycle)\b", head):
        return "cardio", "bike"
    if variant == "run" or re.search(r"\b(run|jog|jogging)\b", head):
        return "cardio", "run"
    if head.startswith("cardio"):
        return "cardio", None
    return "strength", None


def describe(kind: Optional[str], label: Optional[str]) -> tuple[str, str]:
    """(icon key, display name) for an event's kind and optional specific label."""
    if label in LABELS:
        return LABELS[label]
    return LABELS[KIND_DEFAULT.get(kind or "other", "other")]


def _clock(dt_str: str, plus_min: int = 0) -> str:
    import datetime
    d = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M") + datetime.timedelta(minutes=plus_min)
    return d.strftime("%H:%M")


def day_activities(events: list, day_start_ms: int, day_end_ms: int,
                   resolve: Callable[[str], int]) -> list[dict]:
    """Workouts overlapping [day_start_ms, day_end_ms), earliest first.

    `resolve` turns an event's wall-clock string into epoch ms against the zone the device was in that day
    (the same rule the coffee and alcohol windows use). Malformed events are skipped, never raised: this
    feeds a chart, and one bad line in events.json must not blank it.

    Two events that overlap are one workout logged twice (a guided session and a detection of the same
    effort): they are merged into one window, keeping the one with a specific label, else the longer."""
    found = []
    for e in events or []:
        if not isinstance(e, dict) or e.get("type") != "workout":
            continue
        try:
            start = int(resolve(e["datetime"]))
            minutes = int(e.get("duration_min") or 0)
            clock, clock_end = _clock(e["datetime"]), _clock(e["datetime"], minutes)
        except (KeyError, ValueError, TypeError):
            continue
        if minutes < 1:
            continue
        end = start + minutes * 60000
        if end <= day_start_ms or start >= day_end_ms:
            continue
        icon, name = describe(e.get("kind"), e.get("label"))
        found.append({"start_ts": start, "end_ts": end, "minutes": minutes, "start": clock, "end": clock_end,
                      "kind": e.get("kind") or "other", "label": e.get("label"), "icon": icon, "name": name,
                      "source": e.get("source")})
    found.sort(key=lambda a: a["start_ts"])

    merged: list[dict] = []
    for a in found:
        if merged and a["start_ts"] < merged[-1]["end_ts"]:
            b = merged[-1]
            if bool(a["label"]) != bool(b["label"]):
                keep = a if a["label"] else b
            else:
                keep = a if a["minutes"] > b["minutes"] else b
            first = a if a["start_ts"] <= b["start_ts"] else b
            last = a if a["end_ts"] >= b["end_ts"] else b
            keep = dict(keep)
            keep["start_ts"], keep["start"] = first["start_ts"], first["start"]
            keep["end_ts"], keep["end"] = last["end_ts"], last["end"]
            keep["minutes"] = round((keep["end_ts"] - keep["start_ts"]) / 60000)
            merged[-1] = keep
        else:
            merged.append(a)
    return merged
