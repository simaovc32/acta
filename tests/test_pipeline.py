"""End to end: synthetic strap data -> ingest -> API.

The demo generator plants known structure in the data (an unlogged football
game every other Sunday, beer on some weekend nights, a slow training effect)
so these tests can check that the pipeline finds it, not just that it runs.
"""
import datetime as dt
import json
import sqlite3

import pytest

from acta import config
from acta.demo import synth_events, synth_gadgetbridge
from acta.engine import sleep_score as SS
from acta.pipeline import ingest
from conftest import check

# "now", so ingest (which recomputes yesterday and today) always sees the tail of the data
END = dt.datetime.now(config.TZ).replace(second=0, microsecond=0)
DAYS = 35


@pytest.fixture
def demo(data_dir):
    days = synth_gadgetbridge.build(config.GADGETBRIDGE_DB, end=END, days=DAYS, seed=3)
    synth_events.write(config.EVENTS_PATH, days, END)
    return days


def db():
    con = sqlite3.connect(config.ACTA_DB)
    con.row_factory = sqlite3.Row
    return con


def test_hypnogram_blob_round_trips(demo):
    """The generator writes the strap's own blob format; the real parser reads it back."""
    for day in demo[-5:]:
        blob = synth_gadgetbridge._blob(day, day.segs)
        check("stages decode unchanged", SS.parse_hypnogram(blob) == day.segs)
        check("device midnight recovered", SS.session_base_date(blob) == day.base)
        check("UTC offset recovered", SS.session_utc_offset(blob) == day.base.utcoffset().total_seconds())


def test_ingest_scores_every_night_and_fills_the_curve(demo):
    status, nights, minutes = ingest.run_ingest(verbose=False)
    check("ingest ran", status == "ok")
    check("one score per night", nights == DAYS, nights)
    check("1440 biocharge minutes per day", minutes == DAYS * 1440, minutes)
    con = db()
    scores = [r["score"] for r in con.execute("SELECT score FROM sleep_score")]
    check("scores are in range", all(0 <= s <= 100 for s in scores), scores)
    levels = con.execute("SELECT MIN(level), MAX(level) FROM biocharge").fetchone()
    check("biocharge stays in 0..100", 0 <= levels[0] and levels[1] <= 100, tuple(levels))
    labels = {r[0] for r in con.execute("SELECT DISTINCT why_label FROM biocharge")}
    check("sleep, activity and exertion are all labelled", {"sleep", "active", "exertion"} <= labels, labels)


def test_unlogged_workouts_are_proposed_logged_ones_are_not(demo):
    ingest.run_ingest(verbose=False)
    found = {r["date"] for r in db().execute("SELECT date FROM pai_detection")}
    football = {d.date.isoformat() for d in demo for s in d.sessions if s.kind == "football"}
    logged = {d.date.isoformat() for d in demo for s in d.sessions if s.logged and s.kind in ("run", "bike")}
    check("every football game was detected", football and football <= found, (football, found))
    check("no logged run or ride is proposed again", not (logged & found), logged & found)


def test_alcohol_shows_up_in_the_night_after(demo):
    ingest.run_ingest(verbose=False)
    con = db()
    physio = {r["night_of"]: r for r in con.execute("SELECT * FROM night_physio")}
    after = [d.date.isoformat() for d in demo if d.alcohol_units >= 2]
    dry = [d.date.isoformat() for d in demo if not d.alcohol_units]
    mean = lambda xs: sum(xs) / len(xs)
    check("resting HR is higher after drinking",
          mean([physio[n]["resting_hr"] for n in after]) > mean([physio[n]["resting_hr"] for n in dry]))
    check("HRV is lower after drinking",
          mean([physio[n]["hrv_mean"] for n in after]) < mean([physio[n]["hrv_mean"] for n in dry]))


def test_a_new_event_is_picked_up_without_new_strap_data(demo):
    """Logging a coffee must reach the curve on the next ingest, not at the next strap sync."""
    ingest.run_ingest(verbose=False)
    check("unchanged inputs are a no-op", ingest.run_ingest(verbose=False)[0] == "noop")
    events = json.load(open(config.EVENTS_PATH))
    events.append({"datetime": (END - dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"), "type": "alcohol",
                   "kind": "beer", "amount": 3})
    json.dump(events, open(config.EVENTS_PATH, "w"))
    status, _, minutes = ingest.run_ingest(verbose=False)
    check("an event-log change triggers a recompute", status == "ok" and minutes > 0, status)


def test_api_serves_the_demo(demo, client):
    ingest.run_ingest(verbose=False)
    today, past = END.date().isoformat(), (END.date() - dt.timedelta(days=3)).isoformat()
    for path in ("/api/health-summary", "/api/biocharge/latest", f"/api/biocharge?date={past}",
                 f"/api/sleep/latest?date={past}", f"/api/sleep/stages?date={past}", "/api/sleep/recent",
                 f"/api/vitals/history?date={today}", "/api/vitals/snapshot", f"/api/readiness?date={today}",
                 "/api/pai/detections", f"/api/naps?date={past}", "/api/kanban/boards"):
        r = client.get(path)
        check(f"GET {path}", r.status_code == 200, r.text[:200])
    stages = client.get(f"/api/sleep/stages?date={past}").json()
    check("the hypnogram comes back as stage segments", isinstance(stages, list) and len(stages) > 5, str(stages)[:200])
