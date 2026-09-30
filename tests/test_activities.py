"""Tests for tracking.activities and its endpoint /api/activities, plus the guided-session fix."""
import datetime
import json
import os

from acta import config
from acta.api.routers import workout as workout_api
from acta.tracking import activities as A
from conftest import check

TZ = config.TZ


def ms(s):
    return int(datetime.datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp() * 1000)


def test_pure_helpers():
    # -- guided_kind: what a session finished in the Workout tab was --------------------------------------------
    gk = A.guided_kind
    check("strength titles stay strength", gk("Upper Body Pull · Gym") == ("strength", None)
          and gk("Lower Body + Core · List A") == ("strength", None) and gk("Upper Body Push · No equipment") == ("strength", None))
    check("a run day is cardio/run", gk("Steady run") == ("cardio", "run") and gk("Interval run · Run") == ("cardio", "run")
          and gk("Easy run · Run") == ("cardio", "run"))
    check("the Bike variant of a run day is cardio/bike", gk("Steady run · Bike") == ("cardio", "bike")
          and gk("Easy run · Bike") == ("cardio", "bike") and gk("Interval run · bike") == ("cardio", "bike"))
    check("old cardio titles are cardio with no label", gk("Cardio + Mobility · Movement") == ("cardio", None))
    check("empty / odd titles fall back to strength", gk("") == ("strength", None) and gk(None) == ("strength", None)
          and gk("Workout") == ("strength", None))
    check("a word merely containing 'run' is not a run", gk("Trunk stability · List A") == ("strength", None))

    # -- describe ------------------------------------------------------------------------------------------------
    d = A.describe
    check("every PAI label has an icon and a name", all(d(None, s)[0] == s or s == "other" for s in A.LABELS))
    check("label wins over kind", d("other", "coaching") == ("coaching", "Coaching") and d("sport", "football") == ("football", "Football")
          and d("cardio", "bike") == ("bike", "Bike"))
    check("no label: kind decides", d("strength", None) == ("gym", "Gym") and d("cardio", None) == ("run", "Run")
          and d("sport", None) == ("other", "Workout") and d("weird", None) == ("other", "Workout"))
    check("unknown label falls back to the kind", d("cardio", "trampoline") == ("run", "Run"))

    # -- day_activities --------------------------------------------------------------------------------------------
    day0, day1 = ms("2026-09-10 00:00"), ms("2026-09-11 00:00")
    res = lambda s: ms(s)
    evs = [
        {"datetime": "2026-09-10 18:23", "type": "workout", "duration_min": 35, "kind": "cardio", "intensity": "intenso", "source": "pai", "label": "run"},
        {"datetime": "2026-09-09 19:00", "type": "workout", "duration_min": 30, "kind": "strength"},          # other day
        {"datetime": "2026-09-10 08:00", "type": "coffee"},                                                    # not a workout
        {"datetime": "garbage", "type": "workout", "duration_min": 20, "kind": "strength"},                   # malformed
        {"datetime": "2026-09-10 10:00", "type": "workout", "duration_min": 0, "kind": "strength"},            # zero minutes
        {"type": "workout", "duration_min": 20, "kind": "strength"},                                           # no datetime
        "not a dict",
    ]
    out = A.day_activities(evs, day0, day1, res)
    check("only that day's real workouts", len(out) == 1 and out[0]["name"] == "Run" and out[0]["icon"] == "run", out)
    a = out[0]
    check("start/end clocks and minutes", (a["start"], a["end"], a["minutes"]) == ("18:23", "18:58", 35), a)
    check("timestamps are the resolved ones", a["start_ts"] == ms("2026-09-10 18:23") and a["end_ts"] == ms("2026-09-10 18:58"), a)
    check("source and label pass through", a["source"] == "pai" and a["label"] == "run" and a["kind"] == "cardio", a)

    two = [{"datetime": "2026-09-10 19:00", "type": "workout", "duration_min": 25, "kind": "strength"},
           {"datetime": "2026-09-10 12:00", "type": "workout", "duration_min": 40, "kind": "sport", "label": "football"}]
    o2 = A.day_activities(two, day0, day1, res)
    check("earliest first, each with its own icon", [x["icon"] for x in o2] == ["football", "gym"], o2)

    dup = [{"datetime": "2026-09-10 18:20", "type": "workout", "duration_min": 40, "kind": "strength"},
           {"datetime": "2026-09-10 18:23", "type": "workout", "duration_min": 35, "kind": "cardio", "source": "pai", "label": "run"}]
    o3 = A.day_activities(dup, day0, day1, res)
    check("the same effort logged twice is one workout", len(o3) == 1, o3)
    check("the specific label wins the merge", o3[0]["icon"] == "run" and o3[0]["label"] == "run", o3)
    check("merged window is the union, clocks follow it", (o3[0]["start"], o3[0]["end"], o3[0]["minutes"]) == ("18:20", "19:00", 40), o3[0])
    dup2 = [{"datetime": "2026-09-10 18:00", "type": "workout", "duration_min": 20, "kind": "strength"},
            {"datetime": "2026-09-10 18:10", "type": "workout", "duration_min": 45, "kind": "strength"}]
    o4 = A.day_activities(dup2, day0, day1, res)
    check("no labels: the longer one is kept, window widens", len(o4) == 1 and o4[0]["minutes"] == 55 and o4[0]["start"] == "18:00" and o4[0]["end"] == "18:55", o4)
    apart = [{"datetime": "2026-09-10 18:00", "type": "workout", "duration_min": 20, "kind": "strength"},
             {"datetime": "2026-09-10 18:20", "type": "workout", "duration_min": 20, "kind": "strength"}]
    check("back-to-back workouts stay two", len(A.day_activities(apart, day0, day1, res)) == 2)

    late = [{"datetime": "2026-09-10 23:40", "type": "workout", "duration_min": 60, "kind": "strength"}]
    check("a workout crossing midnight shows on both days",
          len(A.day_activities(late, day0, day1, res)) == 1 and len(A.day_activities(late, day1, ms("2026-09-12 00:00"), res)) == 1)
    check("nothing on an empty day and no events", A.day_activities(evs, ms("2026-09-20 00:00"), ms("2026-09-21 00:00"), res) == [] and A.day_activities(None, day0, day1, res) == [])


def test_endpoint_and_guided_sessions(client, monkeypatch):
    import sqlite3
    c = sqlite3.connect(config.ACTA_DB)
    c.execute("CREATE TABLE IF NOT EXISTS sleep_score(night_of TEXT PRIMARY KEY, tz_offset_min INTEGER)")
    c.commit(); c.close()
    logged = []
    monkeypatch.setattr(workout_api, "append_workout_event", lambda *a, **k: logged.append((a, k)))  # no real ingest

    def put(events):
        with open(config.EVENTS_PATH, "w") as f:
            json.dump(events, f)

    put([{"datetime": "2026-09-10 18:23", "type": "workout", "duration_min": 35, "kind": "cardio", "source": "pai", "label": "run"},
         {"datetime": "2026-09-10 19:40", "type": "workout", "duration_min": 25, "kind": "strength"},
         {"datetime": "2026-09-05 11:53", "type": "workout", "duration_min": 10, "kind": "other", "source": "pai", "label": "coaching"},
         {"datetime": "2026-08-29 17:17", "type": "workout", "duration_min": 88, "kind": "sport", "source": "pai", "label": "football"}])
    r = client.get("/api/activities?date=2026-09-10")
    j = r.json()
    check("endpoint answers with that day's workouts", r.status_code == 200 and [a["icon"] for a in j] == ["run", "gym"], j)
    check("a day with none is an empty list", client.get("/api/activities?date=2026-09-11").json() == [])
    check("coaching and football keep their own icons",
          client.get("/api/activities?date=2026-09-05").json()[0]["icon"] == "coaching"
          and client.get("/api/activities?date=2026-08-29").json()[0]["icon"] == "football")
    check("bad date is a 400", client.get("/api/activities?date=nope").status_code == 400)
    check("no date means today (and does not crash)", client.get("/api/activities").status_code == 200)
    os.remove(config.EVENTS_PATH)
    check("missing events.json is an empty list, not an error", client.get("/api/activities?date=2026-09-10").json() == [])
    with open(config.EVENTS_PATH, "w") as f:
        f.write("{ not json")
    check("corrupt events.json is an empty list, not an error", client.get("/api/activities?date=2026-09-10").json() == [])

    # -- the guided-session fix: kind/label reach append_workout_event ----------------------------------------------
    def finish(title):
        logged.clear()
        body = {"id": "s" + title[:3] + str(len(title)), "dateISO": "2026-09-21T09:00:00.000Z", "dayKey": "Tue", "title": title,
                "durationSec": 1800, "setsDone": 3, "setsPlanned": 3, "volume": 0, "exercises": []}
        rr = client.post("/api/workout/session", json=body)
        return rr.status_code, list(logged)

    st, lg = finish("Upper Body Pull · Gym")
    check("a strength session is still logged as strength with no label", st == 200 and len(lg) == 1 and lg[0][0][2] == "strength" and not lg[0][1].get("label"), lg)
    st, lg = finish("Steady run · Bike")
    check("a bike session is logged as cardio/bike", st == 200 and lg[0][0][2] == "cardio" and lg[0][1].get("label") == "bike", lg)
    st, lg = finish("Interval run")
    check("a run session is logged as cardio/run", st == 200 and lg[0][0][2] == "cardio" and lg[0][1].get("label") == "run", lg)
    check("start time and minutes are unchanged (18:00 Lisbon start, 30 min)", lg[0][0][0] == "2026-09-21 10:00" and lg[0][0][1] == 30, lg)
