"""pai — Huami PAI (Personal Activity Intelligence) as an objective training signal.

Two jobs here:

  1. Session detection. Finds training sessions the strap saw but nobody logged
     and PROPOSES them for confirmation -- it never writes a workout event on its
     own, because PAI cannot tell football from cycling from a hard uphill walk.

  2. Monthly-report summary (see summary()).

WHAT PAI ACTUALLY IS
  PAI_TODAY is the points earned today; PAI_TOTAL is a trailing 7-day sum
  (Huami's target is 100). PAI_TOTAL is display-only and never used in a score:
  it drops sharply when a big day ages out of the window. Daily earned values
  and zone minutes are the safe inputs.

EFFECT ON BIOCHARGE -- read this before assuming it is nil
  Confirming a detection appends a workout event, which is NOT inert to
  biocharge: it disables the ambient gate over its window and lets
  inject_manual_activity() fill minutes with no strap HR. Archived days stay
  untouched only because ingest.replay_biocharge recomputes yesterday onward;
  a full replay (reset, restore, manual recompute) will shift those days.
  That is accepted, but any full recompute should expect the diff.

TIMESTAMPS: HUAMI_PAI_SAMPLE.TIMESTAMP is epoch MILLISECONDS, unlike
HUAMI_EXTENDED_ACTIVITY_SAMPLE which is SECONDS (see docs/gadgetbridge.md).

KNOWN LIMITATION -- sessions crossing midnight (open, accepted)
  _session_spans() works on one date's 0..1439 minute grid, so a session
  running 23:00 -> 00:45 is split: the first date yields a span truncated at
  23:59 and the next date's fragment is usually dropped for being under the
  PAI_TODAY threshold. Left unfixed by decision: sessions don't cross midnight
  in practice. Revisit if that changes.

CLI:
    python -m acta.engine.pai                 # detect + list pending, write nothing
    python -m acta.engine.pai --scan          # scan history and store new detections
    python -m acta.engine.pai --summary 2026-08
"""
import datetime
import sqlite3
import sys

from acta import config
from acta.engine import biocharge as BC

TZ = config.TZ
# Days without a session rarely clear 5 PAI, so this separates sessions from
# ordinary movement without tuning.
SESSION_PAI_MIN = 5.0

# Bout location. Thresholds come from biocharge.minute_activity_score(), so
# "exertion" cannot drift between the two modules.
#
# The span is anchored on EXERTION minutes (score 2), not any active minute:
# bridging active minutes chains an evening of light movement into one span.
# A bout is proposed only once its exertion has clearly stopped (not a
# duration cap -- a long match is still detected in full, just after it ends).
QUIET_TAIL_MIN = 20

# Where the effort stops and the cooldown starts. MERGE_GAP_MIN bridges short
# lulls (right for football), but also swallows the cooldown after a run, which
# HR keeps elevated. Cadence separates them, applied to the TAIL ONLY so a
# match's bursts and walks aren't shredded into fragments.
WORK_CADENCE_MIN = 100    # steps/min that counts as sustained effort
MERGE_GAP_MIN = 10    # bridge lulls within a session (halftime, set rest)
MIN_SPAN_MIN = 10     # shorter than this is noise, not a session
MIN_SPAN_ELEVATED = 5  # a span needs this many exertion-grade minutes to count

# Intensity inference from the zone mix, mapped to night_physio.INTENSITY_RPE
# labels. A starting guess the confirmation UI lets the user correct.
INTENSITY_HIGH_MIN = 15   # high-zone minutes -> "intenso"
INTENSITY_HIGH_ANY = 5    # any real high-zone time -> at least "moderado"
INTENSITY_MOD_MIN = 10    # moderate-zone minutes -> "moderado"

# Activity picker: PAI never knows *what* you did, so the user picks it.
# Entries are (slug, display label, canonical biocharge kind). kind stays one of
# biocharge.ACTIVITY_HR_OFFSET's five kinds so strap-less sessions are priced
# right and training_response's per-type aggregation doesn't fragment; the
# specific label is stored separately for display.
ACTIVITY_LABELS = [
    ("football", "Football", "sport"),
    ("padel",    "Padel",    "sport"),
    ("run",      "Run",      "cardio"),
    ("bike",     "Bike",     "cardio"),
    ("swim",     "Swim",     "cardio"),
    ("gym",      "Gym",      "strength"),
    ("walk",     "Walk",     "walk"),
    ("coaching", "Coaching", "other"),
    ("other",    "Other",    "other"),
]
_LABEL_KIND = {slug: kind for slug, _, kind in ACTIVITY_LABELS}


def canonical_kind(slug: str) -> str:
    """Map a picker slug onto a kind biocharge actually understands."""
    return _LABEL_KIND.get(slug, "other")


SCHEMA = """
CREATE TABLE IF NOT EXISTS pai_detection (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  date         TEXT NOT NULL,      -- YYYY-MM-DD (device-local day)
  start_min    INTEGER NOT NULL,   -- minutes from local midnight
  duration_min INTEGER NOT NULL,
  pai_today    REAL,               -- PAI points earned that whole day
  min_low      INTEGER,            -- device zone minutes for the day
  min_mod      INTEGER,
  min_high     INTEGER,
  peak_hr      INTEGER,            -- within the detected span
  avg_hr       INTEGER,
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|dismissed
  kind         TEXT,               -- canonical biocharge kind, set on confirm
  label        TEXT,               -- specific activity name, display only
  intensity    TEXT,               -- leve|moderado|intenso
  created_at   TEXT,
  resolved_at  TEXT,
  pai_final    INTEGER NOT NULL DEFAULT 1,  -- 0 while the day is still running
  work_min     INTEGER,             -- effort only, cooldown trimmed off the tail
  cooldown_min INTEGER,
  work_avg_hr  INTEGER,
  expired_at   TEXT,                -- auto-dismissed after sitting unanswered
  UNIQUE(date, start_min)
)
"""


def register_tz(acta: sqlite3.Connection = None) -> dict:
    """Load the device's per-day UTC offsets into biocharge's registry.

    biocharge.day_tz() / midnight_dt() / day_bounds_s() read a module global
    that ingest.py populates but the API process does not, so BC.load_activity()
    silently used a Lisbon-midnight grid in one process and a device-midnight
    grid in the other -- the same detection could come out with a start_min an
    hour apart depending on who ran it. Worse, the confirmed event is handed to
    biocharge as a bare wall-clock string, which load_events() then resolves
    against the device zone: a travel-day session landed an hour from when it
    actually happened.

    Registering here makes this module's minute grid identical in both
    processes and consistent with ingest, biocharge and the vitals bucketing.
    Source is sleep_score.tz_offset_min -- the same timeline the rest of Acta
    uses (recovered from the hypnogram header, see timezone-data-layer). The
    device also stamps UTC_OFFSET on HUAMI_PAI_SAMPLE rows, which is per-sample
    and marginally more precise, but it exists on only 3 of the 8 tables
    involved, so it cannot be the general mechanism and is deliberately not
    used as a second one.
    """
    close = False
    if acta is None:
        acta, close = open_acta(), True
    try:
        offs = {r["night_of"]: r["tz_offset_min"] for r in acta.execute(
            "SELECT night_of, tz_offset_min FROM sleep_score "
            "WHERE tz_offset_min IS NOT NULL")}
    except sqlite3.Error:
        offs = {}
    finally:
        if close:
            acta.close()
    if offs:
        BC.set_tz_offsets({datetime.date.fromisoformat(d): o
                           for d, o in offs.items()})
    return offs


def device_day(ts_ms: int, offs: dict) -> str:
    """Calendar day the DEVICE was on at ts_ms.

    Same convention as api.device_date(): carry the last known offset forward,
    fall back to the home zone before any offset is known.
    """
    if offs:
        # offsets are keyed by night_of (the wake date); the applicable one is
        # the latest key at or before this instant's home-zone date.
        home_d = datetime.datetime.fromtimestamp(ts_ms / 1000, TZ).date()
        cand = [d for d in offs if datetime.date.fromisoformat(d) <= home_d]
        if cand:
            off = offs[max(cand)]
            tz = datetime.timezone(datetime.timedelta(minutes=off))
            return datetime.datetime.fromtimestamp(ts_ms / 1000, tz).date().isoformat()
    return datetime.datetime.fromtimestamp(ts_ms / 1000, TZ).date().isoformat()


def open_gb() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{config.GADGETBRIDGE_DB}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def open_acta(write: bool = False) -> sqlite3.Connection:
    if write:
        con = sqlite3.connect(config.ACTA_DB, timeout=30)
    else:
        con = sqlite3.connect(f"file:{config.ACTA_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def daily(gb: sqlite3.Connection, offs: dict = None) -> dict:
    """Day-total PAI per date, keyed by Lisbon calendar day.

    Takes the row with the HIGHEST PAI_TODAY of each day, not the last row.
    'Last row' is wrong whenever the device's own midnight falls inside the
    Lisbon day, which happens on timezone-transition days: the device resets
    PAI_TODAY to 0 at its local midnight, so a naive last-row read reports 0
    for a day that had real activity. Bucketing by the device's own UTC_OFFSET
    doesn't help either (the double midnight zeroes that row too), so the max
    is the reliable reading. Within an ordinary day the series is monotonic,
    so max and last agree and nothing changes.

    Today's entry is partial until midnight; callers comparing whole days
    should drop the most recent key.
    """
    rows = gb.execute(
        "SELECT TIMESTAMP, UTC_OFFSET, PAI_LOW, PAI_MODERATE, PAI_HIGH, "
        "TIME_LOW, TIME_MODERATE, TIME_HIGH, PAI_TODAY, PAI_TOTAL "
        "FROM HUAMI_PAI_SAMPLE ORDER BY TIMESTAMP"
    ).fetchall()
    if offs is None:
        offs = register_tz()
    out = {}
    for r in rows:
        d = device_day(r["TIMESTAMP"], offs)
        prev = out.get(d)
        if prev is None or (r["PAI_TODAY"] or 0) >= (prev["PAI_TODAY"] or 0):
            out[d] = dict(r)
    return out


def _session_spans(gb: sqlite3.Connection, date_str: str) -> list:
    """Locate the bout(s) inside a session day.

    PAI reports only per-day totals, so the actual start time and duration have
    to come from the per-minute HR series. Minute scoring is delegated to
    biocharge.minute_activity_score() -- the same function ingest's chart
    labelling uses -- so a minute counts as 'active' here for exactly the
    reason it is coloured that way on the chart.
    """
    date = datetime.date.fromisoformat(date_str)
    activity = BC.load_activity(gb, date)
    if not activity:
        return []
    rhr_bl = BC.resting_hr_baseline(gb, date)

    # 0 = baseline, 1 = active, 2 = exertion
    scores = {}
    for m in range(1440):
        intens, hr, _ = activity.get(m, (0, None, 0))
        scores[m] = BC.minute_activity_score(intens, hr, rhr_bl)

    # Group exertion minutes, bridging gaps <= MERGE_GAP_MIN. The span runs
    # from the first to the last exertion minute of a group, so its duration is
    # bounded by real work rather than by however long movement continued
    # afterwards.
    core = [m for m in range(1440) if scores[m] == 2]
    if not core:
        return []
    groups, cur = [], [core[0]]
    for m in core[1:]:
        if m - cur[-1] <= MERGE_GAP_MIN:
            cur.append(m)
        else:
            groups.append(cur)
            cur = [m]
    groups.append(cur)

    out = []
    for g in groups:
        if len(g) < MIN_SPAN_ELEVATED:
            continue
        s, e = g[0], g[-1] + 1
        # Trim trailing minutes the HR sensor never read (255 sentinel, None here),
        # so a dropout doesn't pad the span's duration and calorie figure.
        while e > s and (e - 1) in activity and activity[e - 1][1] is None:
            e -= 1
        if (e - s) < MIN_SPAN_MIN:
            continue
        hrs = [activity[m][1] for m in range(s, e)
               if m in activity and activity[m][1] is not None]
        if not hrs:
            continue
        work_end = _work_end(activity, s, e)
        work_hrs = [activity[m][1] for m in range(s, work_end)
                    if m in activity and activity[m][1] is not None]
        out.append({
            "start_min": s,
            "duration_min": e - s,
            "work_min": work_end - s,
            "cooldown_min": e - work_end,
            "peak_hr": max(hrs),
            "avg_hr": round(sum(hrs) / len(hrs)),
            "work_avg_hr": round(sum(work_hrs) / len(work_hrs)) if work_hrs else None,
            "elevated_min": len(g),
        })
    return out


def _work_end(activity, start: int, end: int) -> int:
    """Where the effort stopped, as opposed to where the bout stopped.

    Walks back from the end of the span looking for the last minute of
    sustained cadence. Everything after it is cooldown -- stretching, walking it
    off -- during which heart rate stays high while the energy cost has already
    fallen, so pricing it as work overstates the session.

    Tail only, deliberately. Cadence inside a bout dips constantly (football is
    sprints between walking), and splitting on every dip would shred a match
    into fragments. Only the trailing run of quiet minutes is trimmed.

    Walking back to the last sustained-cadence minute already steps over any
    number of quiet minutes, however long the lull, so there is deliberately no
    lull-tolerance counter.
    """
    last = None
    for m in range(end - 1, start - 1, -1):
        if (activity.get(m, (0, None, 0))[2] or 0) >= WORK_CADENCE_MIN:
            last = m
            break
    if last is None:
        return end          # no cadence signal at all (a gym session) -- unchanged
    return min(end, last + 1)


def infer_intensity(min_high: int, min_mod: int) -> str:
    """Map the day's zone mix onto the labels night_physio.INTENSITY_RPE reads.

    High-zone minutes are checked before moderate ones and with a low floor, so
    a hard day isn't labelled 'leve' just because it logged few moderate minutes.
    """
    hi, mod = (min_high or 0), (min_mod or 0)
    if hi >= INTENSITY_HIGH_MIN:
        return "intenso"
    if hi >= INTENSITY_HIGH_ANY or mod >= INTENSITY_MOD_MIN:
        return "moderado"
    return "leve"


def detect(date_str: str, gb=None, day_row=None) -> list:
    """Candidate sessions for one date. Pure read; stores nothing."""
    close = False
    if gb is None:
        gb, close = open_gb(), True
    try:
        if day_row is None:
            day_row = daily(gb).get(date_str)
        if not day_row or (day_row["PAI_TODAY"] or 0) < SESSION_PAI_MIN:
            return []
        spans = _session_spans(gb, date_str)
        if not spans:
            return []
        # Every qualifying bout is returned, not just the biggest: two sessions
        # in one day (morning gym + evening football) are both real and both
        # need proposing.
        #
        # PAI is only ever reported per-day, so points cannot be attributed to a
        # specific bout from the source. They are split across bouts in
        # proportion to exertion minutes -- an estimate, and labelled as one.
        # The zone-minute figures stay as the day totals they are.
        total_elev = sum(s["elevated_min"] for s in spans) or 1
        spans.sort(key=lambda s: s["start_min"])
        out = []
        for sp in spans:
            share = sp["elevated_min"] / total_elev
            out.append({
                "date": date_str,
                "start_min": sp["start_min"],
                "duration_min": sp["duration_min"],
                "work_min": sp.get("work_min"),
                "cooldown_min": sp.get("cooldown_min"),
                "work_avg_hr": sp.get("work_avg_hr"),
                "pai_today": round((day_row["PAI_TODAY"] or 0) * share, 2),
                "pai_day_total": round(day_row["PAI_TODAY"] or 0, 2),
                "split": len(spans) > 1,
                "min_low": day_row["TIME_LOW"],
                "min_mod": day_row["TIME_MODERATE"],
                "min_high": day_row["TIME_HIGH"],
                "peak_hr": sp["peak_hr"],
                "avg_hr": sp["avg_hr"],
                "intensity": infer_intensity(day_row["TIME_HIGH"], day_row["TIME_MODERATE"]),
            })
        return out
    finally:
        if close:
            gb.close()


def logged_windows(acta: sqlite3.Connection) -> dict:
    """{date: [(start_min, end_min), ...]} for every workout already logged.

    Session-level, not day-level. A whole-day check meant one morning gym entry
    hid an unlogged evening football on the same date forever, and confirming
    one detection permanently blocked any second bout that day. Both logging
    paths are read: events.json (cardio/sport, wall-clock + duration) and
    workout_session (guided strength, ISO timestamp + duration_sec).

    Built once per scan rather than re-parsing events.json for every candidate
    day, which is what the previous per-day version did (117 parses per scan).
    """
    import json
    out: dict = {}

    def add(date_str, start_min, dur):
        out.setdefault(date_str, []).append((start_min, start_min + max(1, dur)))

    try:
        with open(config.EVENTS_PATH, encoding="utf-8") as f:
            for e in json.load(f):
                if e.get("type") != "workout":
                    continue
                try:
                    dt = datetime.datetime.strptime(e["datetime"], "%Y-%m-%d %H:%M")
                except (KeyError, ValueError):
                    continue
                add(dt.date().isoformat(), dt.hour * 60 + dt.minute,
                    int(e.get("duration_min") or 60))
    except (OSError, ValueError):
        pass

    try:
        for r in acta.execute(
            "SELECT date_iso, duration_sec FROM workout_session"
        ):
            iso = str(r["date_iso"])
            try:
                # stored as an ISO instant ("...T18:22:33.714Z"); the Z form is
                # UTC, so convert to local before taking a minute-of-day.
                dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                dt = dt.astimezone(TZ) if dt.tzinfo else dt
            except ValueError:
                continue
            add(dt.date().isoformat(), dt.hour * 60 + dt.minute,
                int((r["duration_sec"] or 3600) // 60))
    except sqlite3.Error:
        pass
    return out


# A logged start time is approximate (typed by hand, or a session started a few
# minutes before the strap registered effort), so windows are padded before
# testing overlap. Wide enough to absorb that, narrow enough that a morning
# session cannot swallow an evening one.
OVERLAP_TOL_MIN = 45


def _overlaps(windows: dict, date_str: str, start_min: int, duration_min: int) -> bool:
    """True if this candidate overlaps a workout already logged that day."""
    s, e = start_min, start_min + duration_min
    for ws, we in windows.get(date_str, ()):
        if s < we + OVERLAP_TOL_MIN and ws - OVERLAP_TOL_MIN < e:
            return True
    return False


def scan(write: bool = False, since: str = None) -> list:
    """Find undetected sessions across history. Returns the new candidates.

    Idempotent: UNIQUE(date, start_min) means re-scanning cannot duplicate a
    row, and a detection already resolved (confirmed or dismissed) is never
    re-proposed -- dismissing one has to make it stay dismissed.
    """
    gb = open_gb()
    acta = open_acta(write=True)
    acta.execute(SCHEMA)
    acta.commit()
    try:
        ensure_pai_final(acta)
        offs = register_tz(acta)      # device day-grid, before any BC call
        days = daily(gb, offs)
        today = datetime.datetime.now(TZ).date().isoformat()
        known = {(r["date"], r["start_min"])
                 for r in acta.execute("SELECT date, start_min FROM pai_detection")}
        windows = logged_windows(acta)
        found = []
        _n = datetime.datetime.now(TZ)
        now_min = _n.hour * 60 + _n.minute
        for d in sorted(days):
            if d > today:           # a device day ahead of the clock: ignore
                continue
            if since and d < since:
                continue
            for cand in detect(d, gb=gb, day_row=days[d]):
                # Session-level, so a second bout on an already-logged day is
                # still proposed.
                if _overlaps(windows, d, cand["start_min"], cand["duration_min"]):
                    continue
                if (cand["date"], cand["start_min"]) in known:
                    continue
                # Today only: require the bout to have finished. PAI points are
                # a running daily total split across the day's bouts, so today's
                # figure is provisional and gets corrected by the first scan
                # after midnight.
                provisional = 0
                if d == today:
                    end_min = cand["start_min"] + cand["duration_min"]
                    if now_min is not None and now_min - end_min < QUIET_TAIL_MIN:
                        continue    # still going, or only just stopped
                    provisional = 1
                cand["pai_provisional"] = provisional
                found.append(cand)
                if write:
                    acta.execute(
                        "INSERT OR IGNORE INTO pai_detection "
                        "(date, start_min, duration_min, pai_today, min_low, min_mod, "
                        " min_high, peak_hr, avg_hr, intensity, status, created_at, "
                        " pai_final, work_min, cooldown_min, work_avg_hr) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)",
                        (cand["date"], cand["start_min"], cand["duration_min"],
                         cand["pai_today"], cand["min_low"], cand["min_mod"],
                         cand["min_high"], cand["peak_hr"], cand["avg_hr"],
                         cand["intensity"], datetime.datetime.now(TZ).isoformat(),
                         0 if cand.get("pai_provisional") else 1,
                         cand.get("work_min"), cand.get("cooldown_min"),
                         cand.get("work_avg_hr")),
                    )
        if write:
            _finalise(acta, days, today)
            _backfill_work(acta, gb)
            _expire_stale(acta)
            acta.commit()
        return found
    finally:
        gb.close()
        acta.close()


# A detection unanswered this long won't be answered. It is DISMISSED, never
# deleted: it moves to the Dismissed tab and RESTORE undoes it.
EXPIRE_PENDING_DAYS = 30


def _backfill_work(acta, gb) -> int:
    """Fill work/cooldown on rows written before the split existed.

    Purely additive: it populates columns that were NULL and never touches
    duration_min, so a session already confirmed keeps the record the user
    confirmed. Recomputed from the same detector, so backfilled rows and new
    ones mean the same thing.
    """
    # Every row, not just those missing work_min, so stored and freshly detected
    # rows come from the same detector and mean the same thing. events.json is
    # never rewritten -- it drives biocharge; the detection row is a display record.
    rows = acta.execute(
        "SELECT id, date, start_min, duration_min FROM pai_detection").fetchall()
    if not rows:
        return 0
    by_date = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    fixed = 0
    for d, rs in by_date.items():
        try:
            cands = {c["start_min"]: c for c in detect(d, gb=gb)}
        except Exception:
            continue
        for r in rs:
            c = cands.get(r["start_min"])
            if not c or c.get("work_min") is None:
                continue
            acta.execute(
                "UPDATE pai_detection SET duration_min=?, work_min=?, "
                "cooldown_min=?, work_avg_hr=? WHERE id=?",
                (c["duration_min"], c["work_min"], c["cooldown_min"],
                 c["work_avg_hr"], r["id"]))
            fixed += 1
    return fixed


def _expire_stale(acta, days: int = EXPIRE_PENDING_DAYS) -> int:
    """Auto-dismiss detections left pending past the cutoff."""
    cutoff = (datetime.datetime.now(TZ) - datetime.timedelta(days=days)).isoformat()
    now = datetime.datetime.now(TZ).isoformat()
    cur = acta.execute(
        "UPDATE pai_detection SET status = 'dismissed', resolved_at = ?, "
        "expired_at = ? WHERE status = 'pending' AND created_at < ?",
        (now, now, cutoff))
    return cur.rowcount


def _finalise(acta, days, today: str) -> int:
    """Fill in the real PAI figure for rows written while their day was running.

    A detection made today carries a provisional pai_today, because PAI points
    are a running daily total apportioned across the day's bouts -- a second
    session later in the evening changes the first one's share. Once the day has
    passed, `daily()` reports the settled total and the row is corrected.

    Only the PAI number is provisional. Start, duration, peak and average HR are
    final the moment the bout ends, and confirming a detection writes type,
    kind, intensity and duration to events.json -- never the PAI value. So a
    same-day confirm produces exactly the same event as a next-day one.
    """
    rows = acta.execute(
        "SELECT id, date, start_min, duration_min FROM pai_detection "
        "WHERE pai_final = 0 AND date < ?", (today,)).fetchall()
    fixed = 0
    for r in rows:
        day_row = days.get(r["date"])
        if not day_row:
            continue
        acta.execute(
            "UPDATE pai_detection SET pai_today = ?, min_low = ?, min_mod = ?, "
            "min_high = ?, pai_final = 1 WHERE id = ?",
            (day_row.get("PAI_TODAY"), day_row.get("TIME_LOW"),
             day_row.get("TIME_MODERATE"), day_row.get("TIME_HIGH"), r["id"]))
        fixed += 1
    return fixed


def ensure_pai_final(acta) -> None:
    """Additive migration for databases created before pai_final existed."""
    have = {c[1] for c in acta.execute("PRAGMA table_info(pai_detection)")}
    if "pai_final" not in have:
        acta.execute("ALTER TABLE pai_detection ADD COLUMN "
                     "pai_final INTEGER NOT NULL DEFAULT 1")
    for col, decl in (("work_min", "INTEGER"), ("cooldown_min", "INTEGER"),
                      ("work_avg_hr", "INTEGER"), ("expired_at", "TEXT")):
        if col not in have:
            acta.execute(f"ALTER TABLE pai_detection ADD COLUMN {col} {decl}")


def pending(limit: int = 50) -> list:
    """Unresolved detections, newest first -- what the topbar badge counts.

    Read-only: this is a hot path (every page load, and again after each
    resolve) and acta.db is being written by ingest every few minutes. Opening
    read-write here to run CREATE TABLE IF NOT EXISTS contended for the write
    lock on a 14 MB database for no reason. The table is created by scan() and
    by ensure_schema() at API start; if it somehow does not exist yet, report
    nothing pending rather than taking a write lock to find out.
    """
    try:
        acta = open_acta()
    except sqlite3.Error:
        return []
    try:
        rows = acta.execute(
            "SELECT * FROM pai_detection WHERE status='pending' "
            "ORDER BY date DESC, start_min DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []                     # table not created yet
    finally:
        acta.close()


def ensure_schema() -> None:
    """Create the detection table once, at service start, so the read paths
    never need a write connection just to run DDL."""
    con = open_acta(write=True)
    try:
        con.execute(SCHEMA)
        con.commit()
    finally:
        con.close()


def fmt_clock(start_min: int) -> str:
    return f"{start_min // 60:02d}:{start_min % 60:02d}"


# ── monthly report ────────────────────────────────────────────────────────────

def summary(ym: str) -> list:
    """Lines for monthly_report.py's '## Carga cardio (PAI)' section."""
    register_tz()
    gb = open_gb()
    try:
        days = daily(gb)
    finally:
        gb.close()
    today = datetime.datetime.now(TZ).date().isoformat()
    cur = {d: r for d, r in days.items() if d.startswith(ym) and d < today}
    if not cur:
        return ["No PAI data for this month."]

    prev_ym = (datetime.date.fromisoformat(ym + "-01")
               - datetime.timedelta(days=1)).strftime("%Y-%m")
    prev = {d: r for d, r in days.items() if d.startswith(prev_ym)}

    earned = sum(r["PAI_TODAY"] or 0 for r in cur.values())
    earned_prev = sum(r["PAI_TODAY"] or 0 for r in prev.values())
    at_target = sum(1 for r in cur.values() if (r["PAI_TOTAL"] or 0) >= 100)
    m_low = sum(r["TIME_LOW"] or 0 for r in cur.values())
    m_mod = sum(r["TIME_MODERATE"] or 0 for r in cur.values())
    m_high = sum(r["TIME_HIGH"] or 0 for r in cur.values())

    sessions = [(d, r) for d, r in sorted(cur.items())
                if (r["PAI_TODAY"] or 0) >= SESSION_PAI_MIN]

    acta = open_acta()
    try:
        windows = logged_windows(acta)
    finally:
        acta.close()
    # Day-level here on purpose: this is a monthly headline count, not the
    # detection path, so "did this session day carry any log at all" is the
    # question being answered.
    unlogged = sum(1 for d, _ in sessions if not windows.get(d))

    delta = ""
    if earned_prev:
        diff = earned - earned_prev
        delta = f" ({'+' if diff >= 0 else ''}{diff:.0f} vs {prev_ym})"

    L = [
        f"PAI earned this month: {earned:.0f}{delta}",
        f"Days on target (PAI_TOTAL ≥ 100): {at_target} of {len(cur)}",
        f"Sessions detected: {len(sessions)}"
        + (f", {unlogged} not logged" if unlogged else ", all logged"),
        f"Minutes by zone: {m_low} low · {m_mod} moderate · {m_high} high",
    ]
    if sessions:
        big = max(sessions, key=lambda x: x[1]["PAI_TODAY"] or 0)
        L.append(f"Biggest session: {big[0]} ({big[1]['PAI_TODAY']:.0f} PAI, "
                 f"{big[1]['TIME_HIGH']} min high zone)")
    return L


def main():
    args = sys.argv[1:]
    if "--summary" in args:
        ym = args[args.index("--summary") + 1]
        for line in summary(ym):
            print(" -", line)
        return
    write = "--scan" in args
    found = scan(write=write)
    print(f"{len(found)} new session(s) detected{' and stored' if write else ''}")
    for c in found:
        print(f"  {c['date']}  {fmt_clock(c['start_min'])}  {c['duration_min']:3d}min  "
              f"PAI={c['pai_today']:6.2f}  high={c['min_high']:3d}  "
              f"peak={c['peak_hr']}  -> {c['intensity']}")
    if not write and found:
        print("\n(dry run: use --scan to store)")
    p = pending()
    if p:
        print(f"\n{len(p)} waiting for confirmation in the dashboard")


if __name__ == "__main__":
    main()
