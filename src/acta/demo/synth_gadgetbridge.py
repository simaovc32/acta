"""Synthetic Gadgetbridge database for the demo.

Acta reads a Huami/Zepp strap through the SQLite database that Gadgetbridge
exports. This module writes a database with the same tables, the same column
units and the same sleep-session blob format, filled with an invented person
("Alex"), so the real pipeline can run end to end on data that belongs to
nobody.

The person is simple but not flat, so the algorithms have something to find:

* resting HR ~54 bpm, drifting down ~2 bpm over the period (training effect);
* sleep ~7h15 around 23:30, later and shorter at weekends;
* strength Mon/Wed/Fri, cardio Tue/Thu/Sat, the odd unlogged football game
  on Sunday (which Acta's PAI detector should propose);
* coffee every morning, beer on some Friday/Saturday nights, which raises the
  next night's resting HR, drops its HRV and fragments its sleep;
* a couple of afternoon naps.

Everything is deterministic for a given (end, days, seed).

Unit traps reproduced on purpose (they are what the real export does):
HUAMI_EXTENDED_ACTIVITY_SAMPLE and BATTERY_LEVEL timestamps are in seconds,
every other table is in milliseconds, and UTC_OFFSET columns are milliseconds.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import sqlite3
import struct
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

DDL = """
CREATE TABLE HUAMI_EXTENDED_ACTIVITY_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "RAW_INTENSITY" INTEGER NOT NULL, "STEPS" INTEGER NOT NULL,
  "RAW_KIND" INTEGER NOT NULL, "HEART_RATE" INTEGER NOT NULL, "UNKNOWN1" INTEGER, "SLEEP" INTEGER,
  "DEEP_SLEEP" INTEGER, "REM_SLEEP" INTEGER,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE HUAMI_SLEEP_SESSION_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "DATA" BLOB,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE HUAMI_HEART_RATE_RESTING_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "UTC_OFFSET" INTEGER NOT NULL, "HEART_RATE" INTEGER NOT NULL,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE HUAMI_HEART_RATE_MAX_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "UTC_OFFSET" INTEGER NOT NULL, "HEART_RATE" INTEGER NOT NULL,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE HUAMI_PAI_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "UTC_OFFSET" INTEGER NOT NULL, "PAI_LOW" REAL NOT NULL,
  "PAI_MODERATE" REAL NOT NULL, "PAI_HIGH" REAL NOT NULL, "TIME_LOW" INTEGER NOT NULL,
  "TIME_MODERATE" INTEGER NOT NULL, "TIME_HIGH" INTEGER NOT NULL, "PAI_TODAY" REAL NOT NULL,
  "PAI_TOTAL" REAL NOT NULL,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE GENERIC_HRV_VALUE_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "VALUE" INTEGER NOT NULL,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE HUAMI_STRESS_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "TYPE_NUM" INTEGER NOT NULL, "STRESS" INTEGER NOT NULL,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE HUAMI_SPO2_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "TYPE_NUM" INTEGER NOT NULL, "SPO2" INTEGER NOT NULL,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "UTC_OFFSET" INTEGER NOT NULL, "RATE" INTEGER NOT NULL,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE GENERIC_TEMPERATURE_SAMPLE ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "USER_ID" INTEGER NOT NULL, "TEMPERATURE" REAL NOT NULL, "TEMPERATURE_TYPE" INTEGER NOT NULL,
  "TEMPERATURE_LOCATION" INTEGER NOT NULL,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID") ON CONFLICT REPLACE) WITHOUT ROWID;
CREATE TABLE BATTERY_LEVEL ("TIMESTAMP" INTEGER NOT NULL, "DEVICE_ID" INTEGER NOT NULL,
  "LEVEL" INTEGER NOT NULL, "BATTERY_INDEX" INTEGER NOT NULL,
  PRIMARY KEY ("TIMESTAMP", "DEVICE_ID", "BATTERY_INDEX") ON CONFLICT REPLACE) WITHOUT ROWID;
"""

# Hypnogram stage codes, as the strap writes them.
LIGHT, DEEP, AWAKE, REM = 4, 5, 7, 8

# RAW_KIND values seen in real exports: 120 = strap-detected sleep,
# 96/80 = sedentary, 64 = walking/running.
KIND_SLEEP, KIND_SIT, KIND_IDLE, KIND_MOVE = 120, 96, 80, 64

HR_MAX = 192
DEVICE_ID = USER_ID = 1


@dataclass
class Session:
    """One block of exercise inside a day."""
    start: dt.datetime
    minutes: int
    kind: str            # strength | run | bike | football | walk
    logged: bool = True  # False = the user never logged it (PAI should catch it)


@dataclass
class Day:
    date: dt.date
    bed: dt.datetime                      # sleep onset of the night ENDING this morning
    segs: list                            # [(start_min, end_min, code)] relative to base
    base: dt.datetime                     # local midnight of the day before `date`
    wake: dt.datetime
    rhr: float
    hrv: float
    alcohol_units: float = 0.0            # drunk the evening BEFORE this night
    sessions: list = field(default_factory=list)
    walks: list = field(default_factory=list)
    nap: tuple | None = None              # (start, minutes)
    coffees: list = field(default_factory=list)
    drinks_tonight: float = 0.0            # units drunk this evening (hit the NEXT night)


# ── the plan: what Alex does each day ─────────────────────────────────────────

def _hypnogram(rng: random.Random, asleep_min: int, fragmented: bool) -> list:
    """Stage sequence of ~90-minute cycles: deep early in the night, REM late."""
    out, t, cycle = [], 0, 0
    first_light = rng.randint(8, 18)
    out.append((LIGHT, first_light))
    t += first_light
    while t < asleep_min:
        late = t / max(asleep_min, 1)
        deep = max(3, int(rng.gauss(24 * (1 - late) + 2, 4)))
        light = max(10, int(rng.gauss(40, 8)))
        rem = max(4, int(rng.gauss(8 + 20 * late, 4)))
        for code, m in ((DEEP, deep), (LIGHT, light), (REM, rem)):
            out.append((code, m))
            t += m
        if fragmented or rng.random() < 0.35:
            aw = rng.randint(2, 9 if fragmented else 5)
            out.append((AWAKE, aw))
            t += aw
        cycle += 1
    out.append((LIGHT, rng.randint(3, 8)))
    out.append((AWAKE, rng.randint(1, 3)))
    return out


def plan(end: dt.datetime, days: int, seed: int, tz: ZoneInfo) -> list[Day]:
    rng = random.Random(seed)
    first = end.date() - dt.timedelta(days=days - 1)
    out = []
    drink_tonight = 0.0
    for i in range(days):
        d = first + dt.timedelta(days=i)
        wd = d.weekday()                                   # 0 = Monday
        prev_wd = (d - dt.timedelta(days=1)).weekday()
        weekend_night = prev_wd in (4, 5)                  # Fri/Sat night
        alcohol = drink_tonight
        progress = i / max(days - 1, 1)

        # Night ending this morning.
        base = dt.datetime.combine(d - dt.timedelta(days=1), dt.time(0), tz)
        onset_min = 23 * 60 + 30 + rng.gauss(0, 25) + (50 if weekend_night else 0) + 20 * min(alcohol, 3)
        asleep = rng.gauss(435, 30) - (35 if weekend_night else 0) - 10 * alcohol
        stages = _hypnogram(rng, int(asleep), fragmented=alcohol >= 2)
        segs, t = [], int(onset_min)
        for code, m in stages:
            segs.append((t, t + m - 1, code))
            t += m
        bed = base + dt.timedelta(minutes=segs[0][0])
        wake = base + dt.timedelta(minutes=segs[-1][1])

        rhr = 55.5 - 2.0 * progress + rng.gauss(0, 1.0) + 2.6 * min(alcohol, 3)
        hrv = 76 + 6 * progress + rng.gauss(0, 7) - 11 * min(alcohol, 3)
        day = Day(d, bed, segs, base, wake, rhr, hrv, alcohol)

        def at(h, m=0, jitter=0, d=d):
            return dt.datetime.combine(d, dt.time(h, m), tz) + dt.timedelta(minutes=rng.randint(-jitter, jitter))

        # Training week: strength Mon/Wed/Fri, cardio Tue/Thu/Sat, rest Sun.
        if wd in (0, 2, 4) and rng.random() < 0.9:
            day.sessions.append(Session(at(18, 15, 30), rng.randint(50, 70), "strength"))
        elif wd in (1, 3) and rng.random() < 0.85:
            day.sessions.append(Session(at(12, 30, 15) if wd == 1 else at(18, 30, 20),
                                        rng.randint(32, 45), "bike" if wd == 3 else "run"))
        elif wd == 5 and rng.random() < 0.8:
            day.sessions.append(Session(at(10, 30, 30), rng.randint(50, 70), "run"))
        elif wd == 6 and (i // 7) % 2 == 0:
            day.sessions.append(Session(at(16, 0, 30), rng.randint(60, 80), "football", logged=False))

        # Walks: commute-style, plus a longer weekend one.
        for h in (8, 13, 19):
            if rng.random() < 0.7:
                day.walks.append((at(h, 20, 25), rng.randint(8, 22)))
        if wd >= 5 and rng.random() < 0.6:
            day.walks.append((at(11, 0, 60), rng.randint(35, 60)))

        if rng.random() < 0.05 or i in (days - 12, days - 30):
            day.nap = (at(14, 40, 20), rng.randint(25, 45))

        day.coffees.append(wake + dt.timedelta(minutes=rng.randint(20, 45)))
        if rng.random() < 0.5:
            day.coffees.append(at(14, 30, 45))

        # Drinks tonight (they hit the NEXT night): some Fridays and Saturdays.
        drink_tonight = 0.0
        if wd in (4, 5) and rng.random() < 0.45:
            drink_tonight = float(rng.choice([2, 2, 3, 4]))
        day.drinks_tonight = drink_tonight
        out.append(day)
    return out


# ── per-minute simulation ─────────────────────────────────────────────────────

def _offset_ms(t: dt.datetime) -> int:
    return int(t.utcoffset().total_seconds() * 1000)


def _session_hr(s: Session, k: int, rhr: float, rng: random.Random) -> tuple:
    """(target HR, steps, intensity, raw_kind) for minute k of a session."""
    warm = min(1.0, (k + 1) / 6)
    if s.kind == "strength":
        in_set = (k % 3) == 0
        hr = (132 if in_set else 108) + rng.gauss(0, 6)
        return rhr + (hr - rhr) * warm, rng.randint(0, 8), rng.randint(45, 75), KIND_SIT
    if s.kind == "run":
        interval = s.minutes < 50 and 10 <= k < s.minutes - 8
        hr = (172 if interval and (k // 3) % 2 == 0 else 152) + rng.gauss(0, 4)
        return rhr + (hr - rhr) * warm, rng.randint(150, 172), rng.randint(105, 140), KIND_MOVE
    if s.kind == "bike":
        return rhr + (146 + rng.gauss(0, 5) - rhr) * warm, 0, rng.randint(50, 80), KIND_SIT
    if s.kind == "football":
        return rhr + (138 + 28 * math.sin(k / 4) + rng.gauss(0, 8) - rhr) * warm, rng.randint(40, 140), rng.randint(70, 130), KIND_MOVE
    raise ValueError(s.kind)


def _blob(day: Day, segs: list) -> bytes:
    """Sleep-session blob: two header epochs, filler, then <HHB records.

    The smaller header epoch is the local midnight of the wake day, which is how
    the parser recovers the device's UTC offset; records are minutes from the
    day before that midnight.
    """
    midnight = int((day.base + dt.timedelta(days=1)).timestamp())
    head = struct.pack("<II", int(day.wake.timestamp()), midnight) + bytes(10)
    body = b"".join(struct.pack("<HHB", s, e, c) for s, e, c in segs)
    return head + body + bytes(16)


def build(path: str, end: dt.datetime | None = None, days: int = 70, seed: int = 7,
          tz: str = "Europe/Lisbon") -> list[Day]:
    """Write the synthetic database to `path` and return the plan it followed."""
    zone = ZoneInfo(tz)
    end = (end or dt.datetime.now(zone)).astimezone(zone).replace(second=0, microsecond=0)
    rng = random.Random(seed * 7919)
    days_ = plan(end, days, seed, zone)
    end_s = int(end.timestamp())

    con = sqlite3.connect(path)
    con.executescript(DDL)
    act, hrv_rows, resp, temp, stress, spo2, pai = [], [], [], [], [], [], []
    rest, hrmax, sessions_rows, battery = [], [], [], []

    hr = days_[0].rhr + 15
    pai_hist: list[float] = []
    batt = 100.0
    for idx, day in enumerate(days_):
        nxt = days_[idx + 1] if idx + 1 < len(days_) else None
        # Minute-by-minute from this morning's wake to the next bedtime (or now);
        # the night itself comes from the hypnogram.
        stage_at = {}
        for s, e, c in day.segs:
            for m in range(s, e + 1):
                stage_at[int((day.base + dt.timedelta(minutes=m)).timestamp())] = c
        day_end = nxt.bed if nxt else end + dt.timedelta(minutes=1)
        t = day.bed
        busy = {}
        for s in day.sessions:
            for k in range(s.minutes):
                busy[int((s.start + dt.timedelta(minutes=k)).timestamp())] = (s, k)
        walking = set()
        for start, mins in day.walks:
            for k in range(mins):
                walking.add(int((start + dt.timedelta(minutes=k)).timestamp()))
        napping = set()
        if day.nap:
            for k in range(day.nap[1]):
                napping.add(int((day.nap[0] + dt.timedelta(minutes=k)).timestamp()))

        local_mid = dt.datetime.combine(day.date, dt.time(0), zone)
        zone_min = {"low": 0, "mod": 0, "high": 0}
        hrr = HR_MAX - day.rhr
        peak = 0
        while t < day_end:
            ts = int(t.timestamp())
            if ts > end_s:
                break
            stage = stage_at.get(ts)
            asleep = stage is not None or ts in napping
            if asleep:
                target = day.rhr - 5 + {DEEP: -3, REM: 3, AWAKE: 9, LIGHT: 0}.get(stage, 1)
                steps, inten, kind = 0, rng.randint(0, 4), KIND_SLEEP
            elif ts in busy:
                s, k = busy[ts]
                target, steps, inten, kind = _session_hr(s, k, day.rhr, rng)
            elif ts in walking:
                target, steps, inten, kind = day.rhr + 38 + rng.gauss(0, 4), rng.randint(95, 118), rng.randint(62, 90), KIND_MOVE
            else:
                h = t.hour + t.minute / 60
                target = day.rhr + 16 + 5 * math.sin((h - 9) / 24 * 2 * math.pi) + rng.gauss(0, 4)
                steps = rng.randint(3, 25) if rng.random() < 0.12 else 0
                inten = rng.randint(8, 42)
                kind = KIND_SIT if rng.random() < 0.6 else KIND_IDLE
            hr += (target - hr) * (0.35 if not asleep else 0.5) + rng.gauss(0, 1.2)
            hr_i = int(round(min(max(hr, 38), HR_MAX)))
            if not asleep and rng.random() < 0.01:
                hr_i = 0                                         # strap gap sentinel
            act.append((ts, DEVICE_ID, USER_ID, inten, steps, kind, hr_i, 5, 0, 0, 0))

            ms = ts * 1000
            off = _offset_ms(t)
            temp.append((ms, DEVICE_ID, USER_ID,
                         round((34.6 if asleep else 32.8) + rng.gauss(0, 0.25 if asleep else 0.6), 2), 2, 9))
            if stage is not None:
                v = int(min(max(day.hrv + rng.gauss(0, 16) + (8 if stage == DEEP else 0), 18), 200))
                hrv_rows.append((ms, DEVICE_ID, USER_ID, v))
                resp.append((ms, DEVICE_ID, USER_ID, off,
                             int(round(14.3 + 0.7 * min(day.alcohol_units, 3) / 3 + rng.gauss(0, 0.8)))))
            if t.minute % 5 == 0:
                base_stress = 13 if asleep else (58 if ts in busy else 24 + 8 * (t.weekday() < 5 and 10 <= t.hour < 18))
                stress.append((ms, DEVICE_ID, USER_ID, 1, int(min(max(base_stress + rng.gauss(0, 7), 2), 90))))
                if asleep or t.minute == 0:
                    spo2.append((ms, DEVICE_ID, USER_ID, 0, int(min(99, round(96.5 + rng.gauss(0, 1.0))))))

            if t >= local_mid and hr_i:
                frac = (hr_i - day.rhr) / hrr
                if frac >= 0.75:
                    zone_min["high"] += 1
                elif frac >= 0.55:
                    zone_min["mod"] += 1
                elif frac >= 0.35:
                    zone_min["low"] += 1
                peak = max(peak, hr_i)
            if t >= local_mid and t.minute % 10 == 0:
                p_low, p_mod, p_high = 0.01 * zone_min["low"], 0.12 * zone_min["mod"], 0.35 * zone_min["high"]
                today = p_low + p_mod + p_high
                pai.append((ms, DEVICE_ID, USER_ID, off, round(p_low, 4), round(p_mod, 4), round(p_high, 4),
                            zone_min["low"], zone_min["mod"], zone_min["high"], round(today, 4),
                            round(sum(pai_hist[-6:]) + today, 4)))
            if t.minute % 30 == 0:
                batt -= 0.45
                if batt < 12:
                    batt = 100.0
                battery.append((ts, DEVICE_ID, int(batt), 0))
            t += dt.timedelta(minutes=1)

        pai_hist.append(pai[-1][10] if pai and pai[-1][0] >= int(local_mid.timestamp() * 1000) else 0.0)
        # The strap stores one resting-HR value per day, during the night.
        rest_t = day.wake - dt.timedelta(minutes=90)
        rest.append((int(rest_t.timestamp() * 1000), DEVICE_ID, USER_ID, _offset_ms(rest_t), int(round(day.rhr))))
        if peak >= 170 and day.sessions:
            s = day.sessions[-1]
            done = s.start + dt.timedelta(minutes=s.minutes + 5)
            if int(done.timestamp()) <= end_s:
                hrmax.append((int(done.timestamp() * 1000), DEVICE_ID, USER_ID, _offset_ms(done), peak))
        # Gadgetbridge re-syncs a night several times, and an earlier sync can
        # miss the last few minutes of it. Ingest must keep the longest version.
        if int(day.wake.timestamp()) <= end_s:
            sync = day.wake + dt.timedelta(minutes=rng.randint(10, 90))
            sessions_rows.append((int(sync.timestamp() * 1000), DEVICE_ID, USER_ID, _blob(day, day.segs)))
            if rng.random() < 0.3:
                early = day.wake - dt.timedelta(minutes=2)
                sessions_rows.append((int(early.timestamp() * 1000), DEVICE_ID, USER_ID, _blob(day, day.segs[:-2])))

    ins = lambda table, n, rows: con.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * n)})", rows)
    ins("HUAMI_EXTENDED_ACTIVITY_SAMPLE", 11, act)
    ins("GENERIC_HRV_VALUE_SAMPLE", 4, hrv_rows)
    ins("HUAMI_SLEEP_RESPIRATORY_RATE_SAMPLE", 5, resp)
    ins("GENERIC_TEMPERATURE_SAMPLE", 6, temp)
    ins("HUAMI_STRESS_SAMPLE", 5, stress)
    ins("HUAMI_SPO2_SAMPLE", 5, spo2)
    ins("HUAMI_PAI_SAMPLE", 12, pai)
    ins("HUAMI_HEART_RATE_RESTING_SAMPLE", 5, rest)
    ins("HUAMI_HEART_RATE_MAX_SAMPLE", 5, hrmax)
    ins("HUAMI_SLEEP_SESSION_SAMPLE", 4, sessions_rows)
    ins("BATTERY_LEVEL", 4, battery)
    con.commit()
    con.close()
    return days_
