"""events.json: the append-only log of coffee, alcohol and workouts.

Biocharge and the sleep score read it on every ingest, so every write goes
through one atomic, locked path here.
"""

import datetime
import json
import os
import subprocess
from contextlib import contextmanager
from typing import Optional

from acta import config
from acta.api.deps import INGEST_CMD, INGEST_TIMEOUT_S
from acta.tracking import nutrition


@contextmanager
def _events_lock():
    """Exclusive lock on the event log for one read-modify-write.

    Every writer (API threads, the capture API) goes through here, so two
    appends landing at once can't lose one another. fcntl on Linux/macOS,
    msvcrt on Windows."""
    with open(config.EVENTS_PATH + ".lock", "a+") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def append_workout_event(dt_str: str, duration_min: int, kind: str,
                         intensity: Optional[str] = None,
                         source: Optional[str] = None,
                         label: Optional[str] = None) -> subprocess.CompletedProcess:
    """Append a workout event to events.json and trigger a biocharge recompute.
    Shared by POST /api/log/workout (inline), POST /api/workout/session and
    PAI detection confirms (both via BackgroundTasks). Caller is responsible
    for validating inputs.

    `source` marks provenance ("pai" for a confirmed detection) so an inferred
    session stays distinguishable from one the user logged directly, forever --
    a later analysis can weight or exclude them. `label` keeps the specific
    activity ("football") next to the canonical kind ("sport")."""
    write_event_only(dt_str, duration_min, kind, intensity, source, label)
    # Trigger full recompute so the new event is reflected in biocharge
    return subprocess.run(
        INGEST_CMD,
        capture_output=True, text=True, timeout=INGEST_TIMEOUT_S
    )


def append_events_atomic(new_events: list[dict], *, skip_if=None) -> bool:
    """Append events to events.json under an exclusive lock, atomically.

    The single writer for this file. Every path that adds an event goes through
    here — workouts, PAI confirms, and the coffee/alcohol modifiers — because the
    naive read-modify-write they used to do individually is unsafe in two ways:

      * Lost updates. Two writers dispatched to the threadpool (an assistant coffee
        and a PAI confirm, say) both read N events and both write N+1, so one is
        gone for good. The flock serialises them.
      * Torn reads. open(...,"w") truncates the real file, so ingest reading it
        mid-write gets a JSONDecodeError — and biocharge.load_events() only
        catches FileNotFoundError, so that kills the run. Writing a temp file and
        os.replace()-ing it is atomic on POSIX: a reader sees either the old
        complete file or the new one, never a half-written one.

    `skip_if(existing_events)` is evaluated INSIDE the lock and may veto the
    write, so a dedupe rule judges the file as it is about to be written rather
    than a copy read moments earlier. Returns True when events were written.

    A corrupt events.json deliberately raises rather than being silently reset:
    starting from [] here would discard the entire logged history.
    """
    if not new_events:
        return False
    with _events_lock():
        try:
            with open(config.EVENTS_PATH, encoding="utf-8") as f:
                events = json.load(f)
        except FileNotFoundError:
            events = []
        if skip_if is not None and skip_if(events):
            return False
        events.extend(new_events)
        events.sort(key=lambda e: e["datetime"])
        tmp = config.EVENTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config.EVENTS_PATH)
        return True


def write_event_only(dt_str: str, duration_min: int, kind: str,
                     intensity: Optional[str] = None,
                     source: Optional[str] = None,
                     label: Optional[str] = None) -> None:
    """Append one workout event to events.json, atomically and under a lock.

    The read-modify-write here used to be unguarded, which loses events under
    concurrency: two confirms dispatched to the threadpool both read N events
    and both write N+1, so one is gone for good. Worse, the old code truncated
    the real file with open(...,"w"), so ingest reading mid-write got a
    JSONDecodeError -- and biocharge.load_events() only catches
    FileNotFoundError, so that aborts the run.

    Two guards: an exclusive flock over the whole read-modify-write (so
    concurrent writers serialise), and a write to a temp file followed by
    os.replace (atomic on POSIX, so a reader ever sees either the old complete
    file or the new one, never a truncated one).
    """
    event = {
        "datetime":     dt_str,
        "type":         "workout",
        "duration_min": duration_min,
        "kind":         kind,
    }
    if intensity:
        event["intensity"] = intensity
    if source:
        event["source"] = source
    if label:
        event["label"] = label

    append_events_atomic([event])


# 1 EU standard unit = 10 g pure ethanol; ethanol density 0.789 g/ml. A drink's
# units for a logged amount = ml * abv/100 * 0.789 / 10 (see biocharge.py's
# ALCOHOL_UNITS_PER_DRINK comment, which pre-computes this for the seeded pours).
ETHANOL_G_PER_ML = 0.789


def alcohol_units_for_log(item: Optional[dict], grams: float) -> float:
    """Standard-drink units for this exact logged amount, from the drink's stored
    ABV. `grams` doubles as ml for a drink. 0 unless it's an alcoholic drink.

    Replaces a 3-name whitelist ({beer, wine, cider}) that silently ignored every
    other alcoholic drink — the same class of gap fixed for caffeine 2026-08-12.
    """
    if not item or item.get("kind") != "drink":
        return 0.0
    abv = item.get("abv_pct") or 0.0
    if abv <= 0:
        return 0.0
    return round(float(grams) * (abv / 100.0) * ETHANOL_G_PER_ML / 10.0, 3)


# Caffeinated drinks recognised when logged through food logging, so a latte or
# mocha feeds the same coffee event as the "How I Feel" modal's coffee toggle.
# Matched as substrings of nutrition.norm_key() (accent-stripped, lowercased),
# so "Starbucks Caffè Mocha" → "starbucks caffe mocha" hits "caffe" and "mocha".
# Substring rather than an exact key set on purpose: the previous exact-match on
# "coffee" silently ignored every espresso drink in the library.
CAFFEINE_DRINK_TOKENS = (
    "coffee", "caffe", "cafe", "espresso", "latte", "mocha", "macchiato",
    "cappuccino", "americano", "flat white", "cortado", "galao", "abatanado",
    "carioca", "meia de leite",
)


# Decaf names contain the same tokens ("descafeinado" ⊃ "cafe") but no caffeine.
DECAF_TOKENS = ("decaf", "descafeinado", "sem cafeina")


def is_caffeinated_drink(item: Optional[dict]) -> bool:
    """True when a logged food_item should also raise a coffee event.

    Gated on kind == 'drink' so a mocha cake or coffee ice cream doesn't fire.
    Plain tea is deliberately absent from the token list: real caffeine, but
    roughly a third of a coffee's dose, and the bump in ml/mental_predictor.py
    is not dose-scaled, so treating a tea as a full coffee would overstate it.
    ("Matcha latte" does match, via "latte" — fair, it lands near an espresso.)
    Dose-scaling the bump is the natural upgrade once enough coffees are logged
    to fit it; see LEARN_MIN_ENTRIES in ml/mental_predictor.py.
    """
    if not item or item.get("kind") != "drink":
        return False
    key = nutrition.norm_key(item.get("name") or "")
    if any(d in key for d in DECAF_TOKENS):
        return False
    return any(t in key for t in CAFFEINE_DRINK_TOKENS)


COFFEE_DEDUPE_MIN = 20   # one drink logged twice, not two drinks


def coffee_dupe(events: list, new_dt: datetime.datetime) -> bool:
    """True when a coffee is already logged within COFFEE_DEDUPE_MIN of new_dt.

    A latte logged through food and the same cup reported to the assistant are two paths
    to one coffee. caffeine_bump() sums every active coffee, so a duplicate would
    double the lift — the same double-count class of bug fixed in the predictor on
    2026-08-12. Two real coffees inside 20 min is not a habit worth modelling; an
    accidental double-log is.
    """
    for e in events:
        if e.get("type") != "coffee":
            continue
        try:
            existing = datetime.datetime.strptime(e["datetime"], "%Y-%m-%d %H:%M")
        except (KeyError, ValueError):
            continue
        if abs((new_dt - existing).total_seconds()) < COFFEE_DEDUPE_MIN * 60:
            return True
    return False


def append_modifier_event(dt_str: str, ev_type: str,
                          amount: Optional[float], kind: Optional[str],
                          units_per_drink: Optional[float] = None,
                          label: Optional[str] = None) -> None:
    """Append a coffee/alcohol event to events.json and recompute.

    Goes through append_events_atomic like every other writer: this runs as a
    BackgroundTask, so it is exactly the concurrent path the lock exists for. The
    coffee dedupe is evaluated inside that lock rather than against a copy read
    beforehand, which is what makes it hold when two writers arrive together.

    units_per_drink overrides biocharge's kind→units map for an alcohol event
    whose units were computed from a stored ABV rather than a named drink kind.
    label is a human tag (the drink name), ignored by every consumer.
    """
    ev = {"datetime": dt_str, "type": ev_type}
    if amount is not None:
        ev["amount"] = amount
    if kind:
        ev["kind"] = kind
    if units_per_drink is not None:
        ev["units_per_drink"] = round(units_per_drink, 3)
    if label:
        ev["drink"] = label

    skip = None
    if ev_type == "coffee":
        try:
            new_dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        except ValueError:
            new_dt = None
        if new_dt is not None:
            skip = lambda events: coffee_dupe(events, new_dt)   # noqa: E731

    try:
        if not append_events_atomic([ev], skip_if=skip):
            return              # deduped — nothing changed, so nothing to recompute
    except (OSError, ValueError) as e:                           # noqa: BLE001
        # Background task: a failure here must be visible in the log rather than
        # silently swallowing the event, and must not take the request down.
        print(f"append_modifier_event failed ({ev_type} @ {dt_str}): {e}")
        return
    subprocess.run(INGEST_CMD,
                   capture_output=True, text=True, timeout=INGEST_TIMEOUT_S)


def remove_event_atomic(match) -> bool:
    """Remove the first event for which match(event) is true, under the same lock and
    atomic replace as append_events_atomic. True when one was removed. A corrupt
    events.json raises rather than being rewritten."""
    with _events_lock():
        try:
            with open(config.EVENTS_PATH, encoding="utf-8") as f:
                events = json.load(f)
        except FileNotFoundError:
            return False
        for i, e in enumerate(events):
            if match(e):
                del events[i]
                break
        else:
            return False
        tmp = config.EVENTS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(events, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, config.EVENTS_PATH)
        return True
