// Acta — Workout tab logic.
// Classic script, loaded after the main inline script on all breakpoints.
// Wrapped in an IIFE (coexists with productivity.js on desktop); exposes only
// window.workoutOnShow. All data persists via /api/workout/* (acta.db); only
// the live in-progress session cursor lives in localStorage.
(function () {
  'use strict';

  // ── constants ────────────────────────────────────────────────────────────
  const WK_LS_ACTIVE = 'acta_workout_active_v1';
  const RESTBASE = 45;          // seconds
  const WEIGHT_STEP = 2.5;      // kg
  const C_RING = 2 * Math.PI * 34;   // day-view sets ring
  const C_REST = 2 * Math.PI * 104;  // guided rest ring
  const ACTIVE_TTL_MS = 12 * 3600 * 1000;

  const DAYS = [
    { key: 'Mon', full: 'Monday' }, { key: 'Tue', full: 'Tuesday' },
    { key: 'Wed', full: 'Wednesday' }, { key: 'Thu', full: 'Thursday' },
    { key: 'Fri', full: 'Friday' }, { key: 'Sat', full: 'Saturday' },
    { key: 'Sun', full: 'Sunday' },
  ];
  const DAY_KEYS = DAYS.map(d => d.key);

  const LIBRARY = {
    Chest: ['Bench Press', 'Incline Bench Press', 'Incline DB Press', 'Cable Fly', 'Machine Chest Press', 'Dips', 'Push-up', 'Dumbbell Floor Press', 'Dumbbell Incline Press (feet elevated)', 'Dumbbell Squeeze Press', 'Diamond Push-up', 'Wide Push-up', 'Close-Grip Push-up', 'Push-up (weighted if easy)', 'Pike Push-up', 'Dumbbell Chest Press', 'Incline Bench Press on Smith Machine'],
    Back: ['Deadlift', 'Barbell Row', 'Pull-up', 'Lat Pulldown', 'Chest-Supported Row', 'Seated Cable Row', 'Face Pull', 'Single-Arm Dumbbell Row (each side)', 'Renegade Row (each side)', 'Superman Hold', 'Negative Pull-up (5–8s lower)', 'Negative Pull-up / towel row', 'Dead Hang', 'Bent-Over Row (double-arm)', 'Single-Arm Kettlebell Row (each side)', 'Dumbbell Pullover', 'Kettlebell High Pull', 'Bird Dog (each side)', 'Reverse Snow Angel', 'Prone Y-T-W Raise (each position)', 'Superman (alternating arm/leg, each side)', 'Cable Row', 'T-Bar Row', 'Weighted Pull Ups'],
    Shoulders: ['Overhead Press', 'Seated DB Press', 'Lateral Raise', 'Rear Delt Fly', 'Arnold Press', 'Upright Row', 'Dumbbell Shoulder Press', 'Dumbbell Thruster', 'Kettlebell Halo', 'Front Raise', 'Kettlebell Push Press', 'Lateral + Front Raise Combo (each side)', 'Y-Raise (thumbs up)', 'Rear Delt Fly (light / band face pull)', 'Cable Lateral Raise', 'Cable Rear Delt', 'Shoulder Press'],
    Legs: ['Back Squat', 'Front Squat', 'Romanian Deadlift', 'Leg Press', 'Leg Extension', 'Leg Curl', 'Hip Thrust', 'Calf Raise', 'Goblet Squat', 'Reverse Lunge (each side)', 'Glute Bridge', 'Kettlebell Swing', 'Dumbbell Deadlift', 'Glute Bridge w/ Dumbbell', 'Kettlebell Sumo Squat', 'Single-Leg Romanian Deadlift (each side)', 'Bulgarian Split Squat (each side)', 'Kettlebell Hip Thrust', 'Curtsy Lunge (each side)', 'Step-up (each side)', 'Glute Bridge March (each side)', 'Bodyweight Squat', 'Walking Lunge (each side)', 'Single-Leg Glute Bridge (each side)', 'Wall Sit', 'Abduction', 'Adduction', 'Calves Seated', 'Squat'],
    Arms: ['Barbell Curl', 'Hammer Curl', 'Triceps Pushdown', 'Skull Crusher', 'Preacher Curl', 'Cable Curl', 'Overhead Extension', 'Dumbbell Curl', 'Tricep Kickback', 'Overhead Tricep Extension', 'Tricep Dip (chair/bench)', 'Tricep Dip (chair)', 'Concentration Curl (each side)', 'Reverse Curl', 'Zottman Curl', 'Cross-Body Hammer Curl (each side)', 'Isometric Bicep Hold', 'Dumbbell Bicep Curl', 'Incline Bicep Curl', 'Tricep Push Down'],
    Core: ['Plank', 'Dead Bug', 'Dead Bug (each side)', 'Kettlebell Windmill', 'Hand Grip Squeeze (each hand)', 'Side Plank (each side)', 'Russian Twist w/ Dumbbell (each side)', 'Hollow Body Hold', 'Bicycle Crunch'],
    Conditioning: ['Burpees', 'Mountain Climbers', 'Reverse Lunge to Knee Drive'],
    Cardio: ['Steady run · 145–150 bpm', 'Easy run · 125–135 bpm', 'Warm-up jog · 145–150 bpm', 'Cool-down jog · <130 bpm', 'Intervals · odd set = hard >185 · even set = jog 130–135', 'Cool-down bike', 'Cool-down jog', 'Easy bike', 'Easy run', 'Intervals · 5 × (3 min hard / 3 min easy spin)', 'Intervals · 5 × (3 min hard / 3 min jog)', 'Steady bike', 'Steady run', 'Warm-up bike', 'Warm-up jog'],
    Mobility: ['Moderate Cardio', 'Hip Flexor Stretch', 'Thoracic Spine Rotation', 'Shoulder Mobility'],
  };
  const LIB_FLAT = Object.values(LIBRARY).reduce((a, b) => a.concat(b), []);

  // ── exercise tags + "similar exercises" (BEGIN — the Node test loads exactly this block) ──
  // Powers the "Suggested" group at the top of the change-exercise dropdown and the
  // equipment text next to exercise names. Hand-tagged and deterministic: no model
  // call, so it is instant, works offline at the gym and can never suggest an
  // exercise that isn't in the library.
  //   row:  'Exercise': [ target, pattern, equipment, {t2:[…], time:true} ]
  //   target     the exact muscle worked — a suggestion must share it
  //   pattern    the movement — sharing it ranks higher
  //   equipment  gym (machines/cables/barbell) | dumbbells | kettlebell | bodyweight | gripper
  //   t2         secondary muscles: a candidate is also allowed when its target is in t2
  //              (or vice versa) — but ONLY if it shares the pattern
  //   time       timed hold: reps<->time swaps are never suggested (swapExercise keeps the sets)
  const EX_RAW = {
    // ── Chest ───────────────────────────────────────────────────────────────
    'Bench Press': ['chest', 'press', 'gym', { t2: ['triceps', 'shoulders'] }],
    'Incline Bench Press': ['chest', 'press', 'gym', { t2: ['shoulders'] }],
    'Incline DB Press': ['chest', 'press', 'dumbbells', { t2: ['shoulders'] }],
    'Cable Fly': ['chest', 'fly', 'gym'],
    'Machine Chest Press': ['chest', 'press', 'gym', { t2: ['triceps'] }],
    'Dips': ['chest', 'dip', 'bodyweight', { t2: ['triceps'] }],
    'Push-up': ['chest', 'pushup', 'bodyweight', { t2: ['triceps'] }],
    'Dumbbell Floor Press': ['chest', 'press', 'dumbbells', { t2: ['triceps'] }],
    'Dumbbell Incline Press (feet elevated)': ['chest', 'press', 'dumbbells', { t2: ['shoulders'] }],
    'Dumbbell Squeeze Press': ['chest', 'press', 'dumbbells', { t2: ['triceps'] }],
    'Diamond Push-up': ['triceps', 'pushup', 'bodyweight', { t2: ['chest'] }],
    'Wide Push-up': ['chest', 'pushup', 'bodyweight'],
    'Close-Grip Push-up': ['triceps', 'pushup', 'bodyweight', { t2: ['chest'] }],
    'Push-up (weighted if easy)': ['chest', 'pushup', 'bodyweight', { t2: ['triceps'] }],
    'Pike Push-up': ['shoulders', 'press', 'bodyweight', { t2: ['triceps'] }],
    'Dumbbell Chest Press': ['chest', 'press', 'dumbbells', { t2: ['triceps'] }],
    'Incline Bench Press on Smith Machine': ['chest', 'press', 'gym', { t2: ['shoulders'] }],

    // ── Back ────────────────────────────────────────────────────────────────
    'Deadlift': ['hamstrings', 'hinge', 'gym', { t2: ['glutes', 'lower-back'] }],
    'Barbell Row': ['mid-back', 'row', 'gym', { t2: ['lats'] }],
    'Pull-up': ['lats', 'vertical-pull', 'bodyweight', { t2: ['biceps'] }],
    'Lat Pulldown': ['lats', 'vertical-pull', 'gym', { t2: ['biceps'] }],
    'Chest-Supported Row': ['mid-back', 'row', 'gym', { t2: ['lats'] }],
    'Seated Cable Row': ['mid-back', 'row', 'gym', { t2: ['lats'] }],
    'Face Pull': ['rear-delts', 'rear-delt', 'gym', { t2: ['mid-back'] }],
    'Single-Arm Dumbbell Row (each side)': ['mid-back', 'row', 'dumbbells', { t2: ['lats'] }],
    'Renegade Row (each side)': ['mid-back', 'row', 'dumbbells', { t2: ['core'] }],
    'Superman Hold': ['lower-back', 'extension', 'bodyweight', { time: true }],
    'Negative Pull-up (5–8s lower)': ['lats', 'vertical-pull', 'bodyweight', { t2: ['biceps'] }],
    'Dead Hang': ['forearms', 'hold', 'bodyweight', { time: true, t2: ['lats'] }],
    'Bent-Over Row (double-arm)': ['mid-back', 'row', 'dumbbells', { t2: ['lats'] }],
    'Single-Arm Kettlebell Row (each side)': ['mid-back', 'row', 'kettlebell', { t2: ['lats'] }],
    'Dumbbell Pullover': ['lats', 'pullover', 'dumbbells', { t2: ['chest'] }],
    'Kettlebell High Pull': ['mid-back', 'pull', 'kettlebell', { t2: ['rear-delts', 'shoulders'] }],
    'Bird Dog (each side)': ['lower-back', 'stability', 'bodyweight', { t2: ['core'] }],
    'Reverse Snow Angel': ['rear-delts', 'raise', 'bodyweight', { t2: ['mid-back'] }],
    'Prone Y-T-W Raise (each position)': ['rear-delts', 'raise', 'bodyweight', { t2: ['mid-back'] }],
    'Superman (alternating arm/leg, each side)': ['lower-back', 'extension', 'bodyweight'],
    'T-Bar Row': ['mid-back', 'row', 'gym', { t2: ['lats'] }],
    'Weighted Pull Ups': ['lats', 'vertical-pull', 'gym', { t2: ['biceps'] }],

    // ── Shoulders ───────────────────────────────────────────────────────────
    'Overhead Press': ['shoulders', 'press', 'gym', { t2: ['triceps'] }],
    'Seated DB Press': ['shoulders', 'press', 'dumbbells', { t2: ['triceps'] }],
    'Lateral Raise': ['side-delts', 'raise', 'dumbbells', { t2: ['shoulders'] }],
    'Rear Delt Fly': ['rear-delts', 'rear-delt', 'dumbbells', { t2: ['mid-back'] }],
    'Arnold Press': ['shoulders', 'press', 'dumbbells', { t2: ['triceps'] }],
    'Upright Row': ['side-delts', 'pull', 'gym', { t2: ['shoulders'] }],
    'Dumbbell Shoulder Press': ['shoulders', 'press', 'dumbbells', { t2: ['triceps'] }],
    'Dumbbell Thruster': ['shoulders', 'press', 'dumbbells', { t2: ['quads'] }],
    'Kettlebell Halo': ['shoulders', 'stability', 'kettlebell', { t2: ['core'] }],
    'Front Raise': ['shoulders', 'raise', 'dumbbells', { t2: ['side-delts'] }],
    'Kettlebell Push Press': ['shoulders', 'press', 'kettlebell', { t2: ['triceps'] }],
    'Lateral + Front Raise Combo (each side)': ['side-delts', 'raise', 'dumbbells', { t2: ['shoulders'] }],
    'Y-Raise (thumbs up)': ['rear-delts', 'raise', 'dumbbells', { t2: ['shoulders'] }],
    'Rear Delt Fly (light / band face pull)': ['rear-delts', 'rear-delt', 'dumbbells', { t2: ['mid-back'] }],
    'Cable Lateral Raise': ['side-delts', 'raise', 'gym', { t2: ['shoulders'] }],
    'Cable Rear Delt': ['rear-delts', 'rear-delt', 'gym', { t2: ['mid-back'] }],
    'Shoulder Press': ['shoulders', 'press', 'gym', { t2: ['triceps'] }],

    // ── Legs ────────────────────────────────────────────────────────────────
    'Back Squat': ['quads', 'squat', 'gym', { t2: ['glutes'] }],
    'Front Squat': ['quads', 'squat', 'gym', { t2: ['glutes'] }],
    'Romanian Deadlift': ['hamstrings', 'hinge', 'dumbbells', { t2: ['glutes', 'lower-back'] }],
    'Leg Press': ['quads', 'press', 'gym', { t2: ['glutes'] }],
    'Leg Extension': ['quads', 'extension', 'gym'],
    'Leg Curl': ['hamstrings', 'curl', 'gym'],
    'Hip Thrust': ['glutes', 'bridge', 'gym', { t2: ['hamstrings'] }],
    'Calf Raise': ['calves', 'calf-raise', 'bodyweight'],
    'Goblet Squat': ['quads', 'squat', 'dumbbells', { t2: ['glutes'] }],
    'Reverse Lunge (each side)': ['quads', 'lunge', 'dumbbells', { t2: ['glutes'] }],
    'Glute Bridge': ['glutes', 'bridge', 'bodyweight', { t2: ['hamstrings'] }],
    'Kettlebell Swing': ['glutes', 'hinge', 'kettlebell', { t2: ['hamstrings', 'lower-back'] }],
    'Dumbbell Deadlift': ['hamstrings', 'hinge', 'dumbbells', { t2: ['glutes', 'lower-back'] }],
    'Glute Bridge w/ Dumbbell': ['glutes', 'bridge', 'dumbbells', { t2: ['hamstrings'] }],
    'Kettlebell Sumo Squat': ['quads', 'squat', 'kettlebell', { t2: ['glutes', 'adductors'] }],
    'Single-Leg Romanian Deadlift (each side)': ['hamstrings', 'hinge', 'dumbbells', { t2: ['glutes'] }],
    'Bulgarian Split Squat (each side)': ['quads', 'lunge', 'dumbbells', { t2: ['glutes'] }],
    'Kettlebell Hip Thrust': ['glutes', 'bridge', 'kettlebell', { t2: ['hamstrings'] }],
    'Curtsy Lunge (each side)': ['glutes', 'lunge', 'bodyweight', { t2: ['quads'] }],
    'Step-up (each side)': ['quads', 'lunge', 'bodyweight', { t2: ['glutes'] }],
    'Glute Bridge March (each side)': ['glutes', 'bridge', 'bodyweight', { t2: ['hamstrings'] }],
    'Bodyweight Squat': ['quads', 'squat', 'bodyweight', { t2: ['glutes'] }],
    'Walking Lunge (each side)': ['quads', 'lunge', 'bodyweight', { t2: ['glutes'] }],
    'Single-Leg Glute Bridge (each side)': ['glutes', 'bridge', 'bodyweight', { t2: ['hamstrings'] }],
    'Wall Sit': ['quads', 'hold', 'bodyweight', { time: true }],
    'Abduction': ['glutes', 'abduction', 'gym'],
    'Adduction': ['adductors', 'adduction', 'gym'],
    'Calves Seated': ['calves', 'calf-raise', 'gym'],
    'Squat': ['quads', 'squat', 'gym', { t2: ['glutes'] }],

    // ── Arms ────────────────────────────────────────────────────────────────
    'Barbell Curl': ['biceps', 'curl', 'gym'],
    'Hammer Curl': ['biceps', 'curl', 'dumbbells', { t2: ['forearms'] }],
    'Triceps Pushdown': ['triceps', 'extension', 'gym'],
    'Skull Crusher': ['triceps', 'extension', 'gym'],
    'Preacher Curl': ['biceps', 'curl', 'gym'],
    'Cable Curl': ['biceps', 'curl', 'gym'],
    'Overhead Extension': ['triceps', 'extension', 'dumbbells'],
    'Dumbbell Curl': ['biceps', 'curl', 'dumbbells'],
    'Tricep Kickback': ['triceps', 'extension', 'dumbbells'],
    'Tricep Dip (chair/bench)': ['triceps', 'dip', 'bodyweight', { t2: ['chest'] }],
    'Concentration Curl (each side)': ['biceps', 'curl', 'dumbbells'],
    'Reverse Curl': ['forearms', 'curl', 'dumbbells', { t2: ['biceps'] }],
    'Zottman Curl': ['biceps', 'curl', 'dumbbells', { t2: ['forearms'] }],
    'Cross-Body Hammer Curl (each side)': ['biceps', 'curl', 'dumbbells', { t2: ['forearms'] }],
    'Isometric Bicep Hold': ['biceps', 'hold', 'dumbbells', { time: true }],
    'Incline Bicep Curl': ['biceps', 'curl', 'dumbbells'],

    // ── Core ────────────────────────────────────────────────────────────────
    'Plank': ['core', 'hold', 'bodyweight', { time: true }],
    'Dead Bug': ['core', 'anti-extension', 'bodyweight'],
    'Kettlebell Windmill': ['core', 'lateral', 'kettlebell', { t2: ['shoulders'] }],
    'Hand Grip Squeeze (each hand)': ['forearms', 'grip', 'gripper'],
    'Side Plank (each side)': ['core', 'hold', 'bodyweight', { time: true }],
    'Russian Twist w/ Dumbbell (each side)': ['core', 'rotation', 'dumbbells'],
    'Hollow Body Hold': ['core', 'hold', 'bodyweight', { time: true }],
    'Bicycle Crunch': ['core', 'crunch', 'bodyweight'],

    // ── Conditioning / Mobility ─────────────────────────────────────────────
    'Burpees': ['conditioning', 'full-body', 'bodyweight'],
    'Mountain Climbers': ['conditioning', 'full-body', 'bodyweight'],
    'Reverse Lunge to Knee Drive': ['conditioning', 'full-body', 'bodyweight'],
    'Moderate Cardio': ['mobility', 'cardio', 'bodyweight'],
    'Hip Flexor Stretch': ['mobility', 'stretch', 'bodyweight'],
    'Thoracic Spine Rotation': ['mobility', 'stretch', 'bodyweight'],
    'Shoulder Mobility': ['mobility', 'stretch', 'bodyweight'],
  };

  // Library names that are the same exercise as another entry (shown once, never
  // suggested as a replacement for each other). alias -> canonical.
  const EX_ALIAS = {
    'Tricep Push Down': 'Triceps Pushdown',
    'Overhead Tricep Extension': 'Overhead Extension',
    'Tricep Dip (chair)': 'Tricep Dip (chair/bench)',
    'Dead Bug (each side)': 'Dead Bug',
    'Cable Row': 'Seated Cable Row',
    'Dumbbell Bicep Curl': 'Dumbbell Curl',
    'Negative Pull-up / towel row': 'Negative Pull-up (5–8s lower)',
  };

  const EX_META = {};
  Object.keys(EX_RAW).forEach(k => {
    const r = EX_RAW[k], o = r[3] || {};
    EX_META[k] = { t: r[0], p: r[1], eq: r[2], t2: o.t2 || [], time: !!o.time };
  });
  function exCanon(name) { return EX_ALIAS[name] || name; }

  // Similar exercises for `name`, best first. opts: { exclude:[names already in the
  // session], hasMedia(name)->bool, limit }. Returns [{name, eq, score}].
  function similarTo(name, opts) {
    opts = opts || {};
    const me = EX_META[exCanon(name)];
    if (!me) return [];
    const skip = {};
    skip[exCanon(name)] = 1;
    (opts.exclude || []).forEach(n => { skip[exCanon(n)] = 1; });
    const out = [];
    Object.keys(EX_META).forEach(n => {
      if (skip[n]) return;
      const c = EX_META[n];
      if (c.time !== me.time) return;                      // reps <-> time is never a like-for-like swap
      let s;
      if (c.t === me.t) s = 100;                           // same exact muscle
      else if (c.p === me.p && (me.t2.indexOf(c.t) !== -1 || c.t2.indexOf(me.t) !== -1)) s = 40;   // secondary overlap needs the same movement too
      else return;
      if (c.p === me.p) s += 30;
      if (c.eq === me.eq) s += 10;
      if (opts.hasMedia && opts.hasMedia(n)) s += 3;
      out.push({ name: n, eq: c.eq, score: s });
    });
    out.sort((a, b) => b.score - a.score || a.name.localeCompare(b.name));
    return out.slice(0, opts.limit || 5);
  }

  // Equipment shown next to an exercise name ('' when untagged, e.g. cardio entries).
  function eqLabel(name) { const m = EX_META[exCanon(name)]; return m ? m.eq : ''; }
  // canonical -> [aliases], so "has an image" also counts an alias that owns the upload
  const EX_ALIASES_OF = {};
  Object.keys(EX_ALIAS).forEach(a => { (EX_ALIASES_OF[EX_ALIAS[a]] = EX_ALIASES_OF[EX_ALIAS[a]] || []).push(a); });
  // ── exercise tags (END) ──

  const RPE_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
  // Body areas offered on the finish screen for a quick pain/niggle flag.
  // Slugs are stored; the readiness card + digest surface them as a rule.
  const PAIN_AREAS = [
    ['shoulder', 'Shoulder'], ['elbow', 'Elbow'], ['wrist', 'Wrist'],
    ['neck', 'Neck'], ['lower-back', 'Lower back'], ['hip', 'Hip'],
    ['knee', 'Knee'], ['ankle', 'Ankle'],
  ];

  function muscleOf(name) {
    for (const m in LIBRARY) { if (LIBRARY[m].indexOf(name) !== -1) return m; }
    return 'Other';
  }
  function uid() { return 'e' + Math.random().toString(36).slice(2, 8); }
  function slug(s) { return String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  // ── time-based exercises (planks, cardio, holds) ─────────────────────────
  // An exercise with mode:'time' uses a per-set duration + a countdown in the
  // guided session, instead of weight×reps. Sets carry {durationSec, actualSec}.
  function isTime(ex) { return !!ex && ex.mode === 'time'; }
  // ── how much weight a set actually moves (2026-09-21) ───────────────────────────────────────────────
  // I log the number written on the implement and never adds hands together: two 12.5 kg
  // dumbbells are entered as "12.5", a machine is the stack as set, a barbell is the loaded bar. So one
  // line of a two-dumbbell exercise moves twice what it says — and so does an "each side" exercise, where
  // the same reps are done once per limb. One multiplier covers both cases.
  //
  // The default comes from the exercise name, because the name is the only thing the plan carries. An
  // explicit ex.x2 (true or false) wins, for the ones a name cannot settle: a barbell curl and an
  // overhead extension held with both hands are single (x1); a machine I named after a dumbbell
  // movement is single too. Weight entered before this existed was a mix of conventions, so volume from
  // older sessions is not comparable with today's — the stored session rows keep their old numbers.
  const X2_NAMES = [
    // chest — one dumbbell in each hand
    'Incline DB Press', 'Dumbbell Floor Press', 'Dumbbell Incline Press (feet elevated)',
    'Dumbbell Squeeze Press', 'Dumbbell Chest Press',
    // shoulders
    'Seated DB Press', 'Lateral Raise', 'Lateral Raises', 'Rear Delt Fly',
    'Rear Delt Fly (light / band face pull)', 'Arnold Press', 'Dumbbell Shoulder Press',
    'Dumbbell Thruster', 'Front Raise', 'Y-Raise (thumbs up)',
    // legs
    'Dumbbell Deadlift',
    // arms
    'Hammer Curl', 'Dumbbell Curl', 'Dumbbell Bicep Curl', 'Incline Bicep Curl', 'Zottman Curl',
    'Tricep Kickback', 'Tricep Kickbacks',
  ];
  const X2_EACH = /\beach (side|hand|arm|leg)\b/i;   // "(each side)", "(each hand)" — reps done twice
  function exMult(ex) {
    if (!ex) return 1;
    if (typeof ex.x2 === 'number') return ex.x2 > 0 ? ex.x2 : 1;   // 4 = two hands AND each side (split squat)
    if (ex.x2 === true) return 2;
    if (ex.x2 === false) return 1;
    const n = String(ex.name || '');
    return (X2_EACH.test(n) || X2_NAMES.indexOf(n) !== -1) ? 2 : 1;
  }
  function setVol(ex, s) { return isTime(ex) ? 0 : exMult(ex) * (s.weight || 0) * (s.reps == null ? (s.targetReps || 0) : s.reps); }
  function planVol(ex, s) { return isTime(ex) ? 0 : exMult(ex) * (s.weight || 0) * (s.targetReps || 0); }
  function exVolDone(ex) { return ex.sets.reduce((a, s) => a + (s.done ? setVol(ex, s) : 0), 0); }
  function fmtDur(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    const m = Math.floor(sec / 60), s = sec % 60;
    return m + ':' + String(s).padStart(2, '0');
  }
  function parseDur(raw) {
    if (raw == null) return null;
    const str = String(raw).trim(); if (str === '') return null;
    if (str.indexOf(':') >= 0) {
      const [a, b] = str.split(':');
      return (parseInt(a, 10) || 0) * 60 + (parseInt(b, 10) || 0);
    }
    const n = parseFloat(str); return Number.isNaN(n) ? null : Math.round(n); // bare = seconds
  }
  function setDurVal(s) { return s.done && s.actualSec != null ? s.actualSec : (s.durationSec || 0); }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // mkEx(name, sets, muscle?, mode?) — `sets` is [[reps, weight], …] for reps
  // mode, or [[seconds], …] for a time exercise (mode:'time'). Strength moves in
  // the seeded plan start at bodyweight (weight 0) — filled in per set as loads
  // are chosen.
  function mkEx(name, sets, muscle, mode) {
    const time = mode === 'time';
    const ex = {
      id: uid(), name, muscle: muscle || muscleOf(name),
      sets: sets.map(p => time
        ? { durationSec: p[0], actualSec: null, rpe: null, done: false }
        : { targetReps: p[0], weight: (p[1] || 0), reps: null, rpe: null, done: false }),
    };
    if (time) ex.mode = 'time';
    return ex;
  }
  // n straight sets of `reps` reps at bodyweight; n timed sets of `sec` seconds.
  function sr(n, reps) { const a = []; for (let i = 0; i < n; i++) a.push([reps, 0]); return a; }
  function st(n, sec) { const a = []; for (let i = 0; i < n; i++) a.push([sec]); return a; }
  function mkVariant(name, exercises) { return { id: uid(), name, exercises }; }

  // A run segment = a time-mode exercise whose sets carry an HR target
  // (hrLo/hrHi, either nullable) and a tag ('Warm-up' | 'Hard' | 'Easy' |
  // 'Cool-down' | 'Steady'). specs: [{ sec, lo, hi, tag }].
  function mkRun(name, specs) {
    return {
      id: uid(), name, muscle: 'Cardio', mode: 'time',
      sets: specs.map(s => ({
        durationSec: s.sec, actualSec: null, rpe: null, done: false,
        hrLo: s.lo == null ? null : s.lo,
        hrHi: s.hi == null ? null : s.hi,
        tag: s.tag || null,
      })),
    };
  }
  function intervalSpecs(rounds, workSec, workLo, easySec, easyLo, easyHi) {
    const out = [];
    for (let i = 0; i < rounds; i++) {
      out.push({ sec: workSec, lo: workLo, hi: null, tag: 'Hard' });
      out.push({ sec: easySec, lo: easyLo, hi: easyHi, tag: 'Easy' });
    }
    return out;
  }

  // The plan is a 7-day template of day *types*, each holding one or more
  // interchangeable exercise variants (List A/B/C · No-equipment). `pick` is the
  // selected variant, remembered across the weekly reset. Cardio days set
  // `cardio: true` (rendered as a run card, not the set grid) and hold one
  // variant. Marked `_schema: 3`: an older plan auto-migrates on load — a
  // pre-variant Push/Pull/Legs plan is replaced wholesale, a `_schema: 2` plan
  // keeps its strength days and only regenerates the cardio ones (see
  // ensureSeeded).
  function defaultPlan() {
    const friTail = () => [
      mkEx('Negative Pull-up (5–8s lower)', sr(3, 4), 'Back'),
      mkEx('Dead Hang', st(3, 25), 'Back', 'time'),
      mkEx('Hand Grip Squeeze (each hand)', sr(3, 18), 'Core'),
    ];
    return {
      _schema: 3,
      Mon: { type: 'Upper Body Push', pick: 0, variants: [
        mkVariant('List A', [
          mkEx('Dumbbell Floor Press', sr(4, 10), 'Chest'),
          mkEx('Dumbbell Shoulder Press', sr(4, 10), 'Shoulders'),
          mkEx('Lateral Raise', sr(3, 12), 'Shoulders'),
          mkEx('Tricep Kickback', sr(3, 12), 'Arms'),
          mkEx('Push-up (weighted if easy)', sr(3, 15), 'Chest'),
        ]),
        mkVariant('List B', [
          mkEx('Dumbbell Incline Press (feet elevated)', sr(4, 10), 'Chest'),
          mkEx('Arnold Press', sr(4, 10), 'Shoulders'),
          mkEx('Front Raise', sr(3, 12), 'Shoulders'),
          mkEx('Overhead Tricep Extension', sr(3, 12), 'Arms'),
          mkEx('Diamond Push-up', sr(3, 15), 'Arms'),
        ]),
        mkVariant('List C', [
          mkEx('Dumbbell Squeeze Press', sr(4, 10), 'Chest'),
          mkEx('Kettlebell Push Press', sr(4, 8), 'Shoulders'),
          mkEx('Lateral + Front Raise Combo (each side)', sr(3, 10), 'Shoulders'),
          mkEx('Close-Grip Push-up', sr(3, 15), 'Chest'),
          mkEx('Tricep Dip (chair/bench)', sr(3, 12), 'Arms'),
        ]),
        mkVariant('No equipment', [
          mkEx('Push-up', sr(4, 15), 'Chest'),
          mkEx('Pike Push-up', sr(3, 12), 'Shoulders'),
          mkEx('Diamond Push-up', sr(3, 12), 'Arms'),
          mkEx('Wide Push-up', sr(3, 15), 'Chest'),
          mkEx('Tricep Dip (chair)', sr(3, 12), 'Arms'),
        ]),
      ] },
      Tue: { type: 'Steady run', pick: 0, cardio: true, variants: [
        mkVariant('Run', [
          mkRun('Steady run', [{ sec: 2220, lo: 145, hi: 150, tag: 'Steady' }]),
        ]),
      ] },
      Wed: { type: 'Lower Body + Core', pick: 0, variants: [
        mkVariant('List A', [
          mkEx('Goblet Squat', sr(4, 12), 'Legs'),
          mkEx('Romanian Deadlift', sr(4, 10), 'Legs'),
          mkEx('Reverse Lunge (each side)', sr(3, 10), 'Legs'),
          mkEx('Glute Bridge w/ Dumbbell', sr(3, 15), 'Legs'),
          mkEx('Plank', st(3, 45), 'Core', 'time'),
          mkEx('Dead Bug (each side)', sr(3, 10), 'Core'),
        ]),
        mkVariant('List B', [
          mkEx('Kettlebell Sumo Squat', sr(4, 12), 'Legs'),
          mkEx('Single-Leg Romanian Deadlift (each side)', sr(4, 8), 'Legs'),
          mkEx('Bulgarian Split Squat (each side)', sr(3, 10), 'Legs'),
          mkEx('Kettlebell Hip Thrust', sr(3, 15), 'Legs'),
          mkEx('Side Plank (each side)', st(3, 30), 'Core', 'time'),
          mkEx('Russian Twist w/ Dumbbell (each side)', sr(3, 15), 'Core'),
        ]),
        mkVariant('List C', [
          mkEx('Kettlebell Swing', sr(4, 15), 'Legs'),
          mkEx('Curtsy Lunge (each side)', sr(3, 10), 'Legs'),
          mkEx('Step-up (each side)', sr(3, 10), 'Legs'),
          mkEx('Glute Bridge March (each side)', sr(3, 10), 'Legs'),
          mkEx('Mountain Climbers', sr(3, 20), 'Conditioning'),
          mkEx('Hollow Body Hold', st(3, 30), 'Core', 'time'),
        ]),
        mkVariant('No equipment', [
          mkEx('Bodyweight Squat', sr(4, 20), 'Legs'),
          mkEx('Walking Lunge (each side)', sr(3, 12), 'Legs'),
          mkEx('Single-Leg Glute Bridge (each side)', sr(3, 12), 'Legs'),
          mkEx('Wall Sit', st(3, 45), 'Legs', 'time'),
          mkEx('Plank', st(3, 45), 'Core', 'time'),
          mkEx('Bicycle Crunch', sr(3, 20), 'Core'),
        ]),
      ] },
      Thu: { type: 'Easy run', pick: 0, cardio: true, variants: [
        mkVariant('Run', [
          mkRun('Easy run', [{ sec: 1320, lo: 125, hi: 135, tag: 'Easy' }]),
        ]),
      ] },
      Fri: { type: 'Upper Body Pull', pick: 0, variants: [
        mkVariant('List A', [
          mkEx('Single-Arm Dumbbell Row (each side)', sr(4, 10), 'Back'),
          mkEx('Renegade Row (each side)', sr(3, 8), 'Back'),
          mkEx('Dumbbell Curl', sr(3, 12), 'Arms'),
          mkEx('Hammer Curl', sr(3, 12), 'Arms'),
          mkEx('Rear Delt Fly', sr(3, 15), 'Shoulders'),
          mkEx('Superman Hold', sr(3, 12), 'Back'),
        ].concat(friTail())),
        mkVariant('List B', [
          mkEx('Bent-Over Row (double-arm)', sr(4, 10), 'Back'),
          mkEx('Kettlebell High Pull', sr(3, 10), 'Back'),
          mkEx('Concentration Curl (each side)', sr(3, 12), 'Arms'),
          mkEx('Reverse Curl', sr(3, 12), 'Arms'),
          mkEx('Rear Delt Fly (light / band face pull)', sr(3, 15), 'Shoulders'),
          mkEx('Bird Dog (each side)', sr(3, 10), 'Back'),
        ].concat(friTail())),
        mkVariant('List C', [
          mkEx('Single-Arm Kettlebell Row (each side)', sr(4, 10), 'Back'),
          mkEx('Dumbbell Pullover', sr(3, 12), 'Back'),
          mkEx('Zottman Curl', sr(3, 10), 'Arms'),
          mkEx('Cross-Body Hammer Curl (each side)', sr(3, 12), 'Arms'),
          mkEx('Y-Raise (thumbs up)', sr(3, 12), 'Shoulders'),
          mkEx('Superman (alternating arm/leg, each side)', sr(3, 10), 'Back'),
        ].concat(friTail())),
        mkVariant('No equipment', [
          mkEx('Negative Pull-up / towel row', sr(4, 8), 'Back'),
          mkEx('Superman Hold', sr(3, 12), 'Back'),
          mkEx('Reverse Snow Angel', sr(3, 15), 'Back'),
          mkEx('Prone Y-T-W Raise (each position)', sr(3, 10), 'Back'),
          mkEx('Bird Dog (each side)', sr(3, 10), 'Back'),
          mkEx('Isometric Bicep Hold', st(3, 20), 'Arms', 'time'),
          mkEx('Dead Hang', st(3, 25), 'Back', 'time'),
          mkEx('Hand Grip Squeeze (each hand)', sr(3, 18), 'Core'),
        ]),
      ] },
      Sat: { type: 'Interval run', pick: 0, cardio: true, variants: [
        mkVariant('Run', [
          mkRun('Warm-up jog', [{ sec: 900, lo: 145, hi: 150, tag: 'Warm-up' }]),
          mkRun('Intervals · 5 × (3 min hard / 3 min jog)',
                intervalSpecs(5, 180, 186, 180, 130, 135)),
          mkRun('Cool-down jog', [{ sec: 600, lo: null, hi: 130, tag: 'Cool-down' }]),
        ]),
      ] },
      Sun: { type: 'Rest', pick: 0, variants: [] },
    };
  }

  function todayKey() { return ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][new Date().getDay()]; }
  function mondayKeyOf(d) {
    const x = new Date(d); const dow = (x.getDay() + 6) % 7;
    x.setDate(x.getDate() - dow); x.setHours(0, 0, 0, 0);
    return x.getFullYear() + '-' + x.getMonth() + '-' + x.getDate();
  }
  function currentMondayKey() { return mondayKeyOf(new Date()); }

  // ── state ────────────────────────────────────────────────────────────────
  const WK = {
    loaded: false, fetchedAt: 0,
    plan: null, weekKey: null, sessions: [], videos: {}, soreness: {},
    activeDay: todayKey(),
    timer: { running: false, startEpoch: null, accumSec: 0 },
    training: { active: false, paused: false, day: null, phase: 'work', exIndex: 0, setIndex: 0, restEndEpoch: null, sessionStartEpoch: null },
    ui: { historyOpen: false, libraryOpen: false, calOpen: false, selectedWeek: null, calOffset: 0, swapOpen: false, showDismissed: false, runReviewDate: null, mediaPop: null, confirmReset: false, pendingSwap: null, settingsOpen: false },
  };
  let audioCtx = null;
  let tickInt = null;
  let pendingUploadSlug = null;
  let fileInput = null;

  // ── formatting helpers ───────────────────────────────────────────────────
  function pad(n) { return String(n).padStart(2, '0'); }
  function fmtTime(t) {
    t = Math.max(0, Math.floor(t));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return h > 0 ? h + ':' + pad(m) + ':' + pad(s) : pad(m) + ':' + pad(s);
  }
  function fmtKg(n) { n = Math.round(n * 100) / 100; return String(n); }
  function fmtVol(n) { return Math.round(n).toLocaleString('en-US') + ' kg'; }
  function elapsedSec() {
    const t = WK.timer;
    return t.accumSec + (t.running && t.startEpoch ? (Date.now() - t.startEpoch) / 1000 : 0);
  }
  function restLeft() {
    if (!WK.training.restEndEpoch) return 0;
    return Math.max(0, Math.ceil((WK.training.restEndEpoch - Date.now()) / 1000));
  }
  function weekRangeLabel(key) {
    if (!key) return '';
    const p = key.split('-'); const mon = new Date(+p[0], +p[1], +p[2]); const sun = new Date(mon); sun.setDate(sun.getDate() + 6);
    const M = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const a = M[mon.getMonth()] + ' ' + mon.getDate();
    const b = (mon.getMonth() === sun.getMonth() ? String(sun.getDate()) : M[sun.getMonth()] + ' ' + sun.getDate());
    return a + ' – ' + b;
  }
  function buildCalendar() {
    const base = new Date(); base.setHours(0, 0, 0, 0); base.setDate(1); base.setMonth(base.getMonth() + (WK.ui.calOffset || 0));
    const calY = base.getFullYear(), calM = base.getMonth();
    const MN = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
    const wkDays = new Set((WK.sessions || []).map(se => { const d = new Date(se.dateISO); return d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate(); }));
    const lastOfMonth = new Date(calY, calM + 1, 0);
    const start = new Date(calY, calM, 1); const lead = (start.getDay() + 6) % 7; start.setDate(start.getDate() - lead);
    const tnow = new Date();
    const weeks = []; const cur = new Date(start);
    while (cur <= lastOfMonth) {
      const mondayKey = cur.getFullYear() + '-' + cur.getMonth() + '-' + cur.getDate();
      const cells = [];
      for (let i = 0; i < 7; i++) {
        const inMonth = cur.getMonth() === calM;
        const has = wkDays.has(cur.getFullYear() + '-' + cur.getMonth() + '-' + cur.getDate());
        const isToday = tnow.getFullYear() === cur.getFullYear() && tnow.getMonth() === cur.getMonth() && tnow.getDate() === cur.getDate();
        cells.push({ num: String(cur.getDate()), mark: has ? '✓' : '', inMonth, has, isToday });
        cur.setDate(cur.getDate() + 1);
      }
      weeks.push({ mondayKey, cells });
    }
    return { monthLabel: MN[calM] + ' ' + calY, weeks };
  }

  // ── audio chime ──────────────────────────────────────────────────────────
  function ensureAudio() {
    if (audioCtx) return;
    try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { audioCtx = null; }
  }
  function wkChime() {
    if (!audioCtx) return;
    try {
      const now = audioCtx.currentTime;
      [[880, 0], [1320, 0.16]].forEach(([f, dt]) => {
        const o = audioCtx.createOscillator(), g = audioCtx.createGain();
        o.type = 'sine'; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, now + dt);
        g.gain.exponentialRampToValueAtTime(0.3, now + dt + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, now + dt + 0.18);
        o.connect(g); g.connect(audioCtx.destination);
        o.start(now + dt); o.stop(now + dt + 0.2);
      });
    } catch (e) { /* ignore */ }
  }

  // ── screen wake lock (guided cardio only) ────────────────────────────────
  // Keeps the phone awake through a run so the interval chimes fire. Android
  // Chrome supports this; the lock auto-drops when the tab is hidden, so it is
  // re-requested on visibilitychange while a cardio session is live.
  let wakeLock = null;
  async function acquireWakeLock() {
    try {
      if ('wakeLock' in navigator && navigator.wakeLock && !wakeLock) {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
      }
    } catch (e) { wakeLock = null; }
  }
  function releaseWakeLock() {
    try { if (wakeLock) wakeLock.release(); } catch (e) { }
    wakeLock = null;
  }
  function trainingIsCardio() {
    return WK.training.active && isCardioDay(sourceDay(WK.training.day || WK.activeDay));
  }

  // ── API layer ────────────────────────────────────────────────────────────
  async function apiGetPlan() {
    const r = await fetch('/api/workout/plan'); return r.ok ? r.json() : { plan: null };
  }
  async function apiPutPlan() {
    try {
      await fetch('/api/workout/plan', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: WK.plan, week_key: WK.weekKey }),
      });
    } catch (e) { /* offline — in-memory state stays; retried on next save */ }
  }
  // Readiness trim — "Aplicar" button on the readiness card. Stores only the
  // substitute `variants` array under a date-stamped key; WK.plan[day_key]
  // (the recurring template) is never touched, so next week's session is
  // back to the full plan with no cleanup needed beyond maybeWeekReset().
  async function applyTrainingTrim() {
    if (!WK.plan) return;
    let t = null;
    try { const r = await fetch('/api/workout/trim'); if (r.ok) t = await r.json(); } catch (e) { }
    if (!t || !t.pct || !t.plan) return;
    WK.plan._trim_applied = { date: todayISO(), day_key: t.day_key, variants: t.plan.variants };
    await apiPutPlan();
  }
  async function undoTrainingTrim() {
    if (!WK.plan) return;
    delete WK.plan._trim_applied;
    await apiPutPlan();
  }
  let saveTimer = null;
  function savePlanDebounced() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(apiPutPlan, 1500);
  }
  async function apiGetSessions() {
    const r = await fetch('/api/workout/sessions?limit=300'); return r.ok ? r.json() : [];
  }

  // Detected activity lives in pai_detection, NOT workout_session -- a match has
  // no sets, volume or exercises, and forcing it into that table would corrupt
  // the volume trends. Separate tables, joined only in the history view.
  async function apiActivities() {
    try {
      const [h, p] = await Promise.all([
        fetch('/api/pai/history').then(r => r.ok ? r.json() : { confirmed: [], dismissed: [] }),
        fetch('/api/pai/detections').then(r => r.ok ? r.json() : { detections: [] }),
      ]);
      return [].concat(
        (p.detections || []).map(a => Object.assign({ _state: 'pending' }, a)),
        (h.confirmed || []).map(a => Object.assign({ _state: 'confirmed' }, a)),
        (h.dismissed || []).map(a => Object.assign({ _state: 'dismissed' }, a)));
    } catch (e) { return []; }
  }
  async function apiPostSession(sess) {
    // NOTE: do NOT also call /api/log/workout — the server logs the biocharge
    // strength event itself from this call (BackgroundTasks).
    try {
      await fetch('/api/workout/session', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sess),
      });
    } catch (e) { /* ignore */ }
  }
  async function apiDeleteSession(sid) {
    try { await fetch('/api/workout/session/' + encodeURIComponent(sid), { method: 'DELETE' }); } catch (e) { }
  }
  async function apiListVideos() {
    try { const r = await fetch('/api/workout/videos'); return r.ok ? (await r.json()).videos || {} : {}; } catch (e) { return {}; }
  }
  async function apiUploadVideo(slugName, file) {
    // X-File-Ext is a fallback for pickers that hand over a blank file.type.
    const ext = (file.name || '').split('.').pop().toLowerCase();
    const r = await fetch('/api/workout/video/' + slugName, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-File-Ext': ext },
      body: file,
    });
    return r.ok ? r.json() : null;
  }

  // Stored media is one file per exercise — video, GIF or still photo. The
  // extension decides which tag to render; GIFs animate inside <img>.
  const VIDEO_EXTS = ['mp4', 'webm', 'mov'];
  function isVideoUrl(url) {
    const ext = String(url).split('?')[0].split('.').pop().toLowerCase();
    return VIDEO_EXTS.indexOf(ext) !== -1;
  }
  function mediaTag(url, style) {
    const s = style ? ' style="' + style + '"' : '';
    return isVideoUrl(url)
      ? '<video src="' + esc(url) + '" autoplay muted loop playsinline preload="metadata"' + s + '></video>'
      : '<img src="' + esc(url) + '" alt=""' + s + '>';
  }

  // Media is uploaded from the library, keyed by the canonical exercise name.
  // Plan exercises carry qualifiers the library names don't — "Renegade Row
  // (each side)", "Superman Holds", "Goblet Squat — 3s pause at bottom",
  // "Glute Bridge w/ Dumbbell" — so a raw slug lookup misses the file during a
  // guided session. normKey() strips those so both sides land on one key.
  function normKey(name) {
    const base = String(name).toLowerCase()
      .replace(/\([^)]*\)/g, ' ')               // "(each side)", "(weighted if easy)"
      .split(/[—–]|\s-\s/)[0]                   // "— 3s pause at bottom"
      .split(/\sw\/\s|\swith\s|\susing\s/)[0];  // "w/ Dumbbell"
    return slug(base).replace(/([^s])s$/, '$1'); // "raises"→"raise"; keeps "press"
  }
  // ── soreness chips ─────────────────────────────────────────────────────────
  // Logged on the body map in the "add info" modal. Purely advisory: it marks
  // exercises that load a sore muscle so the choice is visible before the set,
  // and never touches the readiness score or the plan itself.
  // Words follow the body map's type + intensity (api muscle_detail: {severity 1-3, kind}).
  const SORE_WORDS = {
    ache:  ['a bit sore', 'sore', 'very sore'],  tight: ['a bit tight', 'tight', 'very tight'],
    heavy: ['a bit tired', 'tired', 'very tired'], sharp: ['mild pain', 'pain', 'severe pain'],
  };
  const SORE_COLOR = { 1: 'oklch(0.80 0.13 85)', 2: 'oklch(0.70 0.17 45)', 3: 'oklch(0.62 0.20 25)' };
  function soreChip(muscle) {
    const d = WK.soreness[muscle];
    if (!d) return '';
    const sev = d.severity, kind = d.kind;
    // Skipping is only worth saying for the worst cases: severe anything, or moderate sharp pain.
    const label = (SORE_WORDS[kind] || SORE_WORDS.ache)[sev - 1] + (sev >= 3 || (kind === 'sharp' && sev >= 2) ? ' — consider skipping' : '');
    return '<span title="Logged in the last 72h" style="font-family:var(--mono);font-size:12px;' +
      'letter-spacing:0.01em;padding:2px 7px;border-radius:4px;' +
      'border:1px solid ' + SORE_COLOR[sev] + ';color:' + SORE_COLOR[sev] + ';">⚠ ' + label + '</span>';
  }
  async function loadSoreness() {
    try {
      const r = await fetch('/api/soreness/current');
      WK.soreness = r.ok ? (await r.json()).muscle_detail || {} : {};
    } catch (e) { WK.soreness = {}; }
  }

  function mediaFor(name) {
    const exact = WK.videos[slug(name)];
    if (exact) return exact;
    const key = normKey(name);
    for (const k in WK.videos) { if (normKey(k) === key) return WK.videos[k]; }
    return null;
  }

  // ── active-session localStorage ──────────────────────────────────────────
  function saveActiveLS() {
    try {
      localStorage.setItem(WK_LS_ACTIVE, JSON.stringify({
        savedAt: Date.now(), timer: WK.timer, training: WK.training, activeDay: WK.activeDay,
      }));
    } catch (e) { }
  }
  function clearActiveLS() { try { localStorage.removeItem(WK_LS_ACTIVE); } catch (e) { } }
  function loadActiveLS() {
    try {
      const raw = localStorage.getItem(WK_LS_ACTIVE);
      if (!raw) return;
      const s = JSON.parse(raw);
      if (!s || Date.now() - (s.savedAt || 0) > ACTIVE_TTL_MS) { clearActiveLS(); return; }
      if (s.timer) WK.timer = s.timer;
      if (s.training) WK.training = s.training;
      if (s.activeDay) WK.activeDay = s.activeDay;
      // A session that was left mid-flight comes back paused, not active.
      if (WK.training.active) { WK.training.active = false; WK.training.paused = true; }
    } catch (e) { }
  }

  // ── seed / week reset ────────────────────────────────────────────────────
  async function ensureSeeded(resp) {
    const stored = resp && resp.plan;
    const sch = (stored && stored._schema) || 0;
    if (sch >= 2) {
      WK.plan = stored; WK.weekKey = resp.week_key || currentMondayKey();
      if (sch < 3) {
        // v3: cardio days gained structured HR targets + a `cardio` flag.
        // Regenerate only those — strength-day edits and variant picks survive.
        const fresh = defaultPlan();
        ['Tue', 'Thu', 'Sat', 'Sun'].forEach(k => { WK.plan[k] = fresh[k]; });
        WK.plan._schema = 3;
        apiPutPlan();
      }
    } else {
      // No plan yet, or a pre-variant (Push/Pull/Legs) plan → seed the current
      // template. The old plan is replaced, not migrated in place.
      WK.plan = defaultPlan(); WK.weekKey = currentMondayKey();
      await apiPutPlan();
    }
    if (!WK.plan[WK.activeDay] || dayExercises(WK.activeDay).length === 0) {
      // land on today; if today is a rest day, keep it (shows rest state)
      WK.activeDay = todayKey();
    }
  }
  function maybeWeekReset() {
    if (WK.training.active) return;
    const cur = currentMondayKey();
    if (WK.weekKey === cur) return;
    DAY_KEYS.forEach(k => variantsOf(k).forEach(v => (v.exercises || []).forEach(e =>
      e.sets.forEach(s => { s.done = false; s.reps = null; s.rpe = null; s.actualSec = null; }))));
    delete WK.plan._override;      // a day swap never carries into a new week
    delete WK.plan._trim_applied;  // same for a readiness trim — always re-derived, never carried
    WK.weekKey = cur;
    apiPutPlan();
  }

  // ── one-off day-type override ────────────────────────────────────────────
  // "I forgot push day, do it today." The weekly plan is a template reused every
  // week, so swapping a day must NOT rewrite it. Instead a single override lives
  // inside the plan doc under a non-day key (`_override`) — the API only requires
  // the 7 day keys to be present, so extra keys ride along with no backend change.
  // It is stamped with a date and only honoured on that date, so it expires by
  // itself when the day rolls over. Nothing is ever lost or overwritten.
  function todayISO() {
    const d = new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }
  function activeOverride() {
    const o = WK.plan && WK.plan._override;
    if (!o || o.date !== todayISO()) return null;
    if (DAY_KEYS.indexOf(o.from) === -1 || o.from === todayKey()) return null;
    return o;
  }
  // The day key whose exercises should actually be used for `dayKey`.
  function sourceDay(dayKey) {
    const o = activeOverride();
    return (o && dayKey === todayKey()) ? o.from : dayKey;
  }

  // ── day type + selectable variants ──────────────────────────────────────
  // WK.plan[day] = { type, pick, variants:[{id,name,exercises}] }. A cardio or
  // rest day carries one variant (the chip row hides). `pick` persists across
  // the weekly reset — the exercise progress is what clears, not the choice.
  //
  // Readiness trim (2026-09-15): same idea as `_override` below — a one-day
  // adjustment must never rewrite the recurring day template. `_trim_applied`
  // stores only a substitute `variants` array, date+day-stamped; this is the
  // single place that swaps it in, so every reader (durations, set counts,
  // the timer) sees the trimmed numbers without WK.plan[day] itself changing.
  function appliedTrimToday() {
    const a = WK.plan && WK.plan._trim_applied;
    return (a && a.date === todayISO()) ? a : null;
  }
  function variantsOf(dayKey) {
    const d = WK.plan && WK.plan[dayKey];
    const applied = appliedTrimToday();
    if (applied && applied.day_key === dayKey && dayKey === todayKey()) return applied.variants || [];
    return (d && d.variants) || [];
  }
  function pickOf(dayKey) {
    const vs = variantsOf(dayKey); if (!vs.length) return 0;
    const p = (WK.plan[dayKey] && WK.plan[dayKey].pick) || 0;
    return (p >= 0 && p < vs.length) ? p : 0;
  }
  function activeVariant(dayKey) { return variantsOf(dayKey)[pickOf(dayKey)] || null; }
  function dayType(dayKey) { const d = WK.plan && WK.plan[dayKey]; return d ? (d.type || d.title || '') : ''; }
  function dayExercises(dayKey) { const v = activeVariant(dayKey); return (v && v.exercises) || []; }
  function isCardioDay(dayKey) { const d = WK.plan && WK.plan[dayKey]; return !!(d && d.cardio); }
  // Target string for one run set: "145–150 bpm" / "≥ 186 bpm" / "≤ 130 bpm".
  function hrTargetLabel(s) {
    if (!s) return null;
    if (s.hrLo && s.hrHi) return s.hrLo + '–' + s.hrHi + ' bpm';
    if (s.hrLo) return '≥ ' + s.hrLo + ' bpm';
    if (s.hrHi) return '≤ ' + s.hrHi + ' bpm';
    return null;
  }
  // 5-zone colour ramp keyed to the midpoint of an HR target (HRmax ≈ 195).
  const HR_ZONE_COLORS = ['oklch(0.72 0.05 250)', 'oklch(0.72 0.13 200)',
    'oklch(0.74 0.14 150)', 'oklch(0.78 0.15 90)', 'oklch(0.68 0.20 30)'];
  function hrZoneColor(lo, hi) {
    // With one open bound, bias the midpoint toward the realistic range: "≤ 130"
    // trends lower, "≥ 186" trends higher.
    const mid = (lo && hi) ? (lo + hi) / 2 : lo ? lo * 1.06 : hi ? hi * 0.86 : 130;
    const f = mid / 195;
    const i = f < 0.6 ? 0 : f < 0.7 ? 1 : f < 0.8 ? 2 : f < 0.9 ? 3 : 4;
    return HR_ZONE_COLORS[i];
  }
  function setVariant(dayKey, idx) {
    const d = WK.plan && WK.plan[dayKey];
    if (!d || !d.variants || !d.variants[idx] || d.pick === idx) return;
    d.pick = idx;
    render(); savePlanDebounced();
  }
  function planFor(dayKey) {
    const k = sourceDay(dayKey);
    const v = activeVariant(k);
    const multi = variantsOf(k).length > 1;
    return {
      title: dayType(k) + (multi && v && v.name ? ' · ' + v.name : ''),
      exercises: (v && v.exercises) || [],
    };
  }
  function setDayOverride(from) {
    if (!WK.plan) return;
    if (!from || from === todayKey()) delete WK.plan._override;
    else WK.plan._override = { date: todayISO(), from: from };
    WK.activeDay = todayKey();
    WK.ui.swapOpen = false;
    render(); savePlanDebounced();
  }

  // ── plan mutations ───────────────────────────────────────────────────────
  // Edits go to the *source* day, so progress logged today lands on the session
  // that was actually performed rather than on the slot it was borrowed into.
  function curExs() { return planFor(WK.activeDay).exercises || []; }
  function setActiveDay(day) { WK.activeDay = day; WK.ui.swapOpen = false; render(); }
  function updateSet(ei, si, field, raw) {
    const v = raw === '' || raw == null ? null : (field === 'reps' || field === 'rpe' ? parseInt(raw, 10) : parseFloat(raw));
    const st = curExs()[ei].sets[si];
    st[field] = (v != null && Number.isNaN(v)) ? null : v;
    render(); savePlanDebounced();
  }
  // Guided work phase: same mutation but patch inputs in place (no video restart).
  function updateCurSet(field, raw) {
    const t = WK.training;
    const v = raw === '' || raw == null ? null : (field === 'reps' ? parseInt(raw, 10) : parseFloat(raw));
    const st = curExs()[t.exIndex].sets[t.setIndex];
    st[field] = (v != null && Number.isNaN(v)) ? null : v;
    savePlanDebounced(); renderWorkVals();
  }
  function toggleDone(ei, si) {
    const e = curExs()[ei]; const st = e.sets[si];
    st.done = !st.done;
    if (st.done) {
      if (isTime(e)) { if (st.actualSec == null) st.actualSec = st.durationSec; }
      else if (st.reps == null) st.reps = st.targetReps;
    }
    render(); savePlanDebounced();
  }
  // Day-view duration edit: before a set is done it sets the target; after, the actual.
  function updateDur(ei, si, raw) {
    const st = curExs()[ei].sets[si];
    const v = parseDur(raw); if (v == null) { render(); return; }
    if (st.done) st.actualSec = v; else st.durationSec = v;
    render(); savePlanDebounced();
  }
  // Flip an exercise between reps and time mode, converting its sets in place.
  function toggleExMode(ei) {
    const e = curExs()[ei];
    if (isTime(e)) {
      delete e.mode;
      e.sets = e.sets.map(s => ({ targetReps: s.targetReps || 10, weight: s.weight || 0, reps: null, rpe: s.rpe != null ? s.rpe : null, done: false }));
    } else {
      e.mode = 'time';
      e.sets = e.sets.map(s => ({ durationSec: s.durationSec || 45, actualSec: null, rpe: s.rpe != null ? s.rpe : null, done: false }));
    }
    render(); savePlanDebounced();
  }
  function swapExercise(ei, name) {
    const e = curExs()[ei]; e.name = name; e.muscle = muscleOf(name);
    render(); savePlanDebounced();
  }

  // ── swap scope: "just today" vs "from now on" ─────────────────────────────
  // Picking a new exercise for TODAY's session asks which one you mean. A "just
  // today" swap renames the exercise in place and remembers the original on the
  // exercise itself (`revert`: date + name + muscle + mode + set targets); once
  // the date has moved on, maybeDayRevert() puts it all back. Deliberately NOT a
  // copy of the whole day (that's how the readiness trim works): with a copy,
  // every weight you edit today would go to a throwaway and your progression on
  // the other exercises would be lost. Only this one exercise is affected.
  function isTrimmedDay(k) {
    const d = WK.plan && WK.plan[k];
    return !!d && variantsOf(k) !== d.variants;   // a readiness trim already makes every edit today-only
  }
  function requestSwap(ei, name) {
    const e = curExs()[ei];
    if (!e || !name || name === e.name) { render(); return; }
    // The question only makes sense for today's session. Another weekday is a plan
    // edit (as it always was), and under a trim the edit is today-only anyway.
    if (WK.activeDay !== todayKey() || isTrimmedDay(sourceDay(WK.activeDay))) { swapExercise(ei, name); return; }
    if (WK.ui.pendingSwap) return;
    WK.ui.pendingSwap = { ei: ei, from: e.name, to: name };
    renderSwapPop();
    // Cancel / backdrop / Escape / back gesture: forget it and rebuild the card so
    // the dropdown goes back to showing the real exercise.
    BackStack.push(() => { WK.ui.pendingSwap = null; render(); });
  }
  function applySwap(ei, name, scope) {
    const e = curExs()[ei]; if (!e) return;
    if (scope === 'today') {
      if (!e.revert) e.revert = { date: todayISO(), name: e.name, muscle: e.muscle, mode: e.mode || null, sets: clone(e.sets) };
    } else {
      delete e.revert;   // "from now on": the new exercise IS the plan now
    }
    swapExercise(ei, name);
  }
  // Restore every "just today" swap whose day has passed. Runs on load/show, before
  // maybeWeekReset (which clears progress), never mid-session. Returns true if it changed anything.
  function maybeDayRevert() {
    if (!WK.plan || WK.training.active) return false;
    const today = todayISO(); let changed = false;
    DAY_KEYS.forEach(k => {
      const d = WK.plan[k]; if (!d || !d.variants) return;
      d.variants.forEach(v => (v.exercises || []).forEach(e => {
        const r = e.revert;
        if (!r || r.date === today) return;
        e.name = r.name; e.muscle = r.muscle;
        if (r.mode) e.mode = r.mode; else delete e.mode;
        e.sets = r.sets;
        delete e.revert; changed = true;
      }));
    });
    return changed;
  }
  function renderSwapPop() {
    const el = document.getElementById('wk-pop-swap');
    if (!el) return;
    const p = WK.ui.pendingSwap;
    if (!p) { el.classList.add('hidden'); el.innerHTML = ''; return; }
    if (!el.innerHTML) {
      el.innerHTML = '<div class="wk-pop-card" data-act="noop" role="alertdialog" aria-label="Change exercise">' +
        '<div style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);">Change exercise</div>' +
        '<div class="wk-pop-name" style="font-size:17px;margin-top:8px;">' + esc(p.from) + '</div>' +
        '<div style="font-size:13px;color:var(--ink-3);margin:4px 0;">changes to</div>' +
        '<div class="wk-pop-name" style="font-size:17px;color:var(--accent);">' + esc(p.to) + '</div>' +
        '<div style="font-size:12px;color:var(--ink-3);margin-top:12px;line-height:1.5;">Your sets and weights stay as they are.</div>' +
        '<div style="display:flex;flex-direction:column;gap:8px;margin-top:16px;">' +
        '<button class="wk-btn wk-btn-primary" data-act="swap-scope" data-scope="today" style="padding:13px;font-size:13px;">Just today</button>' +
        '<button class="wk-btn wk-btn-ghost" data-act="swap-scope" data-scope="always" style="padding:13px;font-size:13px;">From now on</button>' +
        '<button class="wk-btn wk-btn-ghost" data-act="close-swap" style="padding:11px;font-size:12px;border-color:transparent;color:var(--ink-3);">Cancel</button></div></div>';
    }
    el.classList.remove('hidden');
  }
  function addSet(ei) {
    const e = curExs()[ei];
    if (isTime(e)) {
      const last = e.sets[e.sets.length - 1] || { durationSec: 45 };
      e.sets.push({ durationSec: last.durationSec || 45, actualSec: null, rpe: null, done: false });
    } else {
      const last = e.sets[e.sets.length - 1] || { targetReps: 10, weight: 20 };
      e.sets.push({ targetReps: last.targetReps, weight: last.weight, reps: null, rpe: null, done: false });
    }
    render(); savePlanDebounced();
  }
  function removeSet(ei, si) {
    const e = curExs()[ei]; if (e.sets.length > 1) e.sets.splice(si, 1);
    render(); savePlanDebounced();
  }
  function addExercise() {
    curExs().push({ id: uid(), name: 'Bench Press', muscle: 'Chest', sets: [{ targetReps: 10, weight: 40, reps: null, rpe: null, done: false }] });
    render(); savePlanDebounced();
  }
  function removeExercise(ei) { curExs().splice(ei, 1); render(); savePlanDebounced(); }
  // Move the exercise at index `from` so it ends up at index `to` (the ▲/▼ buttons
  // and drag-and-drop both land here). The array is the day's recurring template,
  // so the new order persists like any other plan edit. Returns the moved card.
  function reorderExercise(from, to) {
    const exs = curExs();
    if (from === to || !exs[from] || !exs[to]) return null;
    // A paused guided session addresses exercises by index: keep its cursor on the
    // exercise it was on, or the saved setIndex would land on a different one.
    const t = WK.training;
    const cursorEx = t.day === WK.activeDay ? exs[t.exIndex] : null;
    exs.splice(to, 0, exs.splice(from, 1)[0]);
    if (cursorEx) { t.exIndex = exs.indexOf(cursorEx); saveActiveLS(); }
    render(); savePlanDebounced();
    // render() rebuilt the DOM: flash the moved card and keep it in view.
    const card = document.querySelector('#wk-exercises .wk-excard[data-ei="' + to + '"]');
    if (card) { card.classList.add('moved'); card.scrollIntoView({ block: 'nearest' }); }
    return card;
  }
  // ▲/▼: swap with the neighbour (dir −1 = up, +1 = down).
  function moveExercise(ei, dir) {
    const card = reorderExercise(ei, ei + dir);
    // Re-focus the same arrow so repeated keyboard presses keep moving the same exercise.
    const btn = card && card.querySelector('[data-act="move-ex"][data-dir="' + dir + '"]:not(:disabled)');
    if (btn) btn.focus({ preventScroll: true });
  }

  // ── drag-and-drop reorder (mouse; touch uses the ▲/▼ buttons) ─────────────
  // Only the grip is draggable — making the whole card draggable would break text
  // selection inside the weight/reps inputs. DRAG_TYPE marks *our* drags so a file
  // or text dragged over the tab is ignored. State lives in module vars, not in
  // dataTransfer (unreadable during dragover), and is cleared on drop *and*
  // dragend: the drop re-renders, detaching the source, so dragend may never reach us.
  const DRAG_TYPE = 'application/x-wk-exercise';
  let dragEi = null;     // index of the exercise being dragged
  let dropIns = null;    // insertion slot 0..n (before card k), null = drop would change nothing
  function clearDropMarks() {
    document.querySelectorAll('#wk-exercises .wk-excard.dragging, #wk-exercises .wk-excard.drop-before, #wk-exercises .wk-excard.drop-after')
      .forEach(el => el.classList.remove('dragging', 'drop-before', 'drop-after'));
  }
  let dragY = 0;         // last pointer Y (viewport) during the drag
  let dragOverList = false;   // pointer is over a valid drop area
  let scrollTimer = null;
  function stopAutoScroll() { clearInterval(scrollTimer); scrollTimer = null; }
  function endDrag() { dragEi = null; dropIns = null; dragOverList = false; stopAutoScroll(); clearDropMarks(); }
  // Page-scroll speed (px per 30 ms tick, negative = up) for a pointer at Y. The
  // 110 px zones clear the sticky top bar / floating bottom nav.
  function edgeSpeed(y) {
    const edge = 110, h = window.innerHeight;
    if (y < edge) return -Math.ceil((edge - Math.max(y, 0)) / edge * 22);
    if (y > h - edge) return Math.ceil((Math.min(y, h) - (h - edge)) / edge * 22);
    return 0;
  }
  function updateDropMark() {
    if (!dragOverList) { setDropSlot(null); return; }
    const ins = dropSlotAt(dragY);
    setDropSlot(ins === dragEi || ins === dragEi + 1 ? null : ins);   // both mean "stay where you are"
  }
  function isExDrag(e) {
    return dragEi != null && e.dataTransfer && Array.from(e.dataTransfer.types || []).indexOf(DRAG_TYPE) !== -1;
  }
  // Which slot the pointer is in: before the first card whose midpoint is below it.
  function dropSlotAt(clientY) {
    const cards = document.querySelectorAll('#wk-exercises .wk-excard');
    for (let i = 0; i < cards.length; i++) {
      const r = cards[i].getBoundingClientRect();
      if (clientY < r.top + r.height / 2) return i;
    }
    return cards.length;
  }
  function setDropSlot(ins) {
    dropIns = ins;
    document.querySelectorAll('#wk-exercises .wk-excard.drop-before, #wk-exercises .wk-excard.drop-after')
      .forEach(el => el.classList.remove('drop-before', 'drop-after'));
    if (ins == null) return;
    const cards = document.querySelectorAll('#wk-exercises .wk-excard');
    if (ins < cards.length) cards[ins].classList.add('drop-before');
    else if (cards.length) cards[cards.length - 1].classList.add('drop-after');
  }
  function onDragStart(e) {
    const grip = e.target.closest && e.target.closest('.wk-grip');
    const card = grip && grip.closest('.wk-excard');
    if (!card) return;
    dragEi = +card.dataset.ei; dropIns = null;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData(DRAG_TYPE, String(dragEi));   // Firefox won't start a drag without data
    // A compact name chip as the drag image — the whole card would be ~300 px tall.
    const sel = card.querySelector('.wk-exsel');
    const ghost = document.createElement('div');
    ghost.className = 'wk-dragghost';
    ghost.setAttribute('data-accent', 'workout');
    ghost.textContent = sel && sel.selectedOptions[0] ? sel.selectedOptions[0].text : 'Exercise';
    document.body.appendChild(ghost);
    e.dataTransfer.setDragImage(ghost, 14, 16);
    // After the browser has snapshotted the drag image: drop the ghost, dim the source.
    setTimeout(() => { ghost.remove(); card.classList.add('dragging'); }, 0);
  }
  function onDragOver(e) {
    if (!isExDrag(e)) return;
    dragY = e.clientY;
    // Decided by pointer position, not e.target: the fixed top bar / floating bottom
    // nav sit over the scroll zones and aren't part of this screen, so target-based
    // checks would lose the drag exactly where the user needs to auto-scroll.
    const main = document.querySelector('#screen-workout .wk-main');
    const r = main && main.getBoundingClientRect();
    dragOverList = !!r && e.clientX >= r.left && e.clientX <= r.right &&
      e.clientY >= r.top - 60 && e.clientY <= r.bottom + 60;
    if (dragOverList) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }   // not preventDefault'd elsewhere → drop cancels
    updateDropMark();
    // The list is taller than the screen: scroll while the pointer sits near an
    // edge. A timer, not the dragover event — browsers only repeat that every
    // 50–350 ms when the pointer is still — and it refreshes the marker as cards
    // slide under the pointer.
    if (edgeSpeed(dragY) && !scrollTimer) {
      scrollTimer = setInterval(() => {
        const s = edgeSpeed(dragY);
        if (!s) { stopAutoScroll(); return; }
        window.scrollBy(0, s); updateDropMark();
      }, 30);
    }
  }
  function onDrop(e) {
    if (!isExDrag(e)) return;
    e.preventDefault();
    const from = dragEi, ins = dropIns;
    endDrag();
    if (from == null || ins == null) return;
    reorderExercise(from, ins > from ? ins - 1 : ins);   // slot numbers count the dragged card itself
  }

  // ── timer ────────────────────────────────────────────────────────────────
  function toggleTimer() {
    const t = WK.timer;
    if (t.running) { t.accumSec = elapsedSec(); t.running = false; t.startEpoch = null; }
    else { t.startEpoch = Date.now(); t.running = true; }
    saveActiveLS(); render();
  }
  // Full reset of the session on screen (↺, after the confirm). It used to clear only
  // the stopwatch, leaving a paused guided session ("Resume training", old cursor,
  // notes/pain) and every ticked set behind. Same per-set rule as the weekly reset:
  // progress goes, weights/targets stay. Saved history sessions are never touched.
  function resetTraining() {
    releaseWakeLock();
    curExs().forEach(e => e.sets.forEach(s => { s.done = false; s.reps = null; s.rpe = null; s.actualSec = null; }));
    WK.timer = { running: false, startEpoch: null, accumSec: 0 };
    WK.training = { active: false, paused: false, day: null, phase: 'work', exIndex: 0, setIndex: 0, restEndEpoch: null, sessionStartEpoch: null };
    clearActiveLS();
    savePlanDebounced();
    render();
  }

  // ── guided training ──────────────────────────────────────────────────────
  function nextStep(ei, si) {
    const exs = curExs();
    if (si + 1 < exs[ei].sets.length) return [ei, si + 1];
    for (let j = ei + 1; j < exs.length; j++) { if (exs[j].sets.length > 0) return [j, 0]; }
    return null;
  }
  function startTraining() {
    ensureAudio();
    const dayKey = WK.activeDay; const exs = curExs();
    if (!exs.length) return;
    const t = WK.training;
    const resumable = t.paused && t.day === dayKey && (t.phase === 'done' || (exs[t.exIndex] && exs[t.exIndex].sets[t.setIndex]));
    if (resumable) {
      t.active = true; t.paused = false;
      if (isCardioDay(sourceDay(dayKey))) acquireWakeLock();
      if (!WK.timer.running) toggleTimer(); else { saveActiveLS(); }
      if (t.phase === 'rest' && restLeft() <= 0) finishRest();
      else if (t.phase === 'work') armTimedSet();   // restart the hold fresh on resume
      render(); return;
    }
    let ei = 0; while (ei < exs.length && exs[ei].sets.length === 0) ei++;
    if (ei >= exs.length) return;
    WK.training = { active: true, paused: false, day: dayKey, phase: 'work', exIndex: ei, setIndex: 0, restEndEpoch: null, timeEndEpoch: null, timeRemainMs: null, sessionStartEpoch: Date.now(), notes: '', pain: [] };
    armTimedSet();
    if (isCardioDay(sourceDay(dayKey))) acquireWakeLock();
    if (!WK.timer.running) toggleTimer(); else saveActiveLS();
    render();
  }
  function stepDone() {
    const t = WK.training; const ex = curExs()[t.exIndex]; const st = ex.sets[t.setIndex];
    st.done = true;
    if (isTime(ex)) {
      const left = Math.max(0, timeLeft());          // full duration if the timer ran out
      st.actualSec = Math.max(0, Math.round((st.durationSec || 0) - left));
      t.timeEndEpoch = null; t.timeRemainMs = null;
    } else if (st.reps == null) { st.reps = st.targetReps; }
    savePlanDebounced();
    const next = nextStep(t.exIndex, t.setIndex);
    if (!next) { t.phase = 'done'; saveActiveLS(); render(); }
    else { startRest(); }
  }
  // ── timed-set countdown (guided work phase for time-mode exercises) ────────
  function timeLeft() {
    const t = WK.training;
    if (t.timeRemainMs != null) return Math.ceil(t.timeRemainMs / 1000);
    if (t.timeEndEpoch) return Math.max(0, Math.ceil((t.timeEndEpoch - Date.now()) / 1000));
    return 0;
  }
  function armTimedSet() {
    const t = WK.training; const ex = curExs()[t.exIndex];
    if (t.phase === 'work' && isTime(ex) && ex.sets[t.setIndex]) {
      t.timeEndEpoch = Date.now() + (ex.sets[t.setIndex].durationSec || 0) * 1000;
      t.timeRemainMs = null;
    } else { t.timeEndEpoch = null; t.timeRemainMs = null; }
  }
  function toggleTimedPause() {
    const t = WK.training;
    if (t.timeRemainMs != null) { t.timeEndEpoch = Date.now() + t.timeRemainMs; t.timeRemainMs = null; }
    else if (t.timeEndEpoch) { t.timeRemainMs = Math.max(0, t.timeEndEpoch - Date.now()); t.timeEndEpoch = null; }
    saveActiveLS(); render();
  }
  function addTime() {
    const t = WK.training;
    if (t.timeRemainMs != null) t.timeRemainMs += 15000;
    else if (t.timeEndEpoch) t.timeEndEpoch += 15000;
    saveActiveLS(); render();
  }
  // ── settings: rest timers (2026-09-21) ─────────────────────────────────────────────────────────────────
  // Two rests, chosen in the ⚙ Settings popup: between sets (the next step is the same exercise) and between
  // exercises (the next step is a different one). Both default to the old fixed 45 s, so nothing changes until
  // you change them. Kept in localStorage on this device on purpose: a preference must never be able to touch
  // the synced plan, and the phone at the gym is where it matters.
  const WK_LS_SETTINGS = 'acta_workout_settings_v1';
  const REST_MIN = 10, REST_MAX = 600, REST_STEP = 5;
  const REST_PRESETS = [30, 45, 60, 90, 120, 180];
  const REST_ROWS = [
    { key: 'restSetSec', label: 'Rest between sets', hint: 'After a set, before the next set of the same exercise' },
    { key: 'restExSec',  label: 'Rest between exercises', hint: 'After the last set, before the next exercise' },
  ];
  function loadWkSettings() {
    const s = { restSetSec: RESTBASE, restExSec: RESTBASE };
    try {
      const j = JSON.parse(localStorage.getItem(WK_LS_SETTINGS) || 'null');
      if (j) Object.keys(s).forEach(k => { const v = Math.round(Number(j[k])); if (v >= REST_MIN && v <= REST_MAX) s[k] = v; });
    } catch (e) { }
    return s;
  }
  WK.settings = loadWkSettings();
  function saveWkSettings() { try { localStorage.setItem(WK_LS_SETTINGS, JSON.stringify(WK.settings)); } catch (e) { } }
  const fmtRest = sec => sec >= 60 ? Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0') : sec + ' s';
  // The rest that follows the set just finished. `next` is nextStep() of that set.
  function restSecFor(next) {
    return (next && next[0] !== WK.training.exIndex) ? WK.settings.restExSec : WK.settings.restSetSec;
  }
  // Length of the rest in progress (the ring's full circle); older saved sessions carry none, so the old 45 s.
  const restTotal = () => WK.training.restTotalSec || RESTBASE;
  function startRest() {
    const t = WK.training;
    const sec = restSecFor(nextStep(t.exIndex, t.setIndex));
    t.phase = 'rest';
    t.restTotalSec = sec;
    t.restEndEpoch = Date.now() + sec * 1000;
    saveActiveLS(); render();
  }
  function addRest() {
    const t = WK.training;
    if (t.restEndEpoch) { t.restEndEpoch += 15000; t.restTotalSec = restTotal() + 15; saveActiveLS(); render(); }
  }

  function openSettings() {
    if (WK.ui.settingsOpen) return;
    WK.ui.settingsOpen = true;
    renderSettingsPop();
    BackStack.push(() => { WK.ui.settingsOpen = false; renderSettingsPop(); });
    const done = document.querySelector('#wk-pop-settings .wk-btn-primary');
    if (done) done.focus();
  }
  function settingsRowHtml(r) {
    const v = WK.settings[r.key];
    return '<div class="wk-set-row" data-key="' + r.key + '">' +
      '<div class="wk-set-lbl"><span>' + r.label + '</span><small>' + r.hint + '</small></div>' +
      '<div class="wk-set-ctl">' +
        '<button class="wk-tbtn wk-set-step" data-act="rest-step" data-key="' + r.key + '" data-delta="-' + REST_STEP + '" aria-label="' + r.label + ': ' + REST_STEP + ' seconds shorter">−</button>' +
        '<output class="wk-set-val wk-mono" aria-live="polite">' + fmtRest(v) + '</output>' +
        '<button class="wk-tbtn wk-set-step" data-act="rest-step" data-key="' + r.key + '" data-delta="' + REST_STEP + '" aria-label="' + r.label + ': ' + REST_STEP + ' seconds longer">+</button>' +
      '</div>' +
      '<div class="wk-variants wk-set-presets">' + REST_PRESETS.map(p =>
        '<button class="wk-vchip' + (p === v ? ' on' : '') + '" data-act="rest-set" data-key="' + r.key + '" data-val="' + p + '">' + fmtRest(p) + '</button>').join('') + '</div>' +
    '</div>';
  }
  function renderSettingsPop() {
    const el = document.getElementById('wk-pop-settings');
    if (!el) return;
    if (!WK.ui.settingsOpen) { el.classList.add('hidden'); el.innerHTML = ''; return; }
    if (!el.innerHTML) {
      el.innerHTML = '<div class="wk-pop-card wk-set-card" data-act="noop" role="dialog" aria-label="Workout settings">' +
        '<div class="wk-pop-name" style="font-size:17px;">Workout settings</div>' +
        '<div class="wk-set-sub">Rest timer</div>' +
        REST_ROWS.map(settingsRowHtml).join('') +
        '<div class="wk-set-note">Applies from the next rest. Saved on this device.</div>' +
        '<button class="wk-btn wk-btn-primary" data-act="close-settings" style="width:100%;margin-top:14px;padding:13px;font-size:13.5px;">Done</button></div>';
    }
    el.classList.remove('hidden');
  }
  // change one value: clamp, save, and update that row in place (a full rebuild would drop the tap's focus)
  function setRest(key, sec) {
    if (!(key in WK.settings)) return;
    const v = Math.max(REST_MIN, Math.min(REST_MAX, Math.round(Number(sec))));
    if (!(v >= REST_MIN)) return;
    WK.settings[key] = v; saveWkSettings();
    const row = document.querySelector('#wk-pop-settings .wk-set-row[data-key="' + key + '"]');
    if (!row) return;
    row.querySelector('.wk-set-val').textContent = fmtRest(v);
    row.querySelectorAll('.wk-vchip').forEach(b => b.classList.toggle('on', +b.dataset.val === v));
  }
  function skipRest() { finishRest(); }
  function finishRest() {
    const t = WK.training;
    const next = nextStep(t.exIndex, t.setIndex);
    t.restEndEpoch = null;
    if (!next) { t.phase = 'done'; }
    else { t.phase = 'work'; t.exIndex = next[0]; t.setIndex = next[1]; armTimedSet(); }
    saveActiveLS(); render();
  }
  function adjustWeight(delta) {
    const t = WK.training; const st = curExs()[t.exIndex].sets[t.setIndex];
    let w = (st.weight || 0) + delta; if (w < 0) w = 0;
    st.weight = Math.round(w * 100) / 100; savePlanDebounced(); renderWorkVals();
  }
  function adjustReps(delta) {
    const t = WK.training; const st = curExs()[t.exIndex].sets[t.setIndex];
    const base = st.reps == null ? st.targetReps : st.reps;
    let r = base + delta; if (r < 0) r = 0; st.reps = r; savePlanDebounced(); renderWorkVals();
  }
  // Update the two stepper inputs in place. A full render() would rebuild the
  // overlay's innerHTML and recreate the <video>, restarting the demo loop on
  // every +/- tap — so the work phase patches values instead of re-rendering.
  function renderWorkVals() {
    const t = WK.training;
    if (!t.active || t.phase !== 'work') { render(); return; }
    const ex = curExs()[t.exIndex]; if (!ex) { render(); return; }
    const st = ex.sets[t.setIndex]; if (!st) { render(); return; }
    const w = document.querySelector('#wk-ov-training [data-act="cur-weight"]');
    const r = document.querySelector('#wk-ov-training [data-act="cur-reps"]');
    if (w) w.value = st.weight == null ? '' : st.weight;
    if (r) r.value = st.reps == null ? '' : st.reps;
  }
  function exitTraining() {
    WK.training.active = false; WK.training.paused = true;
    releaseWakeLock();
    if (WK.timer.running) toggleTimer(); else { saveActiveLS(); render(); }
  }
  function buildSession() {
    const dayKey = WK.training.day || WK.activeDay;
    const day = planFor(dayKey);
    const exs = day.exercises || [];
    const allSets = exs.reduce((a, e) => a.concat(e.sets), []);
    const setsPlanned = allSets.length;
    const setsDone = allSets.filter(s => s.done).length;
    const volume = exs.reduce((a, e) => a + exVolDone(e), 0);
    const doneRpe = allSets.filter(s => s.done && s.rpe != null).map(s => s.rpe);
    const avgRpe = doneRpe.length ? doneRpe.reduce((a, b) => a + b, 0) / doneRpe.length : null;
    const startEpoch = WK.training.sessionStartEpoch || Date.now();
    return {
      id: 's' + startEpoch, dateISO: new Date(startEpoch).toISOString(), dayKey,
      title: day.title || 'Workout', durationSec: Math.round(elapsedSec()),
      setsDone, setsPlanned, volume, avgRpe,
      notes: (WK.training.notes || '').trim() || null,
      pain: (WK.training.pain || []).slice(),
      exercises: exs.map(e => {
        const done = e.sets.filter(s => s.done);
        const top = e.sets.reduce((m, s) => Math.max(m, s.weight || 0), 0);
        const dur = e.sets.reduce((a, s) => a + (s.done ? setDurVal(s) : 0), 0);
        return { name: e.name, muscle: e.muscle, mode: e.mode || 'reps', setsDone: done.length, sets: e.sets.length, top, dur };
      }).filter(e => e.setsDone > 0),
    };
  }
  function finishTraining() {
    const sess = buildSession();
    releaseWakeLock();
    if (WK.timer.running) toggleTimer();
    if (sess.setsDone > 0) { WK.sessions.unshift(sess); apiPostSession(sess); }
    WK.training = { active: false, paused: false, day: null, phase: 'work', exIndex: 0, setIndex: 0, restEndEpoch: null, sessionStartEpoch: null };
    WK.timer = { running: false, startEpoch: null, accumSec: 0 };
    clearActiveLS(); render();
  }

  // ── derived data for stats/sidebar ───────────────────────────────────────
  function dayAgg(exs) {
    const allSets = exs.reduce((a, e) => a.concat(e.sets), []);
    const setsPlanned = allSets.length;
    const setsDone = allSets.filter(s => s.done).length;
    const volDone = exs.reduce((a, e) => a + exVolDone(e), 0);
    const volPlan = exs.reduce((a, e) => a + e.sets.reduce((b, s) => b + planVol(e, s), 0), 0);
    const doneRpe = allSets.filter(s => s.done && s.rpe != null).map(s => s.rpe);
    const avgRpe = doneRpe.length ? doneRpe.reduce((a, b) => a + b, 0) / doneRpe.length : null;
    return { setsPlanned, setsDone, volDone, volPlan, avgRpe, ringPct: setsPlanned ? setsDone / setsPlanned : 0 };
  }
  function computeProgressions() {
    // per exercise: top weight across the last 6 sessions that contained it
    const byName = {};
    const chrono = (WK.sessions || []).slice().sort((a, b) => new Date(a.dateISO) - new Date(b.dateISO));
    chrono.forEach(se => (se.exercises || []).forEach(e => {
      if (e.mode === 'time') return;  // time exercises have no weight to trend
      (byName[e.name] = byName[e.name] || []).push(e.top || 0);
    }));
    return Object.keys(byName).map(name => {
      const vals = byName[name].slice(-6);
      return { name, vals };
    }).filter(p => p.vals.length >= 2).slice(0, 6);
  }
  function computePRs() {
    const prMap = {};
    (WK.sessions || []).forEach(se => (se.exercises || []).forEach(e => {
      if (!prMap[e.name] || (e.top || 0) > prMap[e.name].weight) prMap[e.name] = { name: e.name, muscle: e.muscle, weight: e.top || 0 };
    }));
    DAY_KEYS.forEach(k => variantsOf(k).forEach(v => (v.exercises || []).forEach(e => {
      const w = e.sets.reduce((m, s) => Math.max(m, s.weight || 0), 0);
      if (!prMap[e.name] || w > prMap[e.name].weight) prMap[e.name] = { name: e.name, muscle: e.muscle, weight: w };
    })));
    return Object.values(prMap).filter(p => p.weight > 0).sort((a, b) => b.weight - a.weight).slice(0, 5);
  }

  // ── rendering ────────────────────────────────────────────────────────────
  let skeletonBuilt = false;
  function screen() { return document.getElementById('screen-workout'); }

  function buildSkeleton() {
    if (skeletonBuilt) return;
    const root = screen();
    root.innerHTML =
      '<div class="wk-head">' +
      '  <div class="wk-daybar" id="wk-daybar"></div>' +
      '  <button class="wk-headbtn" data-act="library">Library</button>' +
      '  <button class="wk-headbtn" data-act="history">History</button>' +
      '</div>' +
      '<div class="wk-band">' +
      '  <div class="wk-band-cell" id="wk-readiness" style="display:none;"></div>' +
      '  <div class="wk-band-cell" id="wk-session"></div>' +
      '  <div class="wk-band-cell wk-band-stats" id="wk-stats"></div>' +
      '</div>' +
      '<div class="wk-cols">' +
      '  <div class="wk-main">' +
      '    <div id="wk-exercises"></div>' +
      '  </div>' +
      '  <aside class="wk-side">' +
      '    <div class="panel" id="wk-progression"></div>' +
      '    <div class="panel" id="wk-week"></div>' +
      '    <div class="panel" id="wk-prs"></div>' +
      '  </aside>' +
      '</div>';
    root.addEventListener('click', onClick);
    root.addEventListener('change', onChange);
    root.addEventListener('dragstart', onDragStart);
    root.addEventListener('dragend', endDrag);
    // dragover/drop at document level (see onDragOver); both no-op unless a drag
    // that *we* started is in flight, so they cost nothing on other screens.
    document.addEventListener('dragover', onDragOver);
    document.addEventListener('drop', onDrop);
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && (WK.ui.mediaPop || WK.ui.confirmReset || WK.ui.pendingSwap || WK.ui.settingsOpen)) BackStack.pop(); });

    // overlays live on <body> so the fixed positioning is never clipped
    const ov = document.createElement('div');
    ov.innerHTML =
      '<div class="wk-overlay hidden" id="wk-ov-training"></div>' +
      '<div class="wk-overlay hidden" id="wk-ov-history"></div>' +
      '<div class="wk-overlay hidden" id="wk-ov-library"></div>' +
      '<div class="wk-overlay hidden" id="wk-ov-runreview"></div>' +
      '<div class="wk-pop hidden" id="wk-pop-media" data-act="close-media"></div>' +
      '<div class="wk-pop hidden" id="wk-pop-confirm" data-act="close-confirm"></div>' +
      '<div class="wk-pop hidden" id="wk-pop-swap" data-act="close-swap"></div>' +
      '<div class="wk-pop hidden" id="wk-pop-settings" data-act="close-settings"></div>';
    while (ov.firstChild) {
      const node = ov.firstChild;
      // overlays are on <body>, outside #screen-workout, so they'd inherit the
      // default mint accent — force the workout (orange) accent on each.
      node.setAttribute('data-accent', 'workout');
      document.body.appendChild(node);
      node.addEventListener('click', onClick);
      node.addEventListener('change', onChange);
      node.addEventListener('input', onInput);
    }

    fileInput = document.createElement('input');
    fileInput.type = 'file'; fileInput.accept = 'video/*,image/*'; fileInput.style.display = 'none';
    fileInput.addEventListener('change', onFilePicked);
    document.body.appendChild(fileInput);

    skeletonBuilt = true;
  }

  function rpeColor(rpe) { return rpe == null ? 'var(--ink-2)' : (rpe >= 9 ? 'var(--danger)' : (rpe >= 7 ? 'var(--accent)' : 'var(--warn)')); }

  function renderDaybar() {
    const bar = document.getElementById('wk-daybar');
    bar.innerHTML = DAYS.map(d => {
      const sets = dayExercises(d.key).reduce((a, e) => a.concat(e.sets), []);
      const done = sets.filter(s => s.done).length;
      const cls = 'wk-pill' + (d.key === WK.activeDay ? ' active' : '') + (done > 0 ? ' has-done' : '');
      return '<button class="' + cls + '" data-act="day" data-day="' + d.key + '">' +
        '<span>' + d.key + '</span><span class="wk-dot"></span></button>';
    }).join('');
  }

  function renderSession() {
    const day = planFor(WK.activeDay);
    const exs = day.exercises || [];
    const muscles = []; exs.forEach(e => { if (muscles.indexOf(e.muscle) === -1) muscles.push(e.muscle); });
    const canTrain = exs.length > 0;
    const t = WK.training;
    const canResume = !!t.paused && t.day === WK.activeDay && canTrain;
    const running = WK.timer.running;
    const el = Math.round(elapsedSec());
    const stopLabel = running ? 'Stop' : (el > 0 ? 'Resume' : 'Start');
    const fullName = (DAYS.find(x => x.key === WK.activeDay) || {}).full || '';
    const isToday = WK.activeDay === todayKey();
    const ov = activeOverride();
    const swapped = !!(ov && isToday);
    const srcK = sourceDay(WK.activeDay);

    // Variant picker (List A/B/C · No-equipment). Hidden on single-variant days
    // (cardio, rest). Reads/writes the *source* day so it follows a ⇄ Change.
    let variantUI = '';
    const vs = variantsOf(srcK);
    if (vs.length > 1) {
      const pk = pickOf(srcK);
      variantUI =
        '<div class="wk-variants">' +
        vs.map((v, i) => '<button class="wk-vchip' + (i === pk ? ' on' : '') +
          '" data-act="variant" data-idx="' + i + '">' + esc(v.name) + '</button>').join('') +
        '</div>';
    }

    // Swapping is a today-only action: the override is date-stamped, so offering it
    // while browsing another weekday would silently apply to today instead.
    const swapBtn = isToday
      ? '<button class="wk-swapbtn" data-act="swap-open" title="Do a different session today">⇄ Change</button>'
      : '';

    let swapUI = '';
    if (swapped) {
      swapUI =
        '<div class="wk-swapnote">Swapped for today · normally ' +
        esc(dayType(todayKey()) || todayKey()) +
        ' <button class="wk-swaprevert" data-act="swap-revert">revert</button></div>';
    }
    if (isToday && WK.ui.swapOpen) {
      swapUI +=
        '<div class="wk-swaplist">' +
        DAY_KEYS.map(k => {
          const tt = dayType(k) || k;
          const n = dayExercises(k).length;
          const cur = k === srcK;
          return '<button class="wk-swapopt' + (cur ? ' on' : '') + '" data-act="swap-pick" data-day="' + k + '">' +
            '<span class="wk-swapday">' + k + '</span>' +
            '<span class="wk-swaptitle">' + esc(tt) + '</span>' +
            '<span class="wk-swapn">' + (n ? n + ' ex' : 'rest') + '</span></button>';
        }).join('') +
        '</div>';
    }

    document.getElementById('wk-session').innerHTML =
      '<div class="panel-body" style="padding:16px 14px;">' +
      '<div class="wk-sesshead">' +
      '<div class="wk-sess-day">' + esc(fullName) + '</div>' +
      '<div class="wk-sess-actions">' + swapBtn +
      '<button class="wk-swapbtn wk-setbtn" data-act="open-settings" title="Rest timers and other workout settings">⚙ Settings</button></div></div>' +
      '<div class="wk-session-title">' + esc(dayType(srcK) || 'Workout') + '</div>' +
      '<div class="wk-session-sub"><span class="wk-sub-pre"><span class="dd">' + esc(fullName) + '</span> · </span>' + (muscles.length ? esc(muscles.join(' · ')) : 'Recovery') + '</div>' +
      variantUI +
      swapUI +
      '<div class="wk-startrow">' +
      '<div class="wk-timerrow">' +
      '  <span class="wk-tdot' + (running ? ' on' : '') + '"></span>' +
      '  <span class="wk-tlabel">Session</span>' +
      '  <span class="wk-mono wk-timer" id="wk-timer">' + fmtTime(el) + '</span>' +
      '  <button class="wk-tbtn' + (running ? ' on' : '') + '" data-act="toggle-timer" title="' + stopLabel + '">' +
           (running ? '❚❚' : '▶') + '</button>' +
      '  <button class="wk-tbtn" data-act="reset-training" title="Reset training" aria-label="Reset training">↺</button>' +
      '</div>' +
      '<button class="wk-btn wk-btn-primary wk-startbtn" data-act="start-training"' + (canTrain ? '' : ' disabled') + '>' +
      (canResume ? '▶ Resume training' : (canTrain ? '▶ Start training' : 'Rest day')) + '</button>' +
      '</div>' +
      '</div>';
  }

  function renderStats() {
    const ag = dayAgg(curExs());
    const off = C_RING * (1 - ag.ringPct);
    document.getElementById('wk-stats').innerHTML =
      '<div class="wk-stat wk-stat-ring">' +
      '<svg width="46" height="46" viewBox="0 0 80 80" style="flex:none;">' +
      '<circle cx="40" cy="40" r="34" fill="none" stroke="var(--line-2)" stroke-width="9"></circle>' +
      '<circle cx="40" cy="40" r="34" fill="none" stroke="var(--accent)" stroke-width="9" stroke-linecap="round" stroke-dasharray="' + C_RING.toFixed(1) + '" stroke-dashoffset="' + off.toFixed(1) + '" transform="rotate(-90 40 40)"></circle></svg>' +
      '<div><div class="k">Sets done</div><div class="v wk-sv-lg">' + ag.setsDone + '/' + ag.setsPlanned + '</div>' +
      '<div class="s" style="color:var(--accent);">' + Math.round(ag.ringPct * 100) + '%</div></div></div>' +
      '<div class="wk-stat"><div class="k">Volume</div><div class="v wk-mono wk-sv">' + fmtVol(ag.volDone) + '</div><div class="s">of ' + fmtVol(ag.volPlan) + '</div></div>' +
      '<div class="wk-stat"><div class="k">Avg RPE</div><div class="v wk-mono wk-sv">' + (ag.avgRpe == null ? '—' : (Math.round(ag.avgRpe * 10) / 10).toFixed(1)) + '</div><div class="s">effort</div></div>';
  }

  function hasImage(name) {
    return !!mediaFor(name) || (EX_ALIASES_OF[name] || []).some(a => !!mediaFor(a));
  }
  // `sessionNames`: the day's other exercises — never suggested (already in the session).
  function optionList(selected, sessionNames) {
    let out = '';
    // Custom plan exercises aren't in LIBRARY; without a matching <option> the
    // select would display its first entry ("Bench Press"). Always surface the
    // current name so the day view shows the real exercise.
    if (selected && LIB_FLAT.indexOf(selected) === -1) {
      out += '<optgroup label="Current"><option value="' + esc(selected) + '" selected>' + esc(selected) + '</option></optgroup>';
    }
    // "Suggested": same exact muscle, best first — the answer to "what could I change this to?".
    // Suggested options are never `selected`, so the closed box keeps showing the real exercise.
    const me = EX_META[exCanon(selected)];
    if (me) {
      const sug = similarTo(selected, { exclude: sessionNames || [], hasMedia: hasImage });
      out += '<optgroup label="Suggested · ' + esc(me.t.replace('-', ' ')) + '">' +
        (sug.length
          ? sug.map(s => '<option value="' + esc(s.name) + '">' + esc(s.name) + ' · ' + esc(s.eq) + '</option>').join('')
          : '<option value="" disabled>No close matches</option>') +
        '</optgroup>';
    }
    // Every entry carries its equipment — except the selected one, so the closed
    // box on a phone isn't made longer by a suffix it doesn't need.
    for (const m in LIBRARY) {
      out += '<optgroup label="' + m + '">';
      out += LIBRARY[m].map(n => {
        const eq = eqLabel(n);
        return '<option value="' + esc(n) + '"' + (n === selected ? ' selected' : '') + '>' + esc(n) + (eq && n !== selected ? ' · ' + esc(eq) : '') + '</option>';
      }).join('');
      out += '</optgroup>';
    }
    return out;
  }

  const GRIP = '<svg viewBox="0 0 8 12" width="8" height="12" aria-hidden="true" fill="currentColor"><circle cx="2" cy="2" r="1.1"/><circle cx="6" cy="2" r="1.1"/><circle cx="2" cy="6" r="1.1"/><circle cx="6" cy="6" r="1.1"/><circle cx="2" cy="10" r="1.1"/><circle cx="6" cy="10" r="1.1"/></svg>';
  // Camera button: opens the exercise-image popup (rest screen "Up next" + plan
  // cards). Dimmed, not hidden, when no media is uploaded — the popup then says so.
  const CAMERA = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 8h3l1.6-2.4h6.8L17 8h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13" r="3.4"/></svg>';
  function mediaBtn(ex, ei, from) {
    const has = !!mediaFor(ex.name);
    return '<button class="wk-mediabtn' + (has ? '' : ' none') + '" data-act="show-media" data-ei="' + ei + '" data-from="' + from + '"' +
      ' title="' + (has ? 'Show exercise image' : 'No image yet') + '" aria-label="Show exercise image">' + CAMERA + '</button>';
  }

  const MV_UP = '<svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true"><path d="M1 5l4-4 4 4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const MV_DOWN = '<svg viewBox="0 0 10 6" width="10" height="6" aria-hidden="true"><path d="M1 1l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function renderExercises() {
    const cont = document.getElementById('wk-exercises');
    if (isCardioDay(sourceDay(WK.activeDay))) { renderCardioDay(cont); return; }
    const exs = curExs();
    if (!exs.length) {
      cont.innerHTML = '<div class="panel" style="border-style:dashed;padding:26px 18px;text-align:center;">' +
        '<div style="font-weight:600;font-size:15px;">Rest &amp; recovery</div>' +
        '<div style="font-size:13px;color:var(--ink-3);margin-top:6px;">No session planned. Add exercises if you trained today.</div>' +
        '<button class="wk-headbtn" data-act="add-exercise" style="margin-top:14px;">+ Add exercise</button></div>';
      return;
    }
    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;">' +
      '<div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--ink-2);font-weight:600;">Today\'s exercises</div>' +
      '<button class="wk-headbtn" data-act="add-exercise">+ Add</button></div>';
    html += exs.map((ex, ei) => {
      const time = isTime(ex);
      const timeCols = 'grid-template-columns:18px 1fr 40px 16px;';
      let setsHtml, headHtml, summaryRight;
      if (time) {
        // Time exercise: one duration cell (m:ss) per set instead of weight/reps/RPE.
        setsHtml = ex.sets.map((s, si) =>
          '<div class="wk-setgrid wk-setrow' + (s.done ? ' done' : '') + '" style="' + timeCols + '">' +
          '<div class="wk-setno">' + (si + 1) + '</div>' +
          '<input class="wk-in" type="text" inputmode="numeric" value="' + fmtDur(setDurVal(s)) + '" data-act="set-dur" data-ei="' + ei + '" data-si="' + si + '">' +
          '<button class="wk-done-btn' + (s.done ? ' on' : '') + '" data-act="set-done" data-ei="' + ei + '" data-si="' + si + '">' + (s.done ? '✓' : '') + '</button>' +
          '<button class="wk-x" data-act="rm-set" data-ei="' + ei + '" data-si="' + si + '">✕</button>' +
          '</div>'
        ).join('');
        headHtml = '<div class="wk-setgrid head" style="' + timeCols + '"><div>#</div><div>Duration (m:ss)</div><div style="text-align:center;">✓</div><div></div></div>';
        const exDur = ex.sets.reduce((a, s) => a + (s.durationSec || 0), 0);
        summaryRight = '<span style="color:var(--accent);">Σ ' + fmtDur(exDur) + '</span>';
      } else {
        setsHtml = ex.sets.map((s, si) => {
          const rpeOpts = '<option value="">–</option>' + RPE_OPTIONS.map(r => '<option value="' + r + '"' + (s.rpe === r ? ' selected' : '') + '>' + r + '</option>').join('');
          return '<div class="wk-setgrid wk-setrow' + (s.done ? ' done' : '') + '">' +
            '<div class="wk-setno">' + (si + 1) + '</div>' +
            '<input class="wk-in" type="number" step="' + WEIGHT_STEP + '" value="' + (s.weight == null ? '' : s.weight) + '" placeholder="–" data-act="set-w" data-ei="' + ei + '" data-si="' + si + '">' +
            '<input class="wk-in" type="number" value="' + (s.reps == null ? '' : s.reps) + '" placeholder="' + s.targetReps + '" data-act="set-reps" data-ei="' + ei + '" data-si="' + si + '">' +
            '<select class="wk-rpe" style="color:' + rpeColor(s.rpe) + ';" data-act="set-rpe" data-ei="' + ei + '" data-si="' + si + '">' + rpeOpts + '</select>' +
            '<button class="wk-done-btn' + (s.done ? ' on' : '') + '" data-act="set-done" data-ei="' + ei + '" data-si="' + si + '">' + (s.done ? '✓' : '') + '</button>' +
            '<button class="wk-x" data-act="rm-set" data-ei="' + ei + '" data-si="' + si + '">✕</button>' +
            '</div>';
        }).join('');
        headHtml = '<div class="wk-setgrid head"><div>#</div><div>Weight</div><div>Reps</div><div>RPE</div><div style="text-align:center;">✓</div><div></div></div>';
        const exVol = exVolDone(ex);
        summaryRight = '<span style="color:var(--accent);">' + (exVol > 0 ? fmtVol(exVol) : '—') + '</span>';
      }
      const mv = exs.length < 2 ? '' :
        '<div class="wk-mv">' +
        '<button class="wk-mvbtn" data-act="move-ex" data-ei="' + ei + '" data-dir="-1" title="Swap with the exercise above" aria-label="Move exercise up"' + (ei === 0 ? ' disabled' : '') + '>' + MV_UP + '</button>' +
        '<button class="wk-mvbtn" data-act="move-ex" data-ei="' + ei + '" data-dir="1" title="Swap with the exercise below" aria-label="Move exercise down"' + (ei === exs.length - 1 ? ' disabled' : '') + '>' + MV_DOWN + '</button></div>';
      return '<div class="panel wk-excard" data-ei="' + ei + '" style="margin-top:10px;overflow:visible;">' +
        '<div class="wk-exhead">' + (exs.length < 2 ? '' : '<div class="wk-grip" draggable="true" title="Drag to reorder">' + GRIP + '</div>') + mv +
        '<span class="wk-mtag">' + esc(ex.muscle) + '</span>' + soreChip(ex.muscle) +
        '<select class="wk-exsel" data-act="swap" data-ei="' + ei + '">' + optionList(ex.name, exs.map(e => e.name)) + '</select>' +
        mediaBtn(ex, ei, 'card') +
        '<button class="wk-modebtn' + (time ? ' on' : '') + '" data-act="toggle-mode" data-ei="' + ei + '" title="Switch between reps and time">' + (time ? '⏱ Time' : '⚑ Reps') + '</button>' +
        '<button class="wk-x" data-act="rm-ex" data-ei="' + ei + '">✕</button></div>' +
        '<div style="display:flex;justify-content:space-between;padding:8px 12px 0;font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);">' +
        '<span>' + ex.sets.filter(s => s.done).length + '/' + ex.sets.length + ' sets done' + (eqLabel(ex.name) ? ' · ' + esc(eqLabel(ex.name)) : '') + '</span>' +
        summaryRight + '</div>' +
        headHtml +
        setsHtml +
        '<div style="padding:8px 12px 12px;"><button class="wk-addbtn" data-act="add-set" data-ei="' + ei + '">+ Add set</button></div>' +
        '</div>';
    }).join('');
    cont.innerHTML = html;
  }

  // Cardio day: a run card (segment timeline + prescription list + review),
  // instead of the weight/reps grid.
  function renderCardioDay(cont) {
    const k = sourceDay(WK.activeDay);
    const exs = dayExercises(k);
    const allSets = exs.reduce((a, e) => a.concat(e.sets), []);
    const totalSec = allSets.reduce((a, s) => a + (s.durationSec || 0), 0);
    const doneSec = allSets.reduce((a, s) => a + (s.done ? (s.durationSec || 0) : 0), 0);

    const bars = allSets.map(s => {
      const tip = (s.tag || 'segment') + ' · ' + fmtDur(s.durationSec) +
        (hrTargetLabel(s) ? ' · ' + hrTargetLabel(s) : '');
      return '<div title="' + esc(tip) + '" style="flex:' + Math.max(1, s.durationSec || 1) +
        ';min-width:3px;background:' + hrZoneColor(s.hrLo, s.hrHi) + ';' +
        (s.done ? '' : 'opacity:.8;') + '"></div>';
    }).join('');

    const rows = exs.map(e => {
      const secs = e.sets.reduce((a, s) => a + (s.durationSec || 0), 0);
      const isIv = e.sets.length > 2 && e.sets.some(s => s.tag === 'Hard');
      let detail;
      if (isIv) {
        const hard = e.sets.find(s => s.tag === 'Hard') || {};
        const easy = e.sets.find(s => s.tag === 'Easy') || {};
        const rounds = e.sets.filter(s => s.tag === 'Hard').length;
        detail = rounds + ' × (' + fmtDur(hard.durationSec) + ' ' + (hrTargetLabel(hard) || 'hard') +
          '  /  ' + fmtDur(easy.durationSec) + ' ' + (hrTargetLabel(easy) || 'jog') + ')';
      } else {
        const s = e.sets[0] || {};
        detail = fmtDur(s.durationSec) + (hrTargetLabel(s) ? '  ·  ' + hrTargetLabel(s) : '');
      }
      const done = e.sets.filter(s => s.done).length;
      const s0 = e.sets[0] || {};
      return '<div class="wk-runrow">' +
        '<span class="wk-runzone" style="background:' + hrZoneColor(s0.hrLo, s0.hrHi) + ';"></span>' +
        '<div style="flex:1;min-width:0;">' +
        '<div class="wk-runname">' + esc(e.name) + '</div>' +
        '<div class="wk-rundetail">' + esc(detail) + '</div></div>' +
        '<span class="wk-runsecs">' + fmtDur(secs) + (done ? ' · ' + done + '/' + e.sets.length : '') + '</span>' +
        '</div>';
    }).join('');

    cont.innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;">' +
      '<div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--ink-2);font-weight:600;">Run plan</div>' +
      '<span class="wk-mono" style="font-size:12px;color:var(--ink-3);">' + fmtDur(totalSec) + ' total' +
      (doneSec ? ' · ' + fmtDur(doneSec) + ' done' : '') + '</span></div>' +
      '<div class="wk-runtl">' + bars + '</div>' +
      '<div class="wk-runlist">' + rows + '</div>' +
      '<button class="wk-btn wk-btn-ghost" data-act="open-runreview" style="width:100%;margin-top:14px;padding:11px;font-size:12px;">▸ Run review</button>' +
      '<div style="font-family:var(--mono);font-size:12px;color:var(--ink-3);text-align:center;margin-top:8px;letter-spacing:0.04em;">no GPS on the strap — distance &amp; pace are step estimates</div>';
  }

  function spark(vals, w, h) {
    if (!vals.length) return '';
    const mx = Math.max.apply(null, vals), mn = Math.min.apply(null, vals);
    const P = 4, span = (mx - mn) || 1, n = vals.length;
    const pts = vals.map((v, i) => {
      const x = P + (n > 1 ? i / (n - 1) : 0.5) * (w - 2 * P);
      const y = h - P - ((v - mn) / span) * (h - 2 * P);
      return x.toFixed(1) + ',' + y.toFixed(1);
    });
    const last = pts[pts.length - 1].split(',');
    const up = vals[vals.length - 1] >= vals[0];
    const col = up ? 'var(--accent)' : 'var(--danger)';
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h + '" preserveAspectRatio="none" class="wk-spark">' +
      '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + col + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"></polyline>' +
      '<path d="M' + last[0] + ' ' + last[1] + 'h0.01" fill="none" stroke="' + col + '" stroke-width="5.2" stroke-linecap="round" vector-effect="non-scaling-stroke"></path></svg>';
  }

  function renderSidebar() {
    // progression
    const prog = computeProgressions();
    let ph = '<div class="panel-head"><div class="label">Load progression</div><div class="meta">last 6</div></div><div class="panel-body">';
    if (!prog.length) ph += '<div class="wk-empty">No sessions yet — finish a workout to see trends.</div>';
    else ph += prog.map(p => {
      const dl = p.vals[p.vals.length - 1] - p.vals[0]; const up = dl >= 0;
      return '<div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-top:1px solid var(--line);">' +
        '<div style="flex:1;min-width:0;"><div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + esc(p.name) + '</div>' +
        '<div style="display:flex;align-items:baseline;gap:7px;margin-top:3px;"><span class="wk-mono" style="font-size:14px;font-weight:600;">' + fmtKg(p.vals[p.vals.length - 1]) + ' kg</span>' +
        '<span class="wk-mono" style="font-size:12px;font-weight:600;color:' + (up ? 'var(--accent)' : 'var(--danger)') + ';">' + (up ? '+' : '') + fmtKg(dl) + '</span></div></div>' +
        spark(p.vals, 104, 30) + '</div>';
    }).join('');
    ph += '</div>';
    document.getElementById('wk-progression').innerHTML = ph;

    // this week bars
    let weeklyVol = 0;
    const bars = DAYS.map(d => {
      const dexs = dayExercises(d.key);
      const sets = dexs.reduce((a, e) => a.concat(e.sets), []);
      const done = sets.filter(s => s.done).length;
      weeklyVol += dexs.reduce((a, e) => a + exVolDone(e), 0);
      const pct = sets.length ? done / sets.length : 0;
      const hh = pct === 0 ? 5 : Math.max(7, Math.round(pct * 66));
      const bg = pct === 0 ? 'var(--line-2)' : (d.key === WK.activeDay ? 'var(--accent)' : 'var(--accent-soft)');
      return '<div class="col"><div class="bar" style="height:' + hh + 'px;background:' + bg + ';"></div><div class="lbl">' + d.key[0] + '</div></div>';
    }).join('');
    document.getElementById('wk-week').innerHTML =
      '<div class="panel-head"><div class="label">This week</div></div><div class="panel-body">' +
      '<div class="wk-weekbars">' + bars + '</div>' +
      '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:12px;padding-top:11px;border-top:1px solid var(--line);">' +
      '<span style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);">Weekly volume</span>' +
      '<span class="wk-mono" style="font-size:15px;font-weight:600;color:var(--accent);">' + fmtVol(weeklyVol) + '</span></div></div>';

    // PRs
    const prs = computePRs();
    let rh = '<div class="panel-head"><div class="label">Personal records</div></div><div class="panel-body">';
    if (!prs.length) rh += '<div class="wk-empty">No records yet.</div>';
    else rh += prs.map(p => '<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-top:1px solid var(--line);">' +
      '<div><div style="font-size:13px;font-weight:600;">' + esc(p.name) + '</div>' +
      '<div style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);margin-top:1px;">' + esc(p.muscle) + '</div></div>' +
      '<div class="wk-mono" style="font-size:15px;font-weight:600;color:var(--accent-2);">' + fmtKg(p.weight) + ' kg</div></div>').join('');
    rh += '</div>';
    document.getElementById('wk-prs').innerHTML = rh;
  }

  // ── guided overlay ─────────────────────────────────────────────────────────
  function renderTraining() {
    const ov = document.getElementById('wk-ov-training');
    const t = WK.training;
    if (!t.active) { ov.classList.add('hidden'); return; }
    ov.classList.remove('hidden');
    const exs = curExs();
    const steps = []; exs.forEach((e, ei) => e.sets.forEach((s, si) => steps.push([ei, si])));
    const doneSteps = steps.filter(p => exs[p[0]].sets[p[1]].done).length;
    const totalSteps = steps.length;
    const barPct = totalSteps ? Math.round(doneSteps / totalSteps * 100) : 0;
    const el = Math.round(elapsedSec());

    let body = '';
    if (t.phase === 'work') {
      const ex = exs[t.exIndex]; const st = ex.sets[t.setIndex];
      const vid = mediaFor(ex.name);
      const time = isTime(ex);
      const dots = ex.sets.map((s, i) => {
        const style = s.done ? 'width:9px;height:9px;background:var(--accent);'
          : (i === t.setIndex ? 'width:11px;height:11px;background:transparent;border:2px solid var(--accent);' : 'width:9px;height:9px;background:var(--line-2);');
        return '<span style="border-radius:50%;display:inline-block;' + style + '"></span>';
      }).join('');
      const mediaHtml = vid
        ? mediaTag(vid, 'width:100%;height:100%;object-fit:cover;border-radius:14px;')
        : '<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--ink-3);font-family:var(--mono);font-size:12px;">' + esc(ex.muscle) + '</div>';
      const head =
        '<div style="width:100%;max-width:240px;height:170px;margin:0 auto 10px;background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;">' + mediaHtml + '</div>' +
        '<div style="display:inline-block;font-family:var(--sans);font-size:12px;font-weight:700;letter-spacing:0.01em;color:#0b0705;background:var(--accent);padding:3px 9px;border-radius:5px;">' + esc(ex.muscle) + '</div>' +
        '<div style="font-family:var(--num);font-size:26px;font-weight:600;margin-top:12px;letter-spacing:-0.015em;line-height:1.1;">' + esc(ex.name) + '</div>';
      let mid;
      if (time) {
        const dur = st.durationSec || 0;
        const left = timeLeft();
        const paused = t.timeRemainMs != null;
        const pct = dur ? Math.min(1, Math.max(0, left / dur)) : 0;
        const tgt = hrTargetLabel(st);
        const tagLine = (st.tag || tgt)
          ? '<div style="margin-top:10px;">' +
              (st.tag ? '<span style="display:inline-block;font-family:var(--sans);font-size:12px;font-weight:700;letter-spacing:0.01em;color:' +
                (st.tag === 'Hard' ? 'var(--danger)' : 'var(--accent)') + ';">' + esc(st.tag) + '</span>' : '') +
              (tgt ? '<span style="font-family:var(--mono);font-size:13px;color:var(--ink-2);margin-left:' + (st.tag ? '8px' : '0') + ';">🎯 ' + esc(tgt) + '</span>' : '') +
            '</div>'
          : '';
        mid =
          '<div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);margin-top:8px;letter-spacing:0.01em;">Set ' + (t.setIndex + 1) + ' of ' + ex.sets.length + ' · hold ' + fmtDur(dur) + '</div>' +
          tagLine +
          '<div style="display:flex;gap:7px;justify-content:center;margin-top:14px;">' + dots + '</div>' +
          '<div style="position:relative;width:200px;height:200px;margin:16px auto 8px;">' +
          '<svg width="200" height="200" viewBox="0 0 240 240"><circle cx="120" cy="120" r="104" fill="none" stroke="var(--panel-2)" stroke-width="12"></circle>' +
          '<circle id="wk-work-arc" cx="120" cy="120" r="104" fill="none" stroke="var(--accent)" stroke-width="12" stroke-linecap="round" stroke-dasharray="' + C_REST.toFixed(1) + '" stroke-dashoffset="' + (C_REST * (1 - pct)).toFixed(1) + '" transform="rotate(-90 120 120)"></circle></svg>' +
          '<div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;">' +
          '<div class="wk-mono" id="wk-work-num" style="font-size:46px;font-weight:600;line-height:1;font-variant-numeric:tabular-nums;">' + fmtDur(left) + '</div>' +
          '<div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0.01em;margin-top:4px;">' + (paused ? 'paused' : 'remaining') + '</div></div></div>' +
          '<div style="display:flex;gap:10px;justify-content:center;margin-top:6px;">' +
          '<button class="wk-btn wk-btn-ghost" data-act="time-pause" style="padding:11px 20px;font-size:12px;">' + (paused ? '▶ Resume' : '⏸ Pause') + '</button>' +
          '<button class="wk-btn wk-btn-ghost" data-act="time-add" style="padding:11px 20px;font-size:12px;">+15s</button></div>' +
          '<button class="wk-btn wk-btn-primary" data-act="step-done" style="margin-top:14px;width:100%;padding:17px;font-size:16px;">Finish set</button>';
      } else {
        mid =
          '<div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);margin-top:8px;letter-spacing:0.01em;">Set ' + (t.setIndex + 1) + ' of ' + ex.sets.length + ' · target ' + st.targetReps + ' reps</div>' +
          '<div style="display:flex;gap:7px;justify-content:center;margin-top:14px;">' + dots + '</div>' +
          '<div style="display:flex;gap:12px;justify-content:center;align-items:flex-end;margin-top:22px;">' +
          '<div style="display:flex;flex-direction:column;align-items:center;gap:7px;"><span style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);">Weight kg</span>' +
          '<div class="wk-stepper"><button data-act="w-minus">−</button>' +
          '<input type="number" value="' + (st.weight == null ? '' : st.weight) + '" data-act="cur-weight">' +
          '<button class="plus" data-act="w-plus">+</button></div></div>' +
          '<div style="display:flex;flex-direction:column;align-items:center;gap:7px;"><span style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);">Reps</span>' +
          '<div class="wk-stepper"><button data-act="r-minus">−</button>' +
          '<input type="number" value="' + (st.reps == null ? '' : st.reps) + '" placeholder="' + st.targetReps + '" data-act="cur-reps" style="width:52px;">' +
          '<button class="plus" data-act="r-plus">+</button></div></div></div>' +
          '<button class="wk-btn wk-btn-primary" data-act="step-done" style="margin-top:26px;width:100%;padding:17px;font-size:16px;">Done · Next</button>';
      }
      body = '<div style="width:100%;max-width:380px;text-align:center;">' + head + mid + '</div>';
    } else if (t.phase === 'rest') {
      const rl = restLeft();
      const restPct = Math.min(1, Math.max(0, rl / restTotal()));
      const ex = exs[t.exIndex]; const st = ex.sets[t.setIndex];
      const next = nextStep(t.exIndex, t.setIndex);
      const nextName = next ? exs[next[0]].name : 'Final set done';
      const nextSet = next ? ('Set ' + (next[1] + 1) + ' of ' + exs[next[0]].sets.length) : 'Workout complete';
      const rpeBtns = RPE_OPTIONS.map(v => {
        const sel = st.rpe === v; const c = v >= 9 ? 'var(--danger)' : (v >= 7 ? 'var(--accent)' : 'var(--warn)');
        return '<button data-act="rpe" data-rpe="' + v + '" style="width:30px;height:38px;border-radius:8px;cursor:pointer;font-family:var(--mono);font-size:14px;font-weight:600;' +
          (sel ? ('background:' + c + ';color:#0b0705;border:1px solid ' + c + ';') : 'background:var(--panel-2);color:var(--ink-2);border:1px solid var(--line);') + '">' + v + '</button>';
      }).join('');
      body =
        '<div style="text-align:center;width:100%;max-width:380px;">' +
        '<div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--accent);font-weight:700;">Rest</div>' +
        '<div style="position:relative;width:220px;height:220px;margin:14px auto;">' +
        '<svg width="220" height="220" viewBox="0 0 240 240"><circle cx="120" cy="120" r="104" fill="none" stroke="var(--panel-2)" stroke-width="12"></circle>' +
        '<circle id="wk-rest-arc" cx="120" cy="120" r="104" fill="none" stroke="var(--accent)" stroke-width="12" stroke-linecap="round" stroke-dasharray="' + C_REST.toFixed(1) + '" stroke-dashoffset="' + (C_REST * (1 - restPct)).toFixed(1) + '" transform="rotate(-90 120 120)"></circle></svg>' +
        '<div style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;">' +
        '<div class="wk-mono" id="wk-rest-num" style="font-size:58px;font-weight:600;line-height:1;font-variant-numeric:tabular-nums;">' + rl + '</div>' +
        '<div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0.01em;margin-top:4px;">seconds</div></div></div>' +
        '<div class="panel" style="padding:14px;margin-bottom:18px;"><div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0.01em;">Rate last set · <span style="color:var(--accent);">' + esc(ex.name) + '</span></div>' +
        '<div style="display:flex;gap:5px;justify-content:center;margin-top:11px;flex-wrap:wrap;">' + rpeBtns + '</div></div>' +
        '<div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0.01em;">Up next</div>' +
        '<div style="display:flex;align-items:center;justify-content:center;gap:10px;margin-top:6px;">' +
        '<div style="font-family:var(--num);font-size:20px;font-weight:600;">' + esc(nextName) + '</div>' +
        (next ? mediaBtn(exs[next[0]], next[0], 'rest') : '') + '</div>' +
        '<div style="font-size:13px;color:var(--ink-3);margin-top:3px;">' + esc(nextSet) + '</div>' +
        '<div style="display:flex;gap:10px;justify-content:center;margin-top:20px;">' +
        '<button class="wk-btn wk-btn-ghost" data-act="add-rest" style="padding:12px 20px;font-size:12px;">+15s</button>' +
        '<button class="wk-btn wk-btn-primary" data-act="skip-rest" style="padding:12px 24px;font-size:12px;">Skip rest</button></div></div>';
    } else { // done
      const sess = buildSession();
      // The final set has no rest phase after it, so its RPE can't be rated on
      // the rest screen like every other set — offer the same rating here.
      const lastEx = exs[t.exIndex]; const lastSt = lastEx && lastEx.sets[t.setIndex];
      let ratePanel = '';
      if (lastSt) {
        const rpeBtns = RPE_OPTIONS.map(v => {
          const sel = lastSt.rpe === v; const c = v >= 9 ? 'var(--danger)' : (v >= 7 ? 'var(--accent)' : 'var(--warn)');
          return '<button data-act="rpe" data-rpe="' + v + '" style="width:30px;height:38px;border-radius:8px;cursor:pointer;font-family:var(--mono);font-size:14px;font-weight:600;' +
            (sel ? ('background:' + c + ';color:#0b0705;border:1px solid ' + c + ';') : 'background:var(--panel-2);color:var(--ink-2);border:1px solid var(--line);') + '">' + v + '</button>';
        }).join('');
        ratePanel =
          '<div class="panel" style="padding:14px;margin-top:22px;"><div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0.01em;">Rate last set · <span style="color:var(--accent);">' + esc(lastEx.name) + '</span></div>' +
          '<div style="display:flex;gap:5px;justify-content:center;margin-top:11px;flex-wrap:wrap;">' + rpeBtns + '</div></div>';
      }
      // Pain/niggle flags — tapped areas feed the readiness card & digest as a
      // transparent rule (not a fitted score input).
      const pain = t.pain || [];
      const painChips = PAIN_AREAS.map(([slug, name]) => {
        const on = pain.indexOf(slug) >= 0;
        return '<button data-act="pain-toggle" data-area="' + slug + '" style="padding:7px 12px;border-radius:16px;cursor:pointer;font-size:12px;font-family:var(--mono);' +
          (on ? 'background:var(--danger);color:#0b0705;border:1px solid var(--danger);' : 'background:var(--panel-2);color:var(--ink-2);border:1px solid var(--line);') + '">' + esc(name) + '</button>';
      }).join('');
      const notesPanel =
        '<div class="panel" style="padding:14px;margin-top:14px;text-align:left;">' +
        '<div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0.01em;">Any pain or niggle?</div>' +
        '<div style="display:flex;gap:6px;justify-content:flex-start;margin-top:11px;flex-wrap:wrap;">' + painChips + '</div>' +
        '<div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0.01em;margin-top:16px;">Notes</div>' +
        '<textarea data-act="wk-notes" rows="2" placeholder="e.g. left shoulder twinged on presses; felt strong otherwise" ' +
        'style="width:100%;margin-top:8px;background:var(--panel-2);border:1px solid var(--line);border-radius:8px;color:var(--ink);padding:10px;font-family:inherit;font-size:13px;resize:vertical;box-sizing:border-box;">' + esc(t.notes || '') + '</textarea></div>';
      body =
        '<div style="text-align:center;width:100%;max-width:380px;">' +
        '<div style="width:72px;height:72px;border-radius:50%;background:var(--accent);color:#0b0705;display:flex;align-items:center;justify-content:center;font-size:36px;font-weight:700;margin:0 auto;font-family:var(--mono);">✓</div>' +
        '<div style="font-family:var(--num);font-size:26px;font-weight:600;margin-top:18px;">Workout complete</div>' +
        '<div style="font-size:14px;color:var(--ink-3);margin-top:6px;">' + esc(sess.title) + '</div>' +
        '<div style="display:flex;gap:10px;justify-content:center;margin-top:22px;">' +
        '<div class="panel" style="flex:1;padding:14px;"><div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--ink-3);">Duration</div><div class="wk-mono" style="font-size:18px;font-weight:600;margin-top:6px;">' + fmtTime(el) + '</div></div>' +
        '<div class="panel" style="flex:1;padding:14px;"><div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--ink-3);">Sets</div><div class="wk-mono" style="font-size:18px;font-weight:600;margin-top:6px;">' + sess.setsDone + '/' + sess.setsPlanned + '</div></div>' +
        '<div class="panel" style="flex:1;padding:14px;"><div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--ink-3);">Volume</div><div class="wk-mono" style="font-size:15px;font-weight:600;margin-top:6px;">' + fmtVol(sess.volume) + '</div></div></div>' +
        ratePanel + notesPanel +
        '<button class="wk-btn wk-btn-primary" data-act="finish-training" style="margin-top:24px;width:100%;padding:16px;font-size:15px;">Finish &amp; save</button></div>';
    }

    ov.innerHTML =
      '<div class="wk-ov-head">' +
      '<div style="display:flex;align-items:center;gap:8px;min-width:0;"><span class="wk-tdot' + (WK.timer.running ? ' on' : '') + '"></span>' +
      '<span class="wk-mono" id="wk-tr-timer" style="font-size:14px;font-weight:600;">' + fmtTime(el) + '</span>' +
      '<span style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0.01em;">' + doneSteps + ' / ' + totalSteps + ' sets</span></div>' +
      '<button class="wk-btn wk-btn-ghost" data-act="exit-training" style="padding:7px 12px;font-size:12px;">✕</button></div>' +
      '<div style="height:4px;background:var(--panel-2);flex:none;"><div style="height:100%;background:var(--accent);transition:width .3s;width:' + barPct + '%;"></div></div>' +
      '<div class="wk-ov-body" style="display:flex;align-items:center;justify-content:center;padding:22px 18px;">' + body + '</div>';
  }

  // ── exercise-image popup ───────────────────────────────────────────────────
  // A small centred card over whatever screen is up (guided rest screen or the
  // plan cards). It lives on <body> and is rebuilt ONLY when its target changes:
  // render() runs constantly, and rebuilding innerHTML would restart the <video>
  // loop every time (same trap as the guided work screen). State is a snapshot
  // {name, muscle, from} so it survives plan edits while open.
  let popKey = null;
  function openMedia(ei, from) {
    const ex = curExs()[ei];
    if (!ex || WK.ui.mediaPop) return;
    WK.ui.mediaPop = { name: ex.name, muscle: ex.muscle, from: from };
    renderMediaPop();
    BackStack.push(() => { WK.ui.mediaPop = null; renderMediaPop(); });
  }
  function renderMediaPop() {
    const el = document.getElementById('wk-pop-media');
    if (!el) return;
    // A peek opened from the rest screen ends with the rest: the work screen that
    // follows shows the same exercise's image inline, and the popup would cover Done.
    const p0 = WK.ui.mediaPop;
    if (p0 && p0.from === 'rest' && !(WK.training.active && WK.training.phase === 'rest')) {
      WK.ui.mediaPop = null;
      BackStack.dismiss();   // closed itself: keep history in sync without re-running onClose
    }
    const p = WK.ui.mediaPop;
    if (!p) { if (popKey !== null) { popKey = null; el.innerHTML = ''; } el.classList.add('hidden'); return; }
    const vid = mediaFor(p.name);
    const key = p.name + '|' + (vid || '');
    if (key !== popKey) {
      popKey = key;
      const media = vid
        ? mediaTag(vid, 'display:block;width:100%;max-height:min(56vh,420px);object-fit:contain;border-radius:10px;background:var(--panel-2);')
        : '<div class="wk-pop-none">No image yet for this exercise.<br>Add one from the Library.</div>';
      el.innerHTML = '<div class="wk-pop-card" data-act="noop">' +
        '<div class="wk-pop-head"><span class="wk-mtag">' + esc(p.muscle) + '</span>' +
        '<span class="wk-pop-name">' + esc(p.name) + '</span>' +
        '<button class="wk-x" data-act="close-media" aria-label="Close" style="font-size:15px;padding:6px;">✕</button></div>' +
        media + '</div>';
    }
    el.classList.remove('hidden');
  }

  // ── reset confirmation ─────────────────────────────────────────────────────
  // Same centred-card popup as the exercise image (.wk-pop), closed through
  // BackStack like every other layer. "No" is focused on open so a stray Enter
  // cancels rather than wipes.
  function openConfirmReset() {
    if (WK.ui.confirmReset) return;
    WK.ui.confirmReset = true;
    renderConfirmPop();
    BackStack.push(() => { WK.ui.confirmReset = false; renderConfirmPop(); });
    const no = document.querySelector('#wk-pop-confirm [data-act="close-confirm"].wk-btn');
    if (no) no.focus();
  }
  function renderConfirmPop() {
    const el = document.getElementById('wk-pop-confirm');
    if (!el) return;
    if (!WK.ui.confirmReset) { el.classList.add('hidden'); el.innerHTML = ''; return; }
    if (!el.innerHTML) {
      el.innerHTML = '<div class="wk-pop-card" data-act="noop" role="alertdialog" aria-label="Reset training">' +
        '<div class="wk-pop-name" style="font-size:17px;">Reset the training?</div>' +
        '<div style="font-size:13.5px;color:var(--ink-2);margin-top:8px;line-height:1.5;">Are you sure you want to reset the training? You can\'t undo this.</div>' +
        '<div style="font-size:12px;color:var(--ink-3);margin-top:8px;line-height:1.5;">Clears the timer, the paused session and every set marked done. Weights are kept.</div>' +
        '<div style="display:flex;gap:10px;margin-top:18px;">' +
        '<button class="wk-btn wk-btn-ghost" data-act="close-confirm" style="flex:1;padding:13px;font-size:13px;">No</button>' +
        '<button class="wk-btn wk-btn-warn" data-act="confirm-reset" style="flex:1;padding:13px;font-size:13px;">Yes</button></div></div>';
    }
    el.classList.remove('hidden');
  }

  // ── history overlay ────────────────────────────────────────────────────────
  function renderHistory() {
    const ov = document.getElementById('wk-ov-history');
    if (!WK.ui.historyOpen) { ov.classList.add('hidden'); return; }
    ov.classList.remove('hidden');
    const sessions = (WK.sessions || []).slice().sort((a, b) => new Date(b.dateISO) - new Date(a.dateISO));
    const totalVol = sessions.reduce((a, s) => a + (s.volume || 0), 0);
    const sel = WK.ui.selectedWeek;
    const filtered = sel ? sessions.filter(s => mondayKeyOf(s.dateISO) === sel) : sessions;

    let calHtml = '';
    if (WK.ui.calOpen) {
      const cal = buildCalendar();
      const weekdays = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su'];
      calHtml = '<div class="panel" style="padding:14px;margin-bottom:14px;">' +
        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">' +
        '<button class="wk-btn wk-btn-ghost" data-act="cal-prev" style="width:32px;height:32px;padding:0;font-size:16px;">‹</button>' +
        '<div style="font-weight:600;">' + cal.monthLabel + '</div>' +
        '<button class="wk-btn wk-btn-ghost" data-act="cal-next" style="width:32px;height:32px;padding:0;font-size:16px;">›</button></div>' +
        '<div style="display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin-bottom:5px;">' +
        weekdays.map(w => '<div style="text-align:center;font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);">' + w + '</div>').join('') + '</div>' +
        '<div style="display:flex;flex-direction:column;gap:3px;">' +
        cal.weeks.map(wk => {
          const rs = 'display:grid;grid-template-columns:repeat(7,1fr);gap:3px;border-radius:8px;padding:3px;cursor:pointer;' + (sel === wk.mondayKey ? 'background:var(--accent-soft);box-shadow:inset 0 0 0 1px var(--accent);' : '');
          const cells = wk.cells.map(c => {
            const numC = !c.inMonth ? 'var(--ink-4)' : (c.has ? 'var(--ink)' : 'var(--ink-3)');
            const mark = c.has ? '<div style="width:20px;height:20px;border-radius:50%;background:' + (c.inMonth ? 'var(--accent)' : 'var(--ink-4)') + ';color:#0b0705;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:12px;font-weight:700;">✓</div>' : '';
            return '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;height:42px;border-radius:8px;border:1px solid ' + (c.isToday ? 'var(--accent)' : 'transparent') + ';">' +
              '<div style="font-family:var(--mono);font-size:12px;font-weight:600;color:' + numC + ';">' + c.num + '</div>' + mark + '</div>';
          }).join('');
          return '<div data-act="select-week" data-week="' + wk.mondayKey + '" style="' + rs + '">' + cells + '</div>';
        }).join('') + '</div>' +
        '<div style="font-family:var(--sans);font-weight:500;font-size:12px;color:var(--ink-3);letter-spacing:0.01em;margin-top:10px;text-align:center;">Tap a week to filter · ✓ = workout day</div></div>';
    }

    let selBanner = '';
    if (sel) selBanner = '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--accent-soft);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:14px;">' +
      '<span style="font-size:12px;font-weight:600;">Week of ' + weekRangeLabel(sel) + ' · ' + filtered.length + ' workouts</span>' +
      '<button class="wk-btn wk-btn-ghost" data-act="show-all" style="padding:5px 10px;font-size:12px;border:none;color:var(--accent);">Show all</button></div>';

    let list = '', listItems = [];
    if (!filtered.length) list = '<div class="panel" style="border-style:dashed;padding:28px 20px;text-align:center;"><div style="font-weight:600;">No workouts</div><div style="font-size:13px;color:var(--ink-3);margin-top:6px;">Nothing logged for this selection.</div></div>';
    else listItems = filtered.map(se => {
      const dd = new Date(se.dateISO);
      const wdn = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][dd.getDay()];
      const mon = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][dd.getMonth()];
      const dsec = se.durationSec || 0;
      const durLabel = dsec >= 3600 ? (Math.floor(dsec / 3600) + 'h ' + pad(Math.floor((dsec % 3600) / 60)) + 'm') : (Math.round(dsec / 60) + ' min');
      const seDate = dd.getFullYear() + '-' + pad(dd.getMonth() + 1) + '-' + pad(dd.getDate());
      const isRun = /run|jog/i.test(se.title || '') || (se.exercises || []).some(e => e.muscle === 'Cardio');
      const reviewBtn = isRun
        ? '<button class="wk-btn wk-btn-ghost" data-act="open-run" data-date="' + seDate + '" style="padding:5px 9px;font-size:12px;">▸ review</button>'
        : '';
      const exRows = (se.exercises || []).map(e => '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line);">' +
        '<span style="font-family:var(--sans);font-size:12px;font-weight:700;letter-spacing:0.01em;color:var(--accent);background:var(--panel-2);border:1px solid var(--line);padding:3px 6px;border-radius:5px;flex:none;width:58px;text-align:center;">' + esc(e.muscle) + '</span>' +
        '<span style="flex:1;min-width:0;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + esc(e.name) + '</span>' +
        '<span class="wk-mono" style="font-size:12px;color:var(--ink-3);flex:none;">' + (e.mode === 'time' ? fmtDur(e.dur || 0) : (e.top > 0 ? fmtKg(e.top) + ' kg' : 'Bodyweight')) + ' · ' + e.setsDone + ' sets</span></div>').join('');
      const _card = '<div class="panel" style="padding:16px;">' +
        '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;">' +
        '<div style="min-width:0;"><div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--accent);font-weight:600;">' + wdn + ' · ' + dd.getDate() + ' ' + mon + '</div>' +
        '<div style="font-family:var(--num);font-size:17px;font-weight:600;margin-top:3px;line-height:1.15;">' + esc(se.title) + '</div></div>' +
        '<div style="display:flex;align-items:center;gap:8px;flex:none;">' + reviewBtn +
        '<span class="wk-mono" style="font-size:12px;color:var(--ink-3);">' + durLabel + '</span>' +
        '<button class="wk-x" data-act="del-session" data-sid="' + esc(se.id) + '">✕</button></div></div>' +
        '<div style="display:flex;gap:8px;margin-top:12px;">' +
        '<div class="panel wk-stat" style="padding:8px;"><div class="k">Volume</div><div class="v wk-mono" style="font-size:13px;">' + fmtVol(se.volume || 0) + '</div></div>' +
        '<div class="panel wk-stat" style="padding:8px;"><div class="k">Sets</div><div class="v wk-mono" style="font-size:13px;">' + (se.setsDone || 0) + '/' + (se.setsPlanned || 0) + '</div></div>' +
        '<div class="panel wk-stat" style="padding:8px;"><div class="k">Avg RPE</div><div class="v wk-mono" style="font-size:13px;">' + (se.avgRpe == null ? '—' : (Math.round(se.avgRpe * 10) / 10).toFixed(1)) + '</div></div></div>' +
        (exRows ? '<div style="margin-top:6px;">' + exRows + '</div>' : '') + '</div>';
      return { key: String(se.dateISO), html: _card };
    });

    // One chronological stream: detected activity interleaved with guided
    // sessions by date. Days with nothing in them simply do not appear.
    const showDismissed = !!WK.ui.showDismissed;
    const allActs = WK.activities || [];
    const nDismissed = allActs.filter(a => a._state === 'dismissed').length;
    const actsVisible = allActs.filter(a => showDismissed || a._state !== 'dismissed');
    const actFiltered = sel ? actsVisible.filter(a => mondayKeyOf(a.date) === sel) : actsVisible;
    const STATE_LBL = { pending: 'unconfirmed', confirmed: '', dismissed: 'dismissed' };

    const actItems = actFiltered.map(a => {
      const dim = a._state === 'dismissed' ? 'opacity:0.45;' : '';
      // Effort and cooldown separately: a run plus its stretching is one session,
      // but only the effort should read as the workload.
      const split = (a.work_min != null && a.cooldown_min > 0)
        ? a.work_min + 'min +' + a.cooldown_min + ' cooldown' : a.duration_min + 'min';
      const kc = a.kcal != null
        ? '<span style="font-family:var(--mono);font-size:12px;color:var(--accent);">' + a.kcal + ' kcal</span>'
          + (a.cooldown_kcal ? '<span style="font-family:var(--mono);font-size:12px;color:var(--ink-3);">+' + a.cooldown_kcal + '</span>' : '') : '';
      const st = STATE_LBL[a._state]
        ? '<span style="font-family:var(--mono);font-size:12px;color:var(--ink-3);">' + STATE_LBL[a._state] + '</span>' : '';
      const name = a.label_display || a.label || 'activity';
      const isRun = a.label === 'run' || (a.kind === 'cardio' && !a.label);
      const runBtn = (isRun && a._state === 'confirmed')
        ? '<button class="wk-btn wk-btn-ghost" data-act="open-run" data-date="' + esc(a.date) + '" style="padding:4px 8px;font-size:12px;flex:none;">\u25b8 review</button>' : '';
      return { key: a.date + 'T' + String(a.start_min).padStart(4, '0'),
        html: '<div class="panel" style="padding:10px 12px;display:flex;align-items:center;gap:10px;' + dim + '">' +
          '<span style="font-family:var(--mono);font-size:12px;color:var(--ink-3);min-width:76px;">' + a.date + ' ' + (a.start_clock || '') + '</span>' +
          '<span style="flex:1;font-size:12.5px;">' + name + ' \u00b7 ' + split + '</span>' + st + kc + runBtn + '</div>' };
    });

    const merged = actItems.concat(listItems)
      .sort((x, y) => String(y.key).localeCompare(String(x.key)))
      .map(i => i.html).join('');
    const listHtml = merged || list ||
      '<div class="panel" style="border-style:dashed;padding:28px 20px;text-align:center;">' +
      '<div style="font-weight:600;">Nothing here</div></div>';
    const dismissBtn = nDismissed
      ? '<button class="wk-btn wk-btn-ghost" data-act="toggle-dismissed" style="padding:6px 12px;font-size:12px;margin-bottom:12px;">' +
        (showDismissed ? 'Hide' : 'Show') + ' dismissed (' + nDismissed + ')</button>' : '';

    ov.innerHTML =
      '<div class="wk-ov-head"><div style="min-width:0;"><div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--accent);font-weight:600;">Workout history</div>' +
      '<div style="font-family:var(--num);font-size:18px;font-weight:600;">' + sessions.length + ' sessions · ' + fmtVol(totalVol) + '</div></div>' +
      '<div style="display:flex;gap:8px;flex:none;"><button class="wk-btn wk-btn-ghost" data-act="toggle-cal" style="padding:8px 12px;font-size:13px;">🗓</button>' +
      '<button class="wk-btn wk-btn-ghost" data-act="close-history" style="padding:8px 14px;font-size:12px;">✕ Close</button></div></div>' +
      '<div class="wk-ov-body" style="padding:16px;"><div style="max-width:640px;margin:0 auto;">' + calHtml + selBanner +
      dismissBtn +
      '<div style="display:flex;flex-direction:column;gap:14px;">' + listHtml + '</div></div></div>';
  }

  // ── library overlay ────────────────────────────────────────────────────────
  function renderLibrary() {
    const ov = document.getElementById('wk-ov-library');
    if (!WK.ui.libraryOpen) { ov.classList.add('hidden'); return; }
    ov.classList.remove('hidden');
    const groups = Object.keys(LIBRARY).map(m => {
      const items = LIBRARY[m].map(n => {
        const sl = slug(n); const vid = mediaFor(n);
        const thumb = vid ? mediaTag(vid) : '↑ Add media';
        return '<div class="wk-imgcard"><div class="thumb" data-act="upload" data-slug="' + sl + '">' + thumb + '</div>' +
          '<div style="padding:9px 10px;font-size:12px;font-weight:600;">' + esc(n) + '</div></div>';
      }).join('');
      return '<div><div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;"><span class="wk-mtag">' + m + '</span><div style="flex:1;height:1px;background:var(--line);"></div></div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">' + items + '</div></div>';
    }).join('');
    ov.innerHTML =
      '<div class="wk-ov-head"><div><div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--accent);font-weight:600;">Exercise library</div>' +
      '<div style="font-family:var(--num);font-size:18px;font-weight:600;">All exercises</div></div>' +
      '<button class="wk-btn wk-btn-ghost" data-act="close-library" style="padding:8px 14px;font-size:12px;">✕ Close</button></div>' +
      '<div class="wk-ov-body" style="padding:18px 16px 32px;"><div style="max-width:720px;margin:0 auto;display:flex;flex-direction:column;gap:24px;">' + groups + '</div></div>';
  }

  // ── run review overlay (Tier 2) ──────────────────────────────────────────
  async function apiRunReview(date) {
    try {
      const r = await fetch('/api/workout/run-review?date=' + encodeURIComponent(date));
      return r.ok ? r.json() : { available: false, date: date };
    } catch (e) { return { available: false, date: date }; }
  }
  function openRunReview(date) {
    WK.ui.runReviewDate = date;
    WK._runReview = WK._runReview || {};
    render();
    if (!WK._runReview[date]) {
      apiRunReview(date).then(res => { WK._runReview[date] = res; renderRunReview(); });
    }
    BackStack.push(() => { WK.ui.runReviewDate = null; render(); });
  }
  function fmtPace(s) {
    if (!s) return '—';
    const m = Math.floor(s / 60), ss = Math.round(s % 60);
    return m + ':' + String(ss).padStart(2, '0');
  }
  function tile(k, v, sub) {
    return '<div class="panel wk-stat" style="padding:11px 10px;">' +
      '<div class="k">' + esc(k) + '</div>' +
      '<div class="v wk-mono" style="font-size:16px;">' + v + '</div>' +
      (sub ? '<div class="s">' + esc(sub) + '</div>' : '') + '</div>';
  }

  function renderRunReview() {
    const ov = document.getElementById('wk-ov-runreview');
    const date = WK.ui.runReviewDate;
    if (!date) { ov.classList.add('hidden'); return; }
    ov.classList.remove('hidden');
    const d = (WK._runReview || {})[date];

    const head =
      '<div class="wk-ov-head"><div style="min-width:0;">' +
      '<div style="font-family:var(--sans);font-size:12px;letter-spacing:0.01em;color:var(--accent);font-weight:600;">Run review</div>' +
      '<div style="font-family:var(--num);font-size:18px;font-weight:600;">' + esc(date) + '</div></div>' +
      '<button class="wk-btn wk-btn-ghost" data-act="close-runreview" style="padding:8px 14px;font-size:12px;">✕ Close</button></div>';

    if (!d) {
      ov.innerHTML = head + '<div class="wk-ov-body" style="padding:44px 18px;text-align:center;color:var(--ink-3);">Loading…</div>';
      return;
    }
    if (!d.available) {
      ov.innerHTML = head + '<div class="wk-ov-body" style="padding:44px 22px;text-align:center;">' +
        '<div style="font-weight:600;">No run recorded for ' + esc(date) + '</div>' +
        '<div style="font-size:13px;color:var(--ink-3);margin-top:8px;max-width:340px;margin:8px auto 0;">' +
        'A run shows up here once its strap-detected session is confirmed in Workout ▸ History, or after you finish a guided run.</div></div>';
      return;
    }

    const S = d.hr.series || [];
    const maxT = S.length ? S[S.length - 1].t : 1;
    const W = 1000, H = 230, PAD = 8, PADB = 18;
    const tgt = d.target || {};
    let yLo = Math.min.apply(null, S.map(p => p.v));
    let yHi = Math.max.apply(null, S.map(p => p.v));
    if (tgt.band) { yLo = Math.min(yLo, tgt.band[0]); yHi = Math.max(yHi, tgt.band[1]); }
    if (tgt.hard_lo) yHi = Math.max(yHi, tgt.hard_lo);
    yLo = Math.max(45, yLo - 6); yHi = Math.min(212, yHi + 6);
    const span = (yHi - yLo) || 1;
    const xAt = t => PAD + (maxT ? t / maxT : 0) * (W - 2 * PAD);
    const yAt = v => (H - PADB) - (v - yLo) / span * (H - PAD - PADB);

    let bands = '';
    if (tgt.kind === 'band') {
      bands += '<rect x="' + PAD + '" y="' + yAt(tgt.band[1]).toFixed(1) + '" width="' + (W - 2 * PAD) +
        '" height="' + (yAt(tgt.band[0]) - yAt(tgt.band[1])).toFixed(1) +
        '" fill="oklch(0.74 0.14 150 / 0.18)"></rect>';
    } else if (tgt.kind === 'intervals') {
      if (tgt.hard_lo) bands += '<rect x="' + PAD + '" y="' + yAt(yHi).toFixed(1) + '" width="' + (W - 2 * PAD) +
        '" height="' + (yAt(tgt.hard_lo) - yAt(yHi)).toFixed(1) + '" fill="oklch(0.68 0.20 30 / 0.13)"></rect>';
      if (tgt.easy && tgt.easy[0] && tgt.easy[1]) bands += '<rect x="' + PAD + '" y="' + yAt(tgt.easy[1]).toFixed(1) +
        '" width="' + (W - 2 * PAD) + '" height="' + (yAt(tgt.easy[0]) - yAt(tgt.easy[1])).toFixed(1) +
        '" fill="oklch(0.74 0.14 150 / 0.14)"></rect>';
    }
    const line = S.map((p, i) => (i ? 'L' : 'M') + xAt(p.t).toFixed(1) + ' ' + yAt(p.v).toFixed(1)).join(' ');
    const avgY = yAt(d.hr.avg).toFixed(1);
    const pk = S.reduce((m, p) => p.v > m.v ? p : m, S[0] || { v: 0, t: 0 });
    const gy = [120, 150, 180].filter(v => v > yLo && v < yHi).map(v =>
      '<line x1="' + PAD + '" y1="' + yAt(v).toFixed(1) + '" x2="' + (W - PAD) + '" y2="' + yAt(v).toFixed(1) +
      '" stroke="var(--line)" stroke-width="1"></line>' +
      '<text x="' + (PAD + 2) + '" y="' + (yAt(v) - 2).toFixed(1) + '" fill="var(--ink-4)" font-size="12" font-family="var(--mono)">' + v + '</text>').join('');
    const chart =
      '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="width:100%;height:180px;display:block;">' +
      bands + gy +
      '<line x1="' + PAD + '" y1="' + avgY + '" x2="' + (W - PAD) + '" y2="' + avgY +
      '" stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="5 4"></line>' +
      '<path d="' + line + '" fill="none" stroke="oklch(0.78 0.15 150)" stroke-width="2.2" stroke-linejoin="round"></path>' +
      '<circle cx="' + xAt(pk.t).toFixed(1) + '" cy="' + yAt(pk.v).toFixed(1) + '" r="3.4" fill="oklch(0.68 0.20 30)"></circle>' +
      '</svg>';

    const zoneMax = Math.max.apply(null, d.zones.map(z => z.min)) || 1;
    const zoneBars = d.zones.map((z, i) =>
      '<div style="display:flex;align-items:center;gap:8px;margin-top:5px;">' +
      '<span style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.04em;color:var(--ink-3);width:74px;flex:none;">' + esc(z.name) + '</span>' +
      '<div style="flex:1;height:12px;background:var(--line);border-radius:3px;overflow:hidden;">' +
      '<div style="height:100%;width:' + (100 * z.min / zoneMax).toFixed(0) + '%;background:' + HR_ZONE_COLORS[i] + ';"></div></div>' +
      '<span class="wk-mono" style="font-size:12px;color:var(--ink-2);width:56px;text-align:right;flex:none;">' + z.min + 'm · ' + z.pct + '%</span></div>').join('');

    let adherence = '';
    if (tgt.kind === 'band') {
      adherence =
        '<div style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);margin:16px 0 7px;">Target ' + tgt.band[0] + '–' + tgt.band[1] + ' bpm</div>' +
        '<div style="display:flex;height:16px;border-radius:4px;overflow:hidden;font-family:var(--mono);font-size:12px;">' +
        '<div style="width:' + tgt.below_pct + '%;background:oklch(0.72 0.05 250);"></div>' +
        '<div style="width:' + tgt.in_pct + '%;background:oklch(0.74 0.14 150);"></div>' +
        '<div style="width:' + tgt.above_pct + '%;background:oklch(0.68 0.20 30);"></div></div>' +
        '<div style="display:flex;justify-content:space-between;font-family:var(--mono);font-size:12px;color:var(--ink-3);margin-top:5px;">' +
        '<span>' + tgt.below_pct + '% below</span><span style="color:var(--accent);">' + tgt.in_pct + '% in band</span><span>' + tgt.above_pct + '% above</span></div>';
    } else if (tgt.kind === 'intervals' && tgt.blocks && tgt.blocks.length) {
      const bmax = Math.max.apply(null, tgt.blocks.map(b => b.peak)) || 1;
      const bmin = Math.min.apply(null, tgt.blocks.map(b => b.peak)) || 0;
      let hdr, legend;
      if (tgt.looks_interval) {
        const hb = tgt.blocks.filter(b => b.hard);
        const hit = hb.filter(b => b.hit).length;
        hdr = hit + '/' + hb.length + ' hard reps ≥ ' + tgt.hard_lo + ' bpm';
        legend = 'red = hard rep · amber = hard rep under target · grey = recovery jog';
      } else {
        hdr = (tgt.block_min || 3) + '-min block peaks · planned as intervals — this run didn’t oscillate like one';
        legend = 'coloured by HR zone · no lap data, so blocks aren’t matched to reps';
      }
      adherence =
        '<div style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);margin:16px 0 7px;">' + esc(hdr) + '</div>' +
        '<div style="display:flex;align-items:flex-end;gap:3px;height:64px;">' +
        tgt.blocks.map(b => {
          const h = 12 + 52 * (b.peak - bmin) / ((bmax - bmin) || 1);
          const c = tgt.looks_interval
            ? (!b.hard ? 'var(--line-2)' : (b.hit ? 'oklch(0.68 0.20 30)' : 'oklch(0.80 0.13 85)'))
            : hrZoneColor(b.peak, b.peak);
          return '<div title="peak ' + b.peak + ' bpm" style="flex:1;height:' + h.toFixed(0) + 'px;background:' + c + ';border-radius:2px 2px 0 0;"></div>';
        }).join('') + '</div>' +
        '<div style="font-family:var(--mono);font-size:12px;color:var(--ink-3);margin-top:4px;">' + esc(legend) + '</div>';
    }

    const hrr = d.hrr && d.hrr.one_min != null
      ? d.hrr.one_min + (d.hrr.two_min != null ? ' / ' + d.hrr.two_min : '') + ' bpm'
      : '—';
    const kc = d.kcal && d.kcal.net != null ? d.kcal.net + ' kcal' : '—';
    const tiles =
      '<div class="wk-runtiles">' +
      tile('Avg HR', d.hr.avg, 'peak ' + d.hr.peak) +
      tile('HRR 1/2 min', hrr, d.hrr && d.hrr.one_min != null ? 'drop after effort' : 'no clean cooldown') +
      tile('Energy', kc, d.kcal && d.kcal.cooldown ? '+' + d.kcal.cooldown + ' cooldown' : 'effort, marginal') +
      tile('~Distance', d.distance_km != null ? d.distance_km + ' km' : '—', 'steps × stride') +
      tile('~Pace', d.pace_s_per_km ? fmtPace(d.pace_s_per_km) + ' /km' : '—', d.cadence_spm ? d.cadence_spm + ' spm' : '') +
      tile('Duration', fmtDur(d.duration_min * 60), d.work_min ? d.work_min + ' min effort' : '') +
      '</div>';

    const body =
      '<div style="font-family:var(--mono);font-size:12px;color:var(--ink-3);letter-spacing:0.04em;margin-bottom:4px;">' + esc(d.source || '') + '</div>' +
      '<div class="panel" style="padding:12px 10px 6px;">' + chart +
      '<div style="display:flex;justify-content:space-between;font-family:var(--mono);font-size:12px;color:var(--ink-3);padding:0 4px;">' +
      '<span>0</span><span>' + Math.round(maxT) + ' min</span></div></div>' +
      tiles +
      '<div style="font-family:var(--sans);font-weight:500;font-size:12px;letter-spacing:0.01em;color:var(--ink-3);margin:16px 0 2px;">Time in zone · HRmax ' + d.hrmax + '</div>' +
      zoneBars + adherence +
      '<div style="font-family:var(--mono);font-size:12px;color:var(--ink-3);margin-top:16px;text-align:center;">the strap has no GPS — distance &amp; pace are ~±15% (steps × ' + (d.stride_m || '?') + ' m)</div>';

    ov.innerHTML = head + '<div class="wk-ov-body" style="padding:16px;"><div style="max-width:680px;margin:0 auto;">' + body + '</div></div>';
  }

  function render() {
    if (!skeletonBuilt) return;
    renderDaybar(); renderSession(); renderStats(); renderExercises(); renderSidebar();
    renderTraining(); renderHistory(); renderLibrary(); renderRunReview(); renderMediaPop(); renderConfirmPop(); renderSwapPop(); renderSettingsPop();
  }

  // ── tick (timer/rest repaint only) ─────────────────────────────────────────
  function renderTick() {
    const el = Math.round(elapsedSec());
    const lbl = fmtTime(el);
    const a = document.getElementById('wk-timer'); if (a) a.textContent = lbl;
    const b = document.getElementById('wk-tr-timer'); if (b) b.textContent = lbl;
    if (WK.training.active && WK.training.phase === 'rest') {
      const rl = restLeft();
      const num = document.getElementById('wk-rest-num'); if (num) num.textContent = rl;
      const arc = document.getElementById('wk-rest-arc');
      if (arc) arc.setAttribute('stroke-dashoffset', (C_REST * (1 - Math.min(1, Math.max(0, rl / restTotal())))).toFixed(1));
      if (WK.training.restEndEpoch && Date.now() >= WK.training.restEndEpoch) { wkChime(); finishRest(); }
    }
    if (WK.training.active && WK.training.phase === 'work') {
      const ex = curExs()[WK.training.exIndex];
      const st = ex && ex.sets[WK.training.setIndex];
      if (isTime(ex) && st && (WK.training.timeEndEpoch || WK.training.timeRemainMs != null)) {
        const left = timeLeft();
        const num = document.getElementById('wk-work-num'); if (num) num.textContent = fmtDur(left);
        const arc = document.getElementById('wk-work-arc');
        const dur = st.durationSec || 1;
        if (arc) arc.setAttribute('stroke-dashoffset', (C_REST * (1 - Math.min(1, Math.max(0, left / dur)))).toFixed(1));
        if (WK.training.timeEndEpoch && left <= 0) { wkChime(); stepDone(); }
      }
    }
  }
  function startTick() {
    if (tickInt) return;
    tickInt = setInterval(() => { if (!document.hidden) renderTick(); }, 500);
  }

  // ── event handlers ─────────────────────────────────────────────────────────
  function onClick(e) {
    const node = e.target.closest('[data-act]');
    if (!node || !node.closest('#screen-workout, .wk-overlay, .wk-pop')) return;
    const act = node.dataset.act;
    const ei = node.dataset.ei != null ? +node.dataset.ei : null;
    const si = node.dataset.si != null ? +node.dataset.si : null;
    switch (act) {
      case 'show-media': openMedia(ei, node.dataset.from); break;
      case 'close-media': BackStack.pop(); break;   // backdrop tap / ✕ — same path as the back gesture
      case 'noop': break;                            // taps inside the popup card must not reach the backdrop
      case 'day': setActiveDay(node.dataset.day); break;
      case 'swap-open':
        if (WK.ui.swapOpen) { BackStack.pop(); }
        else {
          WK.ui.swapOpen = true; render();
          BackStack.push(() => { WK.ui.swapOpen = false; render(); });
        }
        break;
      case 'swap-pick': setDayOverride(node.dataset.day); break;
      case 'swap-revert': setDayOverride(null); break;
      case 'variant': setVariant(sourceDay(WK.activeDay), +node.dataset.idx); break;
      case 'start-training':
        startTraining();
        // Covers a fresh start and a resume-from-paused alike; exitTraining()
        // only pauses (never destructive), so back-gesture mid-session is safe.
        if (WK.training.active) BackStack.push(() => exitTraining());
        break;
      case 'toggle-timer': toggleTimer(); break;
      case 'reset-training': openConfirmReset(); break;
      case 'close-confirm': BackStack.pop(); break;   // "No" / backdrop — same path as the back gesture
      case 'close-swap': BackStack.pop(); break;      // Cancel / backdrop — same path as the back gesture
      case 'open-settings': openSettings(); break;
      case 'close-settings': BackStack.pop(); break;  // Done / backdrop — same path as the back gesture
      case 'rest-step': setRest(node.dataset.key, WK.settings[node.dataset.key] + +node.dataset.delta); break;
      case 'rest-set': setRest(node.dataset.key, +node.dataset.val); break;
      case 'swap-scope': {
        const p = WK.ui.pendingSwap; if (!p) break;
        WK.ui.pendingSwap = null;
        applySwap(p.ei, p.to, node.dataset.scope);
        BackStack.dismiss();   // popup already closed itself: keep history in sync, don't re-run onClose
        break;
      }
      case 'confirm-reset':
        WK.ui.confirmReset = false;
        resetTraining();
        BackStack.dismiss();   // popup already closed itself: keep history in sync, don't re-run onClose
        break;
      case 'add-exercise': addExercise(); break;
      case 'toggle-mode': toggleExMode(ei); break;
      case 'rm-ex': removeExercise(ei); break;
      case 'move-ex': moveExercise(ei, +node.dataset.dir); break;
      case 'add-set': addSet(ei); break;
      case 'rm-set': removeSet(ei, si); break;
      case 'set-done': toggleDone(ei, si); break;
      case 'library':
        WK.ui.libraryOpen = true; render();
        BackStack.push(() => { WK.ui.libraryOpen = false; render(); });
        break;
      case 'close-library': BackStack.pop(); break;
      case 'history':
        WK.ui.historyOpen = true; WK.ui.calOpen = false; WK.ui.selectedWeek = null; WK.ui.calOffset = 0;
        render();
        apiActivities().then(a => { WK.activities = a; renderHistory(); });
        BackStack.push(() => { WK.ui.historyOpen = false; render(); });
        break;
      case 'toggle-dismissed': WK.ui.showDismissed = !WK.ui.showDismissed; renderHistory(); break;
      case 'close-history': BackStack.pop(); break;
      case 'open-runreview': openRunReview(todayISO()); break;
      case 'open-run': openRunReview(node.dataset.date); break;
      case 'close-runreview': BackStack.pop(); break;
      case 'toggle-cal': WK.ui.calOpen = !WK.ui.calOpen; render(); break;
      case 'cal-prev': WK.ui.calOffset -= 1; render(); break;
      case 'cal-next': WK.ui.calOffset += 1; render(); break;
      case 'select-week': WK.ui.selectedWeek = WK.ui.selectedWeek === node.dataset.week ? null : node.dataset.week; render(); break;
      case 'show-all': WK.ui.selectedWeek = null; render(); break;
      case 'del-session': WK.sessions = WK.sessions.filter(s => s.id !== node.dataset.sid); apiDeleteSession(node.dataset.sid); render(); break;
      case 'upload': pendingUploadSlug = node.dataset.slug; fileInput.click(); break;
      // guided mode
      case 'w-minus': adjustWeight(-WEIGHT_STEP); break;
      case 'w-plus': adjustWeight(WEIGHT_STEP); break;
      case 'r-minus': adjustReps(-1); break;
      case 'r-plus': adjustReps(1); break;
      case 'step-done': stepDone(); break;
      case 'time-pause': toggleTimedPause(); break;
      case 'time-add': addTime(); break;
      case 'skip-rest': skipRest(); break;
      case 'add-rest': addRest(); break;
      case 'rpe': updateSet(WK.training.exIndex, WK.training.setIndex, 'rpe', node.dataset.rpe); break;
      case 'pain-toggle': togglePain(node.dataset.area); break;
      case 'exit-training': BackStack.pop(); break;
      case 'finish-training':
        finishTraining();
        BackStack.dismiss();  // already closed itself - just keep history in sync
        break;
    }
  }

  // Live capture of the finish-screen notes so a re-render (e.g. tapping an RPE
  // button) never wipes text typed but not yet blurred.
  function onInput(e) {
    const node = e.target.closest('[data-act="wk-notes"]');
    if (!node) return;
    WK.training.notes = node.value;
    saveActiveLS();
  }
  function togglePain(area) {
    if (!area) return;
    const t = WK.training;
    if (!t.pain) t.pain = [];
    const i = t.pain.indexOf(area);
    if (i >= 0) t.pain.splice(i, 1); else t.pain.push(area);
    saveActiveLS(); render();
  }

  function onChange(e) {
    const node = e.target.closest('[data-act]');
    if (!node || !node.closest('#screen-workout, .wk-overlay')) return;
    const act = node.dataset.act;
    const ei = node.dataset.ei != null ? +node.dataset.ei : null;
    const si = node.dataset.si != null ? +node.dataset.si : null;
    switch (act) {
      case 'swap': requestSwap(ei, node.value); break;
      case 'set-w': updateSet(ei, si, 'weight', node.value); break;
      case 'set-reps': updateSet(ei, si, 'reps', node.value); break;
      case 'set-dur': updateDur(ei, si, node.value); break;
      case 'set-rpe': updateSet(ei, si, 'rpe', node.value); break;
      case 'cur-weight': updateCurSet('weight', node.value); break;
      case 'cur-reps': updateCurSet('reps', node.value); break;
    }
  }

  // ── media upload ───────────────────────────────────────────────────────────
  const MAX_VIDEO_BYTES = 50_000_000;  // must match api.py
  const ALLOWED_EXTS = ['mp4', 'webm', 'mov', 'gif', 'jpg', 'jpeg', 'png', 'webp'];
  async function onFilePicked() {
    const f = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!f || !pendingUploadSlug) return;
    const sl = pendingUploadSlug; pendingUploadSlug = null;
    if (f.size > MAX_VIDEO_BYTES) {
      alert('File is too large (' + Math.round(f.size / 1e6) + ' MB). Max 50 MB — trim the clip or use a smaller image.');
      return;
    }
    const ext = (f.name || '').split('.').pop().toLowerCase();
    if (ALLOWED_EXTS.indexOf(ext) === -1) {
      alert('Unsupported file type. Use mp4, webm, mov, gif, jpg, png or webp.');
      return;
    }
    try {
      // Raw upload — no re-encode (browsers can't transcode video client-side);
      // the file streams straight to the server and renders as-is.
      const res = await apiUploadVideo(sl, f);
      if (res && res.file) { WK.videos[sl] = res.file + '?t=' + Date.now(); render(); }
    } catch (e) { /* ignore upload failure */ }
  }

  // ── entry ──────────────────────────────────────────────────────────────────
  // ── readiness card (top of workout tab; data from night_physio) ──────────
  async function renderReadiness() {
    const el = document.getElementById('wk-readiness');
    if (!el) return;
    let d = null;
    try { const r = await fetch('/api/readiness'); if (r.ok) d = await r.json(); } catch (e) { }
    if (!d || d.score == null) { el.style.display = 'none'; return; }
    el.style.display = '';
    const col = d.score >= 75 ? 'var(--good, #4ade80)'
              : d.score >= 55 ? 'var(--warn, #fbbf24)' : 'var(--bad, #ef4444)';
    const label = d.score >= 75 ? 'Ready to push'
                : d.score >= 55 ? 'Train, but listen to your body' : 'Go easy today';
    const P = d.parts || {};
    const bar = (name, v) =>
      '<div class="wk-rd-bar">' +
      '  <div class="wk-rd-bl">' + name + ' · ' + (v == null ? '—' : v) + '</div>' +
      '  <div class="wk-rd-track">' +
      '    <div style="width:' + (v || 0) + '%;background:' + col + ';"></div>' +
      '  </div></div>';
    // Recent pain flags → a transparent caution (not part of the score).
    let painHtml = '';
    const painList = P.pain || [];
    if (painList.length) {
      const areas = Array.from(new Set([].concat.apply([], painList.map(p => p[1] || []))))
        .map(a => a.replace(/-/g, ' '));
      painHtml =
        '<div class="wk-rd-note" style="color:var(--danger);">' +
        '⚠ Flagged recently: ' + esc(areas.join(', ')) + ' — ease into anything that loads it.</div>';
    }
    // Readiness-driven training-load cut for today (null on a green/amber day).
    // Applying it stamps a one-day override (see appliedTrimToday) — the
    // recurring plan template is never rewritten.
    let trimHtml = '';
    if (d.trim) {
      const applied = appliedTrimToday();
      const isApplied = !!applied;
      trimHtml =
        '<div class="wk-rd-note wk-rd-trim" style="color:var(--warn, #fbbf24);">' +
        '<span>📉 ' + esc(d.trim.summary) + ' (~-' + d.trim.pct + '%).</span>' +
        (isApplied
          ? '<span style="color:var(--good, #4ade80);">✓ Aplicado</span>' +
            '<button id="wk-trim-undo" style="font-size:inherit;padding:3px 8px;border-radius:6px;border:1px solid var(--line);background:transparent;color:var(--ink-2);cursor:pointer;">Reverter</button>'
          : '<button id="wk-trim-apply" style="font-size:inherit;padding:3px 8px;border-radius:6px;border:1px solid var(--warn, #fbbf24);background:transparent;color:var(--warn, #fbbf24);cursor:pointer;">Aplicar</button>') +
        '</div>';
    }
    el.innerHTML =
      '<div class="wk-rd-main">' +
      '  <div class="wk-rd-score">' +
      '    <div class="wk-mono wk-rd-num" style="color:' + col + ';">' + d.score + '</div>' +
      '    <div class="wk-rd-lbl">Readiness</div>' +
      '  </div>' +
      '  <div class="wk-rd-body">' +
      '    <div class="wk-rd-head">' + label + '</div>' +
      '    <div class="wk-rd-bars">' +
           bar('Sleep', P.sleep) + bar('RHR', P.rhr) + bar('HRV', P.hrv) + bar('Load', P.load) +
      '    </div>' +
      '  </div>' +
      '</div>' + painHtml + trimHtml;
    el.style.display = '';
    const applyBtn = document.getElementById('wk-trim-apply');
    if (applyBtn) applyBtn.onclick = async () => {
      applyBtn.disabled = true; applyBtn.textContent = '…';
      await applyTrainingTrim();
      await renderReadiness();
      if (WK.loaded) render();
    };
    const undoBtn = document.getElementById('wk-trim-undo');
    if (undoBtn) undoBtn.onclick = async () => {
      undoBtn.disabled = true; undoBtn.textContent = '…';
      await undoTrainingTrim();
      await renderReadiness();
      if (WK.loaded) render();
    };
  }

  window.workoutOnShow = async function () {
    buildSkeleton();
    renderReadiness();  // async, fills its own panel when data exists
    if (!WK.loaded) {
      let planResp, sessions, videos;
      try { [planResp, sessions, videos] = await Promise.all([apiGetPlan(), apiGetSessions(), apiListVideos()]); }
      catch (e) { planResp = { plan: null }; sessions = []; videos = {}; }
      await ensureSeeded(planResp);
      WK.sessions = sessions || [];
      WK.videos = videos || {};
      await loadSoreness();
      loadActiveLS();
      const reverted = maybeDayRevert();   // before the weekly reset, which clears progress
      maybeWeekReset();
      if (reverted) savePlanDebounced();
      WK.loaded = true; WK.fetchedAt = Date.now();
    } else if (!WK.training.active && Date.now() - WK.fetchedAt > 60000) {
      // refetch plan/sessions if stale and not mid-session (never clobber a live session)
      try {
        const [planResp, sessions] = await Promise.all([apiGetPlan(), apiGetSessions()]);
        if (planResp && planResp.plan && (planResp.plan._schema || 0) >= 2) { WK.plan = planResp.plan; WK.weekKey = planResp.week_key || WK.weekKey; }
        WK.sessions = sessions || WK.sessions;
        const reverted = maybeDayRevert();
        maybeWeekReset();
        if (reverted) savePlanDebounced();
      } catch (e) { }
      WK.fetchedAt = Date.now();
    }
    render();
    startTick();
  };

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    // an installed PWA can stay open across midnight: restore yesterday's 'just today' swaps
    if (maybeDayRevert()) { savePlanDebounced(); render(); }
    renderTick();
    if (trainingIsCardio() && !WK.training.paused) acquireWakeLock();  // lock drops when hidden
  });
})();
