"""
Acta ingest — snapshot → hash-compare → compute new windows → upsert → log.

    acta ingest            (or: python -m acta.pipeline.ingest)

A no-op when neither the Gadgetbridge export nor the event log changed since
the last run (fingerprint, then content hash). Otherwise it scores the new
nights, replays BioCharge forward from the last stored end-of-day level and
upserts only the new rows; then the non-fatal leaf steps run (night physiology,
calories, PAI detection, VO2max, illness fingerprint).
"""

import datetime
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

from acta import config
from acta.engine import biocharge as BC
from acta.engine import calories, fitness, night_physio, pai
from acta.engine import sleep_score as SS
from acta.insights import illness_fingerprint

TZ = config.TZ


# ── Schema ────────────────────────────────────────────────────────────────────
DDL = """
CREATE TABLE IF NOT EXISTS sleep_score (
  night_of          TEXT PRIMARY KEY,
  score             REAL NOT NULL,
  c_efficiency      REAL, c_regularity REAL, c_duration REAL,
  c_stage_balance   REAL, c_physio REAL,
  time_in_bed_min   INTEGER, asleep_min INTEGER,
  deep_min INTEGER, light_min INTEGER, rem_min INTEGER, awake_min INTEGER,
  onset_latency_min INTEGER, waso_min INTEGER, n_awakenings INTEGER,
  bedtime_ts        INTEGER, waketime_ts INTEGER,
  why_pos TEXT, why_neg TEXT,
  source_hash       TEXT,
  tz_offset_min     INTEGER,
  tz_transition     INTEGER,
  ext_sleep_min     INTEGER,   -- sleep recovered from the activity stream
  ext_gap_min       INTEGER,   -- awake between the hypnogram end and that sleep
  computed_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS biocharge (
  minute_ts   INTEGER PRIMARY KEY,
  level       REAL NOT NULL,
  recharge    REAL, drain REAL,
  why_label   TEXT,
  computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_state (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS ingest_run (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at      TEXT,
  finished_at     TEXT,
  source_hash     TEXT,
  nights_written  INTEGER,
  minutes_written INTEGER,
  status          TEXT,
  notes           TEXT
);

CREATE TABLE IF NOT EXISTS user_log (
  ts       INTEGER NOT NULL,
  kind     TEXT NOT NULL,
  value    REAL,
  night_of TEXT,
  note     TEXT,
  PRIMARY KEY (ts, kind)
);
"""


# ── Acta open / state ────────────────────────────────────────────────────────

def open_acta():
    os.makedirs(os.path.dirname(config.ACTA_DB), exist_ok=True)
    # timeout=30 (vs sqlite3's 5s default), same as deps.open_db_rw(), so a
    # concurrent API write doesn't fail with "database is locked".
    con = sqlite3.connect(config.ACTA_DB, timeout=30)
    con.executescript(DDL)
    migrate(con)
    ensure_sleep_ext_cols(con)
    con.commit()
    return con


def migrate(con):
    """Additive-only schema migrations for databases created before a column."""
    have = {r[1] for r in con.execute("PRAGMA table_info(sleep_score)")}
    if "tz_offset_min" not in have:
        con.execute("ALTER TABLE sleep_score ADD COLUMN tz_offset_min INTEGER")
    if "tz_transition" not in have:
        con.execute("ALTER TABLE sleep_score ADD COLUMN tz_transition INTEGER")


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM ingest_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key, value):
    con.execute(
        "INSERT INTO ingest_state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
    )


# ── Safe GB snapshot ──────────────────────────────────────────────────────────

def source_fingerprint():
    """Cheap identity of the source file — modification time and size, no read.

    The gate in front of snapshot_and_hash(): most runs are no-ops, and copying
    and hashing the whole export just to find that out is wasted disk I/O.

    Deliberately NOT a replacement for the content hash. mtime+size says "this
    file has not been touched"; only the hash can say "the bytes are the same",
    which still matters because Syncthing rewrites the file whenever the phone
    reconnects, often with byte-identical content. So this skips the copy when
    nothing was touched, and the hash keeps the final word whenever it was.

    The event log is part of the input too: biocharge and the sleep score read
    coffee, alcohol and workouts from it, and the API triggers an ingest right
    after appending one, so a logged coffee reaches the curve right away.
    """
    parts = []
    for path in (config.GADGETBRIDGE_DB, config.EVENTS_PATH):
        try:
            st = os.stat(path)
            parts.append(f"{st.st_mtime_ns}:{st.st_size}")
        except FileNotFoundError:
            parts.append("-")
    return "|".join(parts)


def snapshot_and_hash():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="gb_snap_")
    os.close(fd)
    shutil.copy2(config.GADGETBRIDGE_DB, tmp)
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    try:                                   # the event log counts as input (see above)
        with open(config.EVENTS_PATH, "rb") as f:
            h.update(f.read())
    except FileNotFoundError:
        pass
    return tmp, h.hexdigest()


def open_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


# ── Upserts ───────────────────────────────────────────────────────────────────

def ensure_sleep_ext_cols(acta):
    """Additive migration for databases created before the extension existed."""
    have = {c[1] for c in acta.execute("PRAGMA table_info(sleep_score)")}
    for col in ("ext_sleep_min", "ext_gap_min"):
        if col not in have:
            acta.execute(f"ALTER TABLE sleep_score ADD COLUMN {col} INTEGER")


def upsert_sleep_score(acta, row):
    cols = ",".join(row.keys())
    qs   = ",".join("?" * len(row))
    upd  = ",".join(f"{k}=excluded.{k}" for k in row if k != "night_of")
    acta.execute(
        f"INSERT INTO sleep_score ({cols}) VALUES ({qs}) "
        f"ON CONFLICT(night_of) DO UPDATE SET {upd}",
        list(row.values())
    )


def upsert_biocharge_batch(acta, rows):
    acta.executemany("""
        INSERT INTO biocharge (minute_ts, level, recharge, drain, why_label, computed_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(minute_ts) DO UPDATE SET
          level=excluded.level, recharge=excluded.recharge,
          drain=excluded.drain, why_label=excluded.why_label,
          computed_at=excluded.computed_at
    """, rows)


def write_run_row(acta, started_at, finished_at, src_hash,
                  nights_written, minutes_written, status, notes=None):
    acta.execute(
        "INSERT INTO ingest_run "
        "(started_at,finished_at,source_hash,nights_written,minutes_written,status,notes) "
        "VALUES (?,?,?,?,?,?,?)",
        (started_at, finished_at, src_hash,
         nights_written, minutes_written, status, notes)
    )
    acta.commit()


# ── Session loader ────────────────────────────────────────────────────────────

# The strap never starts a second hypnogram if you fall back asleep, but
# RAW_KIND 120 in HUAMI_EXTENDED_ACTIVITY_SAMPLE still records it. Only the
# duration is recoverable (no stages), so asleep_staged is tracked separately.
# These EXT_* constants are mirrored in biocharge.py -- keep both in sync by hand.
EXT_KIND_SLEEP = 120
EXT_LOOKAHEAD_MIN = 300   # how far past the recorded wake to look
EXT_MIN_BLOCK_MIN = 15    # shorter than this is lying still, not a sleep period
EXT_MAX_GAP_MIN = 5       # bridge a turn-over inside the block
EXT_MAX_START_LAG_MIN = 150   # a block starting later than this is a nap, not this night

# Folding the post-hypnogram awake gap into WASO changes the sleep score, so it is
# forward-only like every other scoring change here (cf. biocharge EXT_CREDIT_START).
# Older nights keep the score they were computed under; their ext_gap_min is still
# recorded, just not penalised, and their n_awakenings stays hypnogram-only.
WASO_EXT_START = "2026-09-05"


def extend_sleep_from_activity(gb_con, waketime):
    """Sleep the hypnogram missed, from the raw activity stream.

    A restless night can fall back asleep more than once after the hypnogram
    ends -- folds in every qualifying block within the window, not just the
    first, so a second or third return-to-sleep isn't silently dropped.
    Returns (new_waketime, slept_min, awake_gap_min, n_return_bouts) or None
    when the recorded wake really was the end of the night. n_return_bouts is
    how many separate times sleep resumed after the hypnogram ended -- each one
    is an awakening the hypnogram never saw.
    """
    w = int(waketime.timestamp())
    rows = gb_con.execute(
        "SELECT TIMESTAMP, RAW_KIND, STEPS FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
        "WHERE TIMESTAMP > ? AND TIMESTAMP <= ? ORDER BY TIMESTAMP",
        (w, w + EXT_LOOKAHEAD_MIN * 60)).fetchall()
    if not rows:
        return None
    sleepy = [(ts, kind == EXT_KIND_SLEEP and not (steps or 0))
              for ts, kind, steps in rows]

    blocks, cur = [], None
    for ts, ok in sleepy:
        if ok:
            if cur is None:
                cur = [ts, ts]
            elif (ts - cur[1]) <= EXT_MAX_GAP_MIN * 60:
                cur[1] = ts
            else:
                blocks.append(cur)
                cur = [ts, ts]
    if cur:
        blocks.append(cur)

    slept_min, last_end, n_bouts = 0, None, 0
    for start, end in blocks:
        if (start - w) // 60 > EXT_MAX_START_LAG_MIN:
            break          # too far after waking to belong to this night
        length = (end - start) // 60 + 1
        if length < EXT_MIN_BLOCK_MIN:
            continue
        slept_min += length
        last_end = end
        n_bouts += 1

    if last_end is None:
        return None
    gap_min = (last_end - w) // 60 - slept_min
    return (datetime.datetime.fromtimestamp(last_end, TZ), slept_min, gap_min, n_bouts)


def load_all_sessions(gb_con):
    """Return list of night dicts (with segs + base) in chronological order, deduped.

    Gadgetbridge re-syncs the same night many times, and an export taken while
    still asleep yields a truncated session. Keep the longest time-in-bed per
    night so a partial fragment never beats the complete session.
    """
    rows = gb_con.execute(
        "SELECT TIMESTAMP, DATA FROM HUAMI_SLEEP_SESSION_SAMPLE ORDER BY TIMESTAMP"
    ).fetchall()
    by_night = {}
    for _, blob in rows:
        if not blob: continue
        segs = SS.parse_hypnogram(blob)
        if not segs: continue
        base     = SS.session_base_date(blob)
        bedtime  = base + datetime.timedelta(minutes=segs[0][0])
        waketime = base + datetime.timedelta(minutes=segs[-1][1])
        night_of = waketime.date()

        tib  = segs[-1][1] - segs[0][0] + 1
        prev = by_night.get(night_of)
        if prev is not None and prev["time_in_bed"] >= tib:
            continue

        mins = {4:0, 5:0, 7:0, 8:0}
        for s, e, c in segs: mins[c] += e - s + 1
        asleep = mins[4] + mins[5] + mins[8]

        onset_latency, waso, toilet_forgiven, n_awakenings = SS.extract_waso(segs)
        physio = SS.fetch_physio(gb_con, bedtime, waketime)
        tz_off = SS.session_utc_offset(blob)

        # Recover any sleep after the hypnogram's end (see above). Applied here
        # so every downstream consumer sees the true night, and recorded in
        # ext_* so the correction is visible rather than silently folded in.
        ext_min = 0
        ext_gap = 0
        ext = extend_sleep_from_activity(gb_con, waketime)
        if ext:
            waketime, ext_min, ext_gap, ext_bouts = ext
            tib = tib + ext_gap + ext_min
            asleep = asleep + ext_min
            mins[7] += ext_gap
            # The gap between the hypnogram's end and the recovered sleep is
            # wake-after-sleep-onset like any other; the recovered bouts are
            # awakenings the hypnogram missed. Forward-only -- see WASO_EXT_START.
            if str(night_of) >= WASO_EXT_START:
                waso += ext_gap
                n_awakenings += ext_bouts

        by_night[night_of] = dict(
            night_of=night_of, bedtime=bedtime, waketime=waketime,
            time_in_bed=tib, asleep=asleep,
            asleep_staged=asleep - ext_min, ext_sleep_min=ext_min,
            ext_gap_min=ext_gap,
            deep=mins[5], light=mins[4], rem=mins[8], awake=mins[7],
            onset_latency=onset_latency, waso=waso,
            toilet_forgiven=toilet_forgiven, n_awakenings=n_awakenings,
            bedtime_mins=SS.wrap_mins(bedtime), waketime_mins=SS.wrap_mins(waketime),
            physio=physio, segs=segs, base=base,
            tz_offset_min=None if tz_off is None else tz_off // 60,
        )
    nights = [by_night[n] for n in sorted(by_night)]

    # Mark nights where the device changed zone: that day was not 24h long, so it
    # must not sit in the rolling baselines it would distort.
    prev_off = None
    for n in nights:
        off = n["tz_offset_min"]
        n["tz_transition"] = (prev_off is not None and off is not None
                              and off != prev_off)
        if off is not None:
            prev_off = off

    # Day boundaries follow the device, not the home zone. Registered here so
    # every downstream biocharge call (midnight_dt / day_bounds_s) sees them.
    BC.set_tz_offsets({n["night_of"]: n["tz_offset_min"]
                       for n in nights if n["tz_offset_min"] is not None})
    return nights


# ── Sleep score computation ───────────────────────────────────────────────────

# Tail nights are always re-scored, not just those past the watermark: a night
# first scored at wake may be missing late-synced data. Same window as
# replay_biocharge's yesterday rule; upsert_sleep_score makes re-emitting free.
RESCORE_TAIL_NIGHTS = 2


def score_nights(nights, src_hash, now_iso, after_night_of=None):
    """
    Score all nights using rolling history; return (all_nights_with_scores, new_db_rows).
    new_db_rows contains nights where night_of > after_night_of, plus the last
    RESCORE_TAIL_NIGHTS nights regardless (see above).
    """
    rescore = {str(n["night_of"]) for n in nights[-RESCORE_TAIL_NIGHTS:]}
    WINDOW = SS.WINDOW
    REG_WINDOW = SS.REG_WINDOW
    WEIGHTS = SS.WEIGHTS
    db_rows = []
    all_events = BC.load_events()

    for i, night in enumerate(nights):
        # Zone-change days are scored like any other, but kept out of the rolling
        # baselines — a 23h or 25h day would otherwise skew duration and
        # regularity for the next three weeks.
        hist     = [h for h in nights[max(0, i - WINDOW):i]
                    if not h.get("tz_transition")]
        reg_hist = [h for h in nights[max(0, i - REG_WINDOW):i]
                    if not h.get("tz_transition")]

        eff_raw, eff_pct = SS.score_efficiency(
            night["asleep"], night["time_in_bed"], night["toilet_forgiven"])
        eff_pts          = eff_raw * WEIGHTS["efficiency"] / 100
        night["eff_pct"] = eff_pct

        reg_raw, reg_dev = SS.score_regularity(night, reg_hist)
        reg_pts = (reg_raw * WEIGHTS["regularity"] / 100) if reg_raw is not None else None

        dur_raw, dur_bl = SS.score_duration(night["asleep"], hist)
        dur_pts         = dur_raw * WEIGHTS["duration"] / 100

        # Stage proportions over the hypnogram portion only: the recovered
        # extension has no deep/REM breakdown, so counting it in the denominator
        # would read as a collapse in deep-sleep share that never happened.
        stg_raw, dp, rp, dc, rc = SS.score_stages(
            night["deep"], night["rem"],
            night.get("asleep_staged") or night["asleep"], hist)
        stg_pts = stg_raw * WEIGHTS["stages"] / 15   # rescale from internal max-15

        # Forward-only: v2 physio (RHR/HRV-driven, no hr_dip/temp_drop) and the
        # halved alcohol penalty apply only from SLEEP_SCORE_V2_START. Settled
        # nights keep the score they were computed under.
        v2 = str(night["night_of"]) >= SS.SLEEP_SCORE_V2_START
        phy_pts, sub_sc = SS.score_physio(night["physio"], hist, v2=v2)

        pairs = [(eff_pts,25),(reg_pts,20),(dur_pts,15),(stg_pts,25),(phy_pts,15)]
        num = sum(p for p,_ in pairs if p is not None)
        den = sum(w for p,w in pairs if p is not None)
        total = num / den * 100 if den else 50.0
        alc_units = BC.alcohol_presleep_units(
            all_events, night["bedtime"], night["waketime"])
        alc_pen = (SS.score_alcohol_penalty(alc_units,
                       SS.ALCOHOL_SCORE_PENALTY_PER_UNIT_V2,
                       SS.ALCOHOL_SCORE_PENALTY_CAP_V2)
                   if v2 else SS.score_alcohol_penalty(alc_units))
        total = max(0.0, total
                    + SS.score_waso_penalty(night["waso"])
                    + alc_pen)
        night["score"] = total

        # Only emit a DB row if this night is new — or is one of the tail nights
        # that must be re-scored in case fuller data has since arrived.
        if (after_night_of is not None
                and str(night["night_of"]) <= after_night_of
                and str(night["night_of"]) not in rescore):
            continue

        comp_norms = {
            "efficiency": eff_raw,
            "regularity": reg_raw,
            "duration":   dur_raw,
            "stages":     stg_pts / WEIGHTS["stages"] * 100 if stg_pts is not None else None,
            "physio":     phy_pts / WEIGHTS["physio"]  * 100 if phy_pts  is not None else None,
        }
        why = SS.generate_why(comp_norms, night, reg_dev, dur_bl,
                              dp, rp, dc, rc, night["physio"], sub_sc)

        db_rows.append(dict(
            night_of          = str(night["night_of"]),
            score             = total,
            c_efficiency      = eff_pts,
            c_regularity      = reg_pts,
            c_duration        = dur_pts,
            c_stage_balance   = stg_pts,
            c_physio          = phy_pts,
            time_in_bed_min   = night["time_in_bed"],
            asleep_min        = night["asleep"],
            deep_min          = night["deep"],
            light_min         = night["light"],
            rem_min           = night["rem"],
            awake_min         = night["awake"],
            onset_latency_min = night["onset_latency"],
            waso_min          = night["waso"],
            n_awakenings      = night["n_awakenings"],
            bedtime_ts        = int(night["bedtime"].timestamp()  * 1000),
            waketime_ts       = int(night["waketime"].timestamp() * 1000),
            why_pos           = why["best"],
            why_neg           = why["worst"],
            source_hash       = src_hash,
            tz_offset_min     = night["tz_offset_min"],
            ext_sleep_min     = night.get("ext_sleep_min") or 0,
            ext_gap_min       = night.get("ext_gap_min") or 0,
            tz_transition     = 1 if night.get("tz_transition") else 0,
            computed_at       = now_iso,
        ))

    return db_rows


# ── Per-minute label map ──────────────────────────────────────────────────────

def build_minute_labels(r):
    labels   = {}
    is_sleep = r["is_sleep"]
    for t, sl in enumerate(is_sleep):
        if sl:
            labels[t] = "sleep"
    activity  = r["activity"]
    drain_log = r["drain_log"]
    rhr_bl    = r["rhr_bl"]
    runs = []
    i = 0
    while i < 1440:
        val = is_sleep[i]
        j = i
        while j < 1440 and is_sleep[j] == val: j += 1
        runs.append((i, j, val))
        i = j
    for start, end, sl in runs:
        if sl or (end - start) < BC.SEG_WASO_GAP: continue
        # The span says WHERE a bout of activity is (and filters noise via its gates);
        # each minute is tagged from its own HR/intensity, not the span's peak, since
        # merged spans would otherwise over-tag the chart and the trend filter.
        for s, e, _tag, _ in BC._active_subspans(start, end, activity, drain_log, rhr_bl):
            for t in range(s, e):
                score = BC.minute_activity_score(*activity.get(t, (0, None, 0))[:2], rhr_bl)
                if score == 2:
                    labels[t] = "exertion"
                elif score == 1:
                    labels[t] = "active"
                # score 0 = a bridged gap inside the span; leave it unlabelled

    # Naps override everything above: a detected daytime-sleep minute is neither
    # "sleep" (that lane is the hypnogram's) nor "active".
    for t in (r.get("nap_minutes") or ()):
        labels[t] = "nap"

    # Extend-sleep: return-to-sleep after the hypnogram ends. Own label rather
    # than "sleep" -- that lane is the hypnogram's, and this stretch has no
    # measured stage. Can't collide with nap_minutes (windows are exclusive by
    # construction), so ordering vs. the loop above doesn't matter.
    for t in (r.get("ext_minutes") or ()):
        labels[t] = "sleep_ext"
    return labels


# ── RHR-based recharge quality factor ─────────────────────────────────────────
# Logic lives in night_physio.recharge_factor (single source, shared with the
# digest's visibility line). Forward-only via night_physio.RHR_FACTOR_START.

def rhr_recharge_factor(acta, date):
    return night_physio.recharge_factor(str(date))


# ── BioCharge incremental replay ──────────────────────────────────────────────

def replay_biocharge(gb_con, nights, acta, after_night_of, now_iso):
    """
    Replay BioCharge forward from the end-of-day level of after_night_of.
    Returns list of (minute_ts, level, recharge, drain, why_label, computed_at)
    for days strictly after after_night_of.
    """
    today = datetime.date.today()

    # Carry-forward start: anchor_date = min(after_date, today-1), the last day the
    # loop skips. Never read today's own 23:59 (circular, inflates the midnight level).
    if after_night_of:
        after_date   = datetime.date.fromisoformat(after_night_of)
        # Go back 2 days so yesterday is also recomputed (evening activity arrives
        # with the overnight sync, after the previous day's ingest already ran).
        anchor_date  = min(after_date, today - datetime.timedelta(days=2))
        midnight_s   = int(BC.midnight_dt(anchor_date).timestamp())
        eod_ts       = (midnight_s + 1439 * 60) * 1000
        row = acta.execute(
            "SELECT level FROM biocharge WHERE minute_ts=?", (eod_ts,)
        ).fetchone()
        start_level = row[0] if row else BC.COLD_START
    else:
        after_date  = None
        start_level = BC.COLD_START

    all_sessions = [(n["segs"], n["base"]) for n in nights]
    scores_map   = {str(n["night_of"]): n["score"] for n in nights}
    all_events   = BC.load_events()
    dates        = BC.find_compute_dates(all_sessions, today)

    yesterday = today - datetime.timedelta(days=1)
    bc_rows = []
    for date in dates:
        if after_date and date <= after_date and date < yesterday:
            continue  # skip days older than yesterday; recompute yesterday + today

        score      = scores_map.get(str(date), 50.0)
        next_score = scores_map.get(str(date + datetime.timedelta(days=1)))
        r = BC.compute_day(gb_con, date, score, start_level, all_sessions, all_events,
                           next_score=next_score,
                           recharge_factor=rhr_recharge_factor(acta, date),
                           next_recharge_factor=rhr_recharge_factor(
                               acta, date + datetime.timedelta(days=1)))
        if r is None:
            continue

        midnight_s = int(BC.midnight_dt(date).timestamp())
        levels     = r["levels"]
        drain_log  = r["drain_log"]
        is_sleep   = r["is_sleep"]
        ml         = build_minute_labels(r)

        nap_minutes = r.get("nap_minutes") or set()
        ext_minutes = r.get("ext_minutes") or set()
        prev_lv = r["start_level"]
        for t in range(1440):
            minute_ts = (midnight_s + t * 60) * 1000
            lv        = levels[t]
            sl        = is_sleep[t] or (t in nap_minutes) or (t in ext_minutes)
            recharge  = (lv - prev_lv) if sl else None
            drain     = drain_log[t]   if not sl else None
            why_label = ml.get(t)
            bc_rows.append((minute_ts, lv, recharge, drain, why_label, now_iso))
            prev_lv = lv

        start_level = levels[-1]

    # Device-local grids don't always meet: flying east they overlap (newer day wins
    # on the key); flying west a gap opens, so bridge it to keep the curve continuous.
    bridged = []
    for i, row in enumerate(bc_rows):
        bridged.append(row)
        if i + 1 >= len(bc_rows):
            continue
        t0, t1 = row[0], bc_rows[i + 1][0]
        if t1 - t0 <= 60_000:
            continue
        lv0, lv1 = row[1], bc_rows[i + 1][1]
        n = (t1 - t0) // 60_000
        for k in range(1, n):
            bridged.append((t0 + k * 60_000, lv0 + (lv1 - lv0) * k / n,
                            None, None, "tz_transition", now_iso))

    return bridged


# ── Idempotency hash (gate check) ─────────────────────────────────────────────

def content_hash(acta):
    h = hashlib.sha256()
    rows = acta.execute(
        "SELECT night_of,score,c_efficiency,c_regularity,c_duration,"
        "c_stage_balance,c_physio,time_in_bed_min,asleep_min,"
        "deep_min,light_min,rem_min,awake_min,"
        "onset_latency_min,waso_min,n_awakenings,"
        "bedtime_ts,waketime_ts,why_pos,why_neg,source_hash "
        "FROM sleep_score ORDER BY night_of"
    ).fetchall()
    h.update(repr(rows).encode())
    rows = acta.execute(
        "SELECT minute_ts,level,recharge,drain,why_label "
        "FROM biocharge ORDER BY minute_ts"
    ).fetchall()
    h.update(repr(rows).encode())
    rows = acta.execute("SELECT key,value FROM ingest_state ORDER BY key").fetchall()
    h.update(repr(rows).encode())
    return h.hexdigest()


# ── Main ──────────────────────────────────────────────────────────────────────

def run_ingest(verbose=True):
    started_at = datetime.datetime.now(TZ).isoformat()
    now_iso    = started_at

    acta = gb_con = None
    tmp = None
    try:
        acta = open_acta()

        # ── Idempotency layer 0: has the file even been touched? ──────────────
        # Answered from a stat() alone, before anything is copied or hashed.
        src_fp = source_fingerprint()
        if get_state(acta, "last_source_fp") == src_fp:
            finished_at = datetime.datetime.now(TZ).isoformat()
            write_run_row(acta, started_at, finished_at, None,
                          0, 0, "noop", "source file untouched (mtime/size)")
            if verbose:
                print("  status          : noop (source file untouched)")
            return "noop", 0, 0

        # ── Idempotency layer 1: file-level hash check ─────────────────────────
        tmp, src_hash = snapshot_and_hash()
        last_hash = get_state(acta, "last_source_hash")
        if last_hash == src_hash:
            # Touched but byte-identical (a Syncthing rewrite). Record the new
            # fingerprint so the next run skips the copy instead of re-hashing
            # the same bytes every time until real data finally arrives.
            set_state(acta, "last_source_fp", src_fp)
            acta.commit()
            finished_at = datetime.datetime.now(TZ).isoformat()
            write_run_row(acta, started_at, finished_at, src_hash,
                          0, 0, "noop", "source hash unchanged")
            if verbose:
                print("  status          : noop (source hash unchanged)")
                print(f"  source_hash     : {src_hash[:16]}…")
            return "noop", 0, 0

        # ── New data: compute + upsert ─────────────────────────────────────────
        gb_con          = open_ro(tmp)
        last_night_of   = get_state(acta, "last_night_of")  # may be None on first run

        # Load all sessions (needed for full rolling-history scoring)
        nights = load_all_sessions(gb_con)

        # Score — only emit DB rows for nights after last_night_of
        ss_rows = score_nights(nights, src_hash, now_iso, after_night_of=last_night_of)
        for row in ss_rows:
            upsert_sleep_score(acta, row)
        acta.commit()

        # Persist per-night RHR/HRV (needs the sleep rows above; feeds the
        # recharge factor below). Never blocks the pipeline.
        try:
            night_physio.update()
        except Exception as e:
            print(f"  night_physio update failed (non-fatal): {e}")

        # Replay BioCharge forward from last stored end-of-day
        bc_rows = replay_biocharge(gb_con, nights, acta, last_night_of, now_iso)
        upsert_biocharge_batch(acta, bc_rows)
        acta.commit()

        # Daily energy expenditure. Reads the sleep rows and events written
        # above; deliberately never feeds back into biocharge or readiness (it
        # is derived from the same HR stream, so that would double-count).
        # Never blocks the pipeline.
        try:
            calories.update()
        except Exception as e:
            print(f"  calories update failed (non-fatal): {e}")

        # PAI session detection on every ingest. Proposes only; nothing is written to
        # events.json here. Never blocks the pipeline.
        try:
            new = pai.scan(write=True)
            if new:
                print(f"  pai: {len(new)} new session(s) detected")
        except Exception as e:
            print(f"  pai scan failed (non-fatal): {e}")

        # Estimated VO2max (biocharge-tab card). Reads night_physio (RHR), the
        # confirmed pai_detection rows above, and Huami PAI zone minutes. Like
        # calories, it deliberately feeds nothing else — leaf metric, display
        # only. Never blocks the pipeline.
        try:
            fitness.update()
        except Exception as e:
            print(f"  fitness update failed (non-fatal): {e}")

        # Illness fingerprint: snapshot the week before a hard recovery alarm, or
        # before a strong acute nosedive (RHR and HRV crashing together), so the
        # personal pre-illness pattern can be learned. record() merges alarms less
        # than 14 days apart into one episode, so running it every ingest is safe.
        try:
            level, _msg, signals = night_physio.recovery_alarm()
            if level == "hard":
                illness_fingerprint.record()
            elif signals.get("rhr_acute") and signals.get("hrv_acute"):
                illness_fingerprint.record(None, "acute")
        except Exception as e:
            print(f"  illness fingerprint failed (non-fatal): {e}")

        # Update watermarks
        if nights:
            set_state(acta, "last_night_of", str(nights[-1]["night_of"]))
        if bc_rows:
            set_state(acta, "last_minute_ts", str(bc_rows[-1][0]))
        set_state(acta, "last_source_hash", src_hash)
        set_state(acta, "last_source_fp", src_fp)
        acta.commit()

        nights_written  = len(ss_rows)
        minutes_written = len(bc_rows)
        finished_at     = datetime.datetime.now(TZ).isoformat()
        write_run_row(acta, started_at, finished_at, src_hash,
                      nights_written, minutes_written, "ok")

        if verbose:
            print("  status          : ok")
            print(f"  source_hash     : {src_hash[:16]}…")
            print(f"  nights written  : {nights_written}")
            print(f"  minutes written : {minutes_written:,}")
            if nights:
                print(f"  last night_of   : {nights[-1]['night_of']}")
            if bc_rows:
                last_ts = datetime.datetime.fromtimestamp(bc_rows[-1][0] / 1000, TZ)
                print(f"  last BC level   : {bc_rows[-1][1]:.1f}  "
                      f"(at {last_ts.strftime('%Y-%m-%d %H:%M')})")
            print(f"  content hash    : {content_hash(acta)[:24]}…")

        return "ok", nights_written, minutes_written

    except Exception as e:
        # Record the failure in ingest_run, then re-raise so the exit code still says so.
        if acta is not None:
            try:
                write_run_row(acta, started_at, datetime.datetime.now(TZ).isoformat(),
                              src_hash, 0, 0, "error",
                              f"{type(e).__name__}: {e}"[:300])
            except Exception:
                pass        # the DB itself may be what failed — never mask the original
        if verbose:
            print(f"  status          : ERROR — {type(e).__name__}: {e}")
        raise

    finally:
        # Always close both connections; the GB handle pins the snapshot file.
        for con in (gb_con, acta):
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
        if tmp is not None:
            os.remove(tmp)


if __name__ == "__main__":
    import sys
    verbose = "--quiet" not in sys.argv
    if verbose:
        print("=== Acta ingest ===")
    run_ingest(verbose=verbose)
