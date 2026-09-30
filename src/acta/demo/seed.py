"""Build a complete demo: an invented person, ten weeks of data, every tab filled.

    acta demo                 # writes ./data (refuses if it already has a database)
    acta demo --force         # replace an existing demo
    acta demo --data-dir /tmp/acta-demo --days 90

What it does, in order:
  1. writes a synthetic Gadgetbridge export and event log (demo/synth_*.py);
  2. creates the API's tables and the body profile;
  3. runs the real ingest pipeline over it (sleep scores, biocharge, PAI, VO2max);
  4. fills the manual side the way a user would, mostly through the real API:
     weight, mood check-ins, food, finished workouts, soreness, finance, to-dos.

The finance screens are PIN-locked; the demo PIN is 4321.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
from pathlib import Path

DEMO_PIN = "4321"


def _args(argv=None):
    ap = argparse.ArgumentParser(prog="acta demo", description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", default=os.environ.get("ACTA_DATA_DIR", "data"))
    ap.add_argument("--days", type=int, default=70)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--force", action="store_true", help="replace an existing database in --data-dir")
    return ap.parse_args(argv)


def main(argv=None):
    a = _args(argv)
    data = Path(a.data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    if (data / "acta.db").exists() and not a.force:
        sys.exit(f"{data}/acta.db already exists. Use --force to replace it with a fresh demo.")
    for name in ("acta.db", "acta.db-wal", "acta.db-shm", "Gadgetbridge.db", "events.json"):
        (data / name).unlink(missing_ok=True)
    os.environ["ACTA_DATA_DIR"] = str(data)
    for var in ("ACTA_DB", "ACTA_GADGETBRIDGE_DB", "ACTA_EVENTS"):
        os.environ.pop(var, None)

    # Imported only now: acta.config reads the environment at import time.
    from acta import config
    from acta.demo import synth_events, synth_gadgetbridge

    now = dt.datetime.now(config.TZ).replace(second=0, microsecond=0)
    days = synth_gadgetbridge.build(config.GADGETBRIDGE_DB, end=now, days=a.days, seed=a.seed,
                                    tz=str(config.TZ))
    events = synth_events.write(config.EVENTS_PATH, days, now)
    print(f"strap data: {a.days} days, {len(events)} logged events")

    from acta.api import schema
    from acta.api.deps import open_db_rw
    from acta.tracking import body
    schema.init_db()
    con = open_db_rw()
    body.set_config(con, 178, "2000-01-01", "male")
    rng = random.Random(a.seed)
    for i, d in enumerate(days[::5]):
        kg = 77.4 - 1.6 * i / max(len(days[::5]) - 1, 1) + rng.gauss(0, 0.25)
        body.log_weight(con, round(kg, 1), ts=d.wake + dt.timedelta(minutes=10))
    body.log_weight(con, waist_cm=81.5, ts=days[-20].wake + dt.timedelta(minutes=12))
    con.commit()
    con.close()

    from acta.pipeline import ingest
    ingest.run_ingest(verbose=False)
    print("ingest: sleep, biocharge, calories, PAI and VO2max computed")

    import warnings
    warnings.filterwarnings("ignore", message="Using .httpx. with .starlette.testclient.")
    from fastapi.testclient import TestClient

    from acta.api.app import app
    with TestClient(app) as api:
        _mood(days, rng)
        _food(days, now, rng)
        _workouts(days, now, rng)
        _soreness(api, days)
        _finance(api, now, rng)
        _productivity(api)
    serve = "acta serve" if data == Path("data").resolve() else f"ACTA_DATA_DIR={data} acta serve"
    print(f"\ndemo ready in {data}\n  run:  {serve}\n  then open http://{config.HOST}:{config.PORT}"
          f"  (finance PIN: {DEMO_PIN})")


# ── the manual side ───────────────────────────────────────────────────────────

def _mood(days, rng):
    """Mood check-ins (1-5), a little better after a good night."""
    from acta.api.deps import open_db_rw
    con = open_db_rw()
    scores = dict(con.execute("SELECT night_of, score FROM sleep_score"))
    for d in days:
        for hour in rng.sample([10, 13, 16, 19, 21], rng.randint(1, 3)):
            t = dt.datetime.combine(d.date, dt.time(hour, rng.randint(0, 59)), d.bed.tzinfo)
            if t.timestamp() * 1000 > _now_ms():
                continue
            s = scores.get(d.date.isoformat(), 75)
            v = max(1, min(5, round(3.2 + (s - 78) / 14 + rng.gauss(0, 0.7))))
            con.execute("INSERT OR IGNORE INTO user_log(ts, kind, value, night_of) VALUES(?,?,?,?)",
                        (int(t.timestamp() * 1000), "mental_state", float(v), d.date.isoformat()))
    con.commit()
    con.close()


FOODS = [  # name, kind, kcal/100 g, protein, carbs, fat, portion g, portion label
    ("Oats", "food", 379, 13.2, 67.7, 6.5, 60, "1 bowl (60 g)"),
    ("Greek yogurt", "food", 97, 9.0, 3.9, 5.0, 170, "1 pot (170 g)"),
    ("Banana", "food", 89, 1.1, 22.8, 0.3, 120, "1 medium"),
    ("Eggs", "food", 143, 12.6, 0.7, 9.5, 100, "2 eggs"),
    ("Wholegrain bread", "food", 247, 13.0, 41.0, 3.4, 40, "1 slice"),
    ("Chicken breast", "food", 165, 31.0, 0.0, 3.6, 150, "1 fillet"),
    ("Rice (cooked)", "food", 130, 2.7, 28.0, 0.3, 180, "1 plate"),
    ("Salmon", "food", 208, 20.0, 0.0, 13.0, 140, "1 fillet"),
    ("Potatoes (boiled)", "food", 87, 1.9, 20.1, 0.1, 200, "1 plate"),
    ("Mixed salad", "food", 20, 1.2, 3.6, 0.2, 100, "1 bowl"),
    ("Pasta (cooked)", "food", 158, 5.8, 30.9, 0.9, 200, "1 plate"),
    ("Tomato sauce", "food", 29, 1.3, 5.0, 0.2, 100, "1 ladle"),
    ("Apple", "food", 52, 0.3, 13.8, 0.2, 150, "1 medium"),
    ("Almonds", "food", 579, 21.2, 21.6, 49.9, 30, "1 handful"),
    ("Whey shake", "drink", 380, 78.0, 8.0, 5.0, 30, "1 scoop"),
]
MEALS = {
    "breakfast": [[("Oats", 60), ("Greek yogurt", 170), ("Banana", 120)], [("Eggs", 100), ("Wholegrain bread", 80)]],
    "lunch": [[("Chicken breast", 150), ("Rice (cooked)", 200), ("Mixed salad", 100)],
              [("Pasta (cooked)", 220), ("Tomato sauce", 120), ("Chicken breast", 120)]],
    "afternoon_snack": [[("Apple", 150), ("Almonds", 30)], [("Whey shake", 30), ("Banana", 120)]],
    "dinner": [[("Salmon", 140), ("Potatoes (boiled)", 220), ("Mixed salad", 100)],
               [("Eggs", 100), ("Wholegrain bread", 80), ("Mixed salad", 100)]],
}
MEAL_TIME = {"breakfast": (8, 10), "lunch": (13, 0), "afternoon_snack": (16, 45), "dinner": (20, 15)}


def _food(days, now, rng):
    """Two weeks of meals, logged as dishes the way the Food tab logs them."""
    from acta.api.deps import open_db_rw
    from acta.tracking import nutrition
    con = open_db_rw()
    ids = {}
    for name, kind, kcal, p, c, f, pg, label in FOODS:
        ids[name] = nutrition.upsert_item(con, name, kcal, kind=kind, protein_g=p, carbs_g=c, fat_g=f,
                                          portion_g=pg, portion_label=label)["id"]
    for d in days[-14:]:
        for slot, options in MEALS.items():
            h, m = MEAL_TIME[slot]
            t = dt.datetime.combine(d.date, dt.time(h, m), now.tzinfo) + dt.timedelta(minutes=rng.randint(-20, 20))
            if t > now:
                continue
            parts = rng.choice(options)
            nutrition.log_meal(con, name=slot.replace("_", " ").capitalize(), slot=slot, ts=t,
                               components=[{"food_id": ids[n], "grams": g} for n, g in parts])
    con.commit()
    con.close()


EXERCISES = {  # what a strength day looked like in the Workout tab
    0: ("Upper Body Push · List A", [("Dumbbell Floor Press", "Chest", 22), ("Dumbbell Shoulder Press", "Shoulders", 16),
                                     ("Lateral Raise", "Shoulders", 8), ("Tricep Kickback", "Arms", 10)]),
    2: ("Lower Body + Core · List A", [("Goblet Squat", "Legs", 26), ("Romanian Deadlift", "Legs", 24),
                                       ("Walking Lunge", "Legs", 14), ("Plank", "Core", 0)]),
    4: ("Upper Body Pull · List A", [("One-arm Dumbbell Row", "Back", 26), ("Negative Pull-up (5–8s lower)", "Back", 0),
                                     ("Hammer Curl", "Arms", 12), ("Dead Hang", "Back", 0)]),
}


def _workouts(days, now, rng):
    """Finished strength sessions, as the Workout tab would have saved them."""
    from acta.api.deps import open_db_rw
    con = open_db_rw()
    for d in days:
        for s in d.sessions:
            if s.kind != "strength" or s.start + dt.timedelta(minutes=s.minutes) > now:
                continue
            title, lifts = EXERCISES[d.date.weekday()]
            weeks = (d.date - days[0].date).days / 7
            ex = []
            for name, muscle, kg in lifts:
                top = round(kg * (1 + 0.012 * weeks)) if kg else 0
                ex.append({"name": name, "muscle": muscle, "mode": "time" if not kg else "reps",
                           "setsDone": 4 if rng.random() > 0.15 else 3, "sets": 4, "top": top, "dur": 0 if kg else 35})
            done = sum(e["setsDone"] for e in ex)
            volume = sum(e["setsDone"] * e["top"] * 10 for e in ex)
            con.execute(
                "INSERT INTO workout_session (id, date_iso, day_key, title, duration_sec, sets_done, sets_planned,"
                " volume, avg_rpe, exercises_json, created_at, notes, pain_flags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"demo-{d.date.isoformat()}", d.date.isoformat(), s.start.strftime("%a"), title, s.minutes * 60,
                 done, 16, volume, round(rng.uniform(6.5, 8.5), 1), json.dumps(ex),
                 (s.start + dt.timedelta(minutes=s.minutes)).isoformat(), None, None))
    con.commit()
    con.close()


def _soreness(api, days):
    last = next((d for d in reversed(days[:-1]) if d.date.weekday() == 2 and d.sessions), None)
    if last is not None:
        api.post("/api/log/soreness", json={"areas": {"quads": {"severity": 2, "kind": "ache"},
                                                      "hamstrings": {"severity": 1, "kind": "tight"}}})


def _finance(api, now, rng):
    def call(method, path, body=None, tok=None):
        r = api.request(method, path, json=body, headers={"X-Finance-Token": tok} if tok else {})
        r.raise_for_status()
        return r.json()

    call("POST", "/api/finance/setup-pin", {"pin": DEMO_PIN})
    tok = call("POST", "/api/finance/unlock", {"pin": DEMO_PIN})["token"]
    day = lambda n: (now.date() - dt.timedelta(days=n)).isoformat()   # noqa: E731
    ids = {}
    for name, kind in (("Everyday account", "bank"), ("Savings", "bank"), ("Cash", "cash"),
                       ("Broker", "stocks"), ("Crypto wallet", "crypto")):
        r = call("POST", "/api/finance/accounts", {"name": name, "kind": kind}, tok)
        ids[name] = r.get("id") or r.get("account", {}).get("id")
    balances = {"Everyday account": [1850, 2100, 2450, 2840], "Savings": [5000, 5400, 5800, 6120],
                "Cash": [40, 60, 95, 85], "Broker": [3600, 3900, 4050, 4300], "Crypto wallet": [280, 310, 340, 380]}
    for i, n in enumerate((90, 60, 30, 6)):
        call("POST", "/api/finance/balance",
             {"as_of": day(n), "balances": {str(ids[k]): v[i] for k, v in balances.items()}}, tok)
    for name, amt, dom, direction in (("Salary", 1650, 25, "in"), ("Rent share", 420, 1, "out"),
                                      ("Gym", 29.9, 1, "out"), ("Phone plan", 14.5, 12, "out"),
                                      ("Music streaming", 11.99, 5, "out")):
        call("POST", "/api/finance/subs", {"name": name, "amount": amt, "direction": direction,
                                           "day_of_month": dom, "account_id": ids["Everyday account"]}, tok)
    purchases = [(-3.2, "Coffee shop", "coffee"), (-24.8, "Supermarket", "groc"), (-8.5, "Ride share", "move"),
                 (-13.9, "Pizzeria", "eat"), (-39.99, "Sports store", "shop"), (-2.4, "Bakery", "eat"),
                 (-31.6, "Supermarket", "groc"), (-9.0, "Cinema", "fun")]
    for i, (amt, desc, cat) in enumerate(purchases):
        if i > now.day + 3:
            break
        call("POST", "/api/finance/transaction", {"account_id": ids["Everyday account"], "amount_eur": amt,
                                                  "description": desc, "category": cat, "source": "manual",
                                                  "occurred_on": day(min(i, now.day - 1))}, tok)
    holdings = [("VWCE", "FTSE All-World ETF", 21, 118.4), ("AAPL", "Apple", 4, 201.5), ("ASML", "ASML Holding", 1.5, 702),
                ("MSFT", "Microsoft", 2, 415), ("SAP", "SAP SE", 2, 225)]
    crypto = [("BTC", "Bitcoin", 0.004, 62000), ("ETH", "Ethereum", 0.05, 2400)]
    from acta.api.deps import open_db_rw
    con = open_db_rw()
    for s, _n, _q, p in holdings + crypto:
        con.execute("INSERT OR REPLACE INTO finance_holding_price(symbol, as_of, price, created_at) VALUES(?,?,?,?)",
                    (s, now.date().isoformat(), p, now.isoformat()))
    con.commit()
    con.close()
    for acct, rows in ((ids["Broker"], holdings), (ids["Crypto wallet"], crypto)):
        for s, n, q, _p in rows:
            call("POST", "/api/finance/holdings", {"account_id": acct, "symbol": s, "quantity": q, "name": n}, tok)
    call("POST", f"/api/finance/holdings/{ids['Broker']}/cash", {"amount_eur": 120.0}, tok)
    for s, n in (("PLTR", "Palantir"), ("ADBE", "Adobe")):
        call("POST", "/api/portfolio/watchlist", {"symbol": s, "name": n})
    for name, price, want, need in (("Trail running shoes", 129.0, 8, 6), ("Espresso machine", 349.0, 7, 3),
                                    ("Noise-cancelling headphones", 279.0, 6, 4)):
        call("POST", "/api/finance/wishlist", {"name": name, "price": price, "want": want, "need": need}, tok)


def _productivity(api):
    boards = api.get("/api/kanban/boards").json()
    native = [b for b in boards.get("boards", boards) if isinstance(b, dict) and b.get("kind") == "native"]
    if native:
        board = api.get(f"/api/kanban/boards/{native[0]['slug']}").json()
        lanes = board.get("lanes") or []
        cards = [["Write the README", "Record a demo GIF"], ["Refactor the ingest tests"], ["Ship v1"]]
        for lane, texts in zip(lanes, cards):
            for text in texts:
                api.post(f"/api/kanban/boards/{native[0]['slug']}/cards", json={"lane_id": lane["id"], "text": text})
    for dow in range(5):
        api.post("/api/schedule/blocks", json={"dow": dow, "start_min": 9 * 60, "end_min": 12 * 60,
                                               "title": "Deep work", "category": "deep"})
        api.post("/api/schedule/blocks", json={"dow": dow, "start_min": 14 * 60, "end_min": 16 * 60,
                                               "title": "Classes", "category": "class"})
    for dow in (0, 2, 4):
        api.post("/api/schedule/blocks", json={"dow": dow, "start_min": 18 * 60, "end_min": 19 * 60 + 15,
                                               "title": "Gym", "category": "training"})


def _now_ms():
    return int(dt.datetime.now().timestamp() * 1000)


if __name__ == "__main__":
    main()
