"""The Workout tab: weekly plan, finished sessions, run review, exercise media."""

import datetime
import json
import os
import re
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel

from acta import config
from acta.api.days import check_date, day_midnight_ms
from acta.api.deps import open_db, open_db_rw, open_gb
from acta.api.event_log import append_workout_event
from acta.api.routers.body import SORENESS_MUSCLE
from acta.config import TZ
from acta.engine import calories, night_physio
from acta.tracking import activities, body

router = APIRouter()


DAY_KEYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


VIDEOS_DIR = config.MEDIA_DIR


MAX_VIDEO_BYTES = 50_000_000  # 50 MB — short looping demo clips


# Demo media for an exercise: video, animated GIF or a still photo. One file per
# slug; uploading a new one replaces whatever type was there before.
MEDIA_EXTS = {
    "video/mp4":       "mp4",
    "video/webm":      "webm",
    "video/quicktime": "mov",
    "image/gif":       "gif",
    "image/jpeg":      "jpg",
    "image/png":       "png",
    "image/webp":      "webp",
}


MEDIA_EXT_SET = set(MEDIA_EXTS.values()) | {"jpeg"}


class WorkoutPlanDoc(BaseModel):
    plan: dict
    week_key: Optional[str] = None


class WorkoutSessionIn(BaseModel):
    id: str
    dateISO: str
    dayKey: str
    title: str
    durationSec: int
    setsDone: int
    setsPlanned: int
    volume: float
    avgRpe: Optional[float] = None
    exercises: list = []
    notes: Optional[str] = None
    pain: list = []          # body areas flagged, e.g. ["shoulder", "lower-back"]


@router.get("/api/workout/plan")
def workout_get_plan():
    con = open_db()
    try:
        row = con.execute(
            "SELECT plan_json, week_key, updated_at FROM workout_plan WHERE id=1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        return {"plan": None, "week_key": None, "updated_at": None}
    return {
        "plan":       json.loads(row["plan_json"]),
        "week_key":   row["week_key"],
        "updated_at": row["updated_at"],
    }


@router.put("/api/workout/plan")
def workout_put_plan(doc: WorkoutPlanDoc):
    plan_json = json.dumps(doc.plan, ensure_ascii=False)
    if len(plan_json) > 512_000:
        raise HTTPException(400, "plan too large")
    missing = [d for d in DAY_KEYS if d not in doc.plan]
    if missing:
        raise HTTPException(400, f"plan missing day keys: {missing}")
    updated_at = datetime.datetime.now(TZ).isoformat()
    con = open_db_rw()
    try:
        con.execute(
            "INSERT INTO workout_plan(id, plan_json, week_key, updated_at) "
            "VALUES(1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "plan_json=excluded.plan_json, week_key=excluded.week_key, "
            "updated_at=excluded.updated_at",
            (plan_json, doc.week_key, updated_at),
        )
        con.commit()
    finally:
        con.close()
    return {"status": "ok", "updated_at": updated_at}


@router.get("/api/workout/trim")
def workout_get_trim():
    """Today's readiness-driven training-load suggestion, full trimmed plan
    included (source for the Workout tab's "Aplicar" button). Read-only -
    applying it is a client-side PUT of the *current* plan doc with a
    date-stamped `_trim_applied` key added, mirroring how `_override` (the
    one-off day-swap) already rides along without touching the day
    templates. Empty dict on a green/amber day or no plan for today."""
    trim = night_physio.training_trim()
    return trim or {}


@router.get("/api/workout/sessions")
def workout_get_sessions(limit: int = Query(300, ge=1, le=1000)):
    con = open_db()
    try:
        rows = con.execute(
            "SELECT * FROM workout_session ORDER BY date_iso DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()
    return [
        {
            "id":          r["id"],
            "dateISO":     r["date_iso"],
            "dayKey":      r["day_key"],
            "title":       r["title"],
            "durationSec": r["duration_sec"],
            "setsDone":    r["sets_done"],
            "setsPlanned": r["sets_planned"],
            "volume":      r["volume"],
            "avgRpe":      r["avg_rpe"],
            "exercises":   json.loads(r["exercises_json"]),
            "notes":       r["notes"] if "notes" in r.keys() else None,
            "pain":        json.loads(r["pain_flags"]) if ("pain_flags" in r.keys() and r["pain_flags"]) else [],
        }
        for r in rows
    ]


@router.post("/api/workout/session")
def workout_post_session(sess: WorkoutSessionIn, background: BackgroundTasks):
    if sess.setsDone < 1:
        raise HTTPException(400, "session has no completed sets")
    con = open_db_rw()
    try:
        notes = (sess.notes or "").strip() or None
        pain_json = json.dumps(sess.pain, ensure_ascii=False) if sess.pain else None
        con.execute(
            "INSERT OR REPLACE INTO workout_session("
            "id, date_iso, day_key, title, duration_sec, sets_done, sets_planned, "
            "volume, avg_rpe, exercises_json, created_at, notes, pain_flags) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sess.id, sess.dateISO, sess.dayKey, sess.title, sess.durationSec,
                sess.setsDone, sess.setsPlanned, sess.volume, sess.avgRpe,
                json.dumps(sess.exercises, ensure_ascii=False),
                datetime.datetime.now(TZ).isoformat(), notes, pain_json,
            ),
        )
        # Mirror the finish-screen pain chips into the body map so there is one
        # source of truth for "what hurts today", with one reset rule (daily).
        # workout_session.pain_flags stays as the record of that session.
        # These chips are pain flags: intensity 2 (moderate), kind 'sharp'. The body map can be
        # edited afterwards to correct either.
        if sess.pain:
            now_ms = int(datetime.datetime.now(TZ).timestamp() * 1000)
            for area in sess.pain:
                if area in SORENESS_MUSCLE:
                    con.execute(
                        "INSERT OR REPLACE INTO muscle_soreness(ts, area, severity, note, kind) "
                        "VALUES(?,?,?,?,?)", (now_ms, area, 2, "flagged during a workout", "sharp"))
        con.commit()
    finally:
        con.close()

    # Auto-log to the biocharge pipeline at the session START time, off the
    # request path so the 120s ingest never blocks Finish. The kind follows the
    # session title: a run or a bike ride done in the app is logged as cardio
    # (label run/bike, so the chart shows the right icon and a strap-less
    # session is priced as cardio), everything else stays strength.
    # The client must NOT also call /api/log/workout — this is the single source.
    try:
        start_dt = datetime.datetime.fromisoformat(sess.dateISO).astimezone(TZ)
        dt_str = start_dt.strftime("%Y-%m-%d %H:%M")
        duration_min = min(480, max(1, round(sess.durationSec / 60)))
        kind, label = activities.guided_kind(sess.title)
        background.add_task(append_workout_event, dt_str, duration_min, kind, label=label)
    except (ValueError, TypeError):
        pass  # bad dateISO → skip biocharge log, session is still saved

    return {"status": "ok", "id": sess.id}


@router.delete("/api/workout/session/{sid}")
def workout_delete_session(sid: str):
    # Removes the session row only. Does NOT un-log the events.json workout
    # event already fed to biocharge — that stays.
    con = open_db_rw()
    try:
        con.execute("DELETE FROM workout_session WHERE id=?", (sid,))
        con.commit()
    finally:
        con.close()
    return {"status": "ok"}


_HR_ZONES = [
    ("Recovery", 0.00, 0.60),
    ("Aerobic",  0.60, 0.70),
    ("Tempo",    0.70, 0.80),
    ("Threshold", 0.80, 0.90),
    ("VO₂max", 0.90, 1.01),
]


_CARDIO_DAY_KEYS = {"Tue", "Thu", "Sat"}


def _run_window(con, date_iso: str):
    """Resolve the run for `date_iso` → (start_min, duration_min, work_min, source).

    Prefers a confirmed cardio detection, then a logged session on a cardio day.
    """
    d = con.execute(
        "SELECT start_min, duration_min, work_min, label, kind FROM pai_detection "
        "WHERE date = ? AND status = 'confirmed' "
        "AND (label = 'run' OR (kind = 'cardio' AND label IS NULL)) "
        "ORDER BY duration_min DESC LIMIT 1",
        (date_iso,),
    ).fetchone()
    if d:
        return (int(d["start_min"]), int(d["duration_min"]),
                int(d["work_min"]) if d["work_min"] is not None else None,
                f"detection:{d['label'] or d['kind']}")
    s = con.execute(
        "SELECT date_iso, duration_sec, day_key, title FROM workout_session "
        "WHERE substr(date_iso,1,10) = ? ORDER BY duration_sec DESC LIMIT 1",
        (date_iso,),
    ).fetchone()
    if s and (s["day_key"] in _CARDIO_DAY_KEYS or "run" in (s["title"] or "").lower()
              or "jog" in (s["title"] or "").lower()):
        start = datetime.datetime.fromisoformat(s["date_iso"]).astimezone(TZ)
        start_min = start.hour * 60 + start.minute
        return (start_min, max(1, round(s["duration_sec"] / 60)), None,
                f"session:{s['title']}")
    return None


def _planned_run_segments(con, date_iso: str) -> list:
    """The cardio plan for `date_iso`'s weekday → flat segment list, or []."""
    row = con.execute("SELECT plan_json FROM workout_plan WHERE id = 1").fetchone()
    if not row:
        return []
    try:
        plan = json.loads(row["plan_json"])
    except (ValueError, TypeError):
        return []
    wd = datetime.date.fromisoformat(date_iso).weekday()   # Mon=0
    key = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][wd]
    day = plan.get(key) or {}
    if not day.get("cardio"):
        return []
    variants = day.get("variants") or []
    pick = day.get("pick") or 0
    if pick < 0 or pick >= len(variants):
        pick = 0
    if not variants:
        return []
    segs = []
    for ex in variants[pick].get("exercises", []):
        for st in ex.get("sets", []):
            segs.append({
                "tag": st.get("tag") or ex.get("name"),
                "sec": int(st.get("durationSec") or 0),
                "lo": st.get("hrLo"), "hi": st.get("hrHi"),
            })
    return segs


@router.get("/api/workout/run-review")
def workout_run_review(date: str = Query(...)):
    """Post-run analysis for `date`: HR trace, time-in-zone, plan adherence,
    HRR, calories, and a step-based distance/pace estimate. The strap has no
    GPS, so distance/pace are ~±15 % (steps × stride)."""
    check_date(date)
    acon = open_db()
    try:
        win = _run_window(acon, date)
        if not win:
            return {"available": False, "date": date}
        start_min, dur_min, work_min, source = win
        segments = _planned_run_segments(acon, date)
        base_ms = day_midnight_ms(acon, date)
        try:
            stride_m = body.run_stride(acon)
        except Exception:
            stride_m = None
        hrmax_row = acon.execute(
            "SELECT hrmax FROM fitness_estimate WHERE hrmax IS NOT NULL "
            "ORDER BY date DESC LIMIT 1"
        ).fetchone()
        hrmax = int(hrmax_row["hrmax"]) if hrmax_row and hrmax_row["hrmax"] else 195
    finally:
        acon.close()

    start_ms = base_ms + start_min * 60_000
    end_ms   = start_ms + dur_min * 60_000
    s0, s1   = start_ms // 1000, end_ms // 1000    # activity table = seconds

    try:
        gb = open_gb()
    except Exception:
        return {"available": False, "date": date, "note": "gadgetbridge unavailable"}
    try:
        rows = gb.execute(
            "SELECT TIMESTAMP, HEART_RATE, STEPS FROM HUAMI_EXTENDED_ACTIVITY_SAMPLE "
            "WHERE TIMESTAMP >= ? AND TIMESTAMP < ? ORDER BY TIMESTAMP",
            (s0, s1),
        ).fetchall()
    finally:
        gb.close()

    # Bucket to one point per minute from run start. STEPS is a per-minute delta
    # (~157/min running, verified) — sum it; count effort-window steps apart for
    # cadence, since the cool-down is a walk.
    buckets: dict[int, list] = {}
    steps_total = 0
    steps_work = 0
    work_cut = work_min if work_min else dur_min
    for r in rows:
        m = int((r["TIMESTAMP"] - s0) // 60)
        if r["STEPS"] and r["STEPS"] > 0:
            steps_total += int(r["STEPS"])
            if m < work_cut:
                steps_work += int(r["STEPS"])
        hr = r["HEART_RATE"]
        if hr is None or not (1 <= hr <= 250):
            continue
        buckets.setdefault(m, []).append(int(hr))
    series = [{"t": m, "v": round(sum(v) / len(v))}
              for m, v in sorted(buckets.items())]
    if not series:
        return {"available": False, "date": date, "note": "no heart-rate data for this window"}

    hrs = [p["v"] for p in series]
    avg_hr, peak_hr, min_hr = round(sum(hrs) / len(hrs)), max(hrs), min(hrs)

    # Time in zone (minutes ≈ points).
    zones = []
    for name, lo_f, hi_f in _HR_ZONES:
        lo, hi = lo_f * hrmax, hi_f * hrmax
        n = sum(1 for v in hrs if lo <= v < hi)
        zones.append({"name": name, "lo": round(lo), "hi": round(hi),
                      "min": n, "pct": round(100 * n / len(hrs))})

    # Plan adherence.
    def _seg(tag):
        return next((s for s in segments if (s["tag"] or "").lower() == tag), None)

    target = {"kind": None}
    hard_seg = _seg("hard")
    if hard_seg:
        easy = _seg("easy")
        blk = max(1, round(hard_seg["sec"] / 60))
        # Per-block peaks over the whole run — the strap has no lap markers, so
        # this is a coarse HR shape, not a claim about which block was which rep.
        peaks = []
        for i in range(0, len(hrs), blk):
            chunk = hrs[i:i + blk]
            if chunk:
                peaks.append(max(chunk))
        # Real 5×(hard/easy) intervals oscillate: most adjacent block peaks
        # swing ≥ 20 bpm. A steady run + cool-down has one big step, not many —
        # so gate on the *fraction* of big adjacent swings, not the total range.
        swings = sum(1 for a, b in zip(peaks, peaks[1:]) if abs(a - b) >= 20)
        looks_interval = len(peaks) >= 6 and swings >= 4 and swings >= 0.4 * (len(peaks) - 1)
        # Only when it oscillates like intervals do we score alternating blocks
        # against the hard target; the run's first surge sets the phase.
        hard_lo = hard_seg["lo"] or 0
        first_hard = 0
        if looks_interval:
            first_hard = max(range(len(peaks)), key=lambda j: peaks[j], default=0) % 2
        blocks = []
        for j, pk in enumerate(peaks):
            hard = looks_interval and (j % 2 == first_hard)
            blocks.append({"peak": pk, "hard": bool(hard),
                           "hit": bool(hard and pk >= hard_lo)})
        target = {
            "kind": "intervals",
            "hard_lo": hard_seg["lo"],
            "easy": [easy["lo"], easy["hi"]] if easy else [None, None],
            "looks_interval": looks_interval,
            "block_min": blk,
            "blocks": blocks,
        }
    else:
        band = next((s for s in segments if s["lo"] and s["hi"]), None)
        if band:
            lo, hi = band["lo"], band["hi"]
            inb = sum(1 for v in hrs if lo <= v <= hi)
            above = sum(1 for v in hrs if v > hi)
            below = len(hrs) - inb - above
            target = {"kind": "band", "band": [lo, hi],
                      "in_pct": round(100 * inb / len(hrs)),
                      "above_pct": round(100 * above / len(hrs)),
                      "below_pct": round(100 * below / len(hrs))}

    # HRR — this run's own 1- and 2-min drop after the effort ended.
    eff_end = work_min if work_min else dur_min
    eff_end = min(eff_end, len(series))
    def _hr_at(minute):
        return next((p["v"] for p in series if p["t"] == minute), None)
    ref = max((p["v"] for p in series if eff_end - 2 <= p["t"] <= eff_end), default=None)
    hrr1 = hrr2 = None
    if ref and ref >= 130:
        a1, a2 = _hr_at(eff_end + 1), _hr_at(eff_end + 2)
        if a1 is not None and 5 <= ref - a1 <= 90:
            hrr1 = ref - a1
        if a2 is not None and hrr1 is not None and ref - a2 >= hrr1 - 5:
            hrr2 = ref - a2

    # Calories.
    kcal = None
    try:
        c = calories.activity_cost(date, start_min, dur_min, work_min=work_min)
        kcal = {"net": c.get("net_kcal"), "cooldown": c.get("cooldown_kcal"),
                "total": c.get("total_net_kcal")}
    except Exception:
        pass

    # Distance / pace — step estimate, no GPS. Cadence + pace over the effort
    # window (the cool-down is a walk); distance over every step covered.
    dist = pace = cadence = None
    if work_cut > 0 and steps_work > 0:
        cadence = round(steps_work / work_cut)
    if stride_m and steps_total > 0:
        dist_km = steps_total * stride_m / 1000.0
        if dist_km > 0.3:
            dist = round(dist_km, 2)
            if steps_work > 0:
                run_km = steps_work * stride_m / 1000.0
                pace = round(work_cut * 60 / run_km) if run_km > 0.1 else None

    return {
        "available": True, "date": date, "source": source,
        "start_min": start_min, "duration_min": dur_min, "work_min": work_min,
        "hrmax": hrmax,
        "hr": {"series": series, "avg": avg_hr, "peak": peak_hr, "min": min_hr},
        "zones": zones,
        "target": target,
        "hrr": {"one_min": hrr1, "two_min": hrr2},
        "kcal": kcal,
        "steps": steps_total, "cadence_spm": cadence,
        "distance_km": dist, "pace_s_per_km": pace,
        "stride_m": round(stride_m, 3) if stride_m else None,
        "plan_segments": segments,
    }


@router.post("/api/workout/video/{slug}")
async def workout_upload_video(slug: str, request: Request):
    if not re.fullmatch(r"[a-z0-9-]{1,64}", slug):
        raise HTTPException(400, "bad slug")
    body = await request.body()
    if not body or len(body) > MAX_VIDEO_BYTES:
        raise HTTPException(400, f"media must be 1B–{MAX_VIDEO_BYTES // 1_000_000}MB")

    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    ext = MEDIA_EXTS.get(ctype)
    if ext is None:
        # Some Android pickers send an empty/generic content-type; fall back to
        # the extension the client read off the filename.
        hinted = (request.headers.get("x-file-ext") or "").lstrip(".").lower()
        ext = "jpg" if hinted == "jpeg" else (hinted if hinted in MEDIA_EXT_SET else None)
    if ext is None:
        raise HTTPException(400, "unsupported media type — use mp4, webm, mov, gif, jpg, png or webp")

    os.makedirs(VIDEOS_DIR, exist_ok=True)
    # One file per slug: drop any previously uploaded media of another type.
    for other in MEDIA_EXT_SET:
        if other == ext:
            continue
        other_path = os.path.join(VIDEOS_DIR, f"{slug}.{other}")
        if os.path.exists(other_path):
            os.remove(other_path)
    with open(os.path.join(VIDEOS_DIR, f"{slug}.{ext}"), "wb") as f:
        f.write(body)
    return {"status": "ok", "file": f"/exercise-videos/{slug}.{ext}"}


@router.get("/api/workout/videos")
def workout_list_videos():
    videos = {}
    if os.path.isdir(VIDEOS_DIR):
        pat = re.compile(r"([a-z0-9-]{1,64})\.(" + "|".join(sorted(MEDIA_EXT_SET)) + r")")
        for fn in os.listdir(VIDEOS_DIR):
            m = pat.fullmatch(fn)
            if m:
                videos[m.group(1)] = f"/exercise-videos/{fn}"
    return {"videos": videos}
