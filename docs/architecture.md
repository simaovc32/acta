# Architecture

## Data flow

```
phone (Gadgetbridge app)  --sync-->  Gadgetbridge.db  --acta ingest-->  acta.db  <--  FastAPI  <-->  web / PWA
                                                              ^                        |
                                   events.json  --------------+------------------------+
                                   (coffee, alcohol, workouts: written by the API, read by ingest)
```

There are three stores:

| Store | Written by | Read by | Notes |
|---|---|---|---|
| `Gadgetbridge.db` | the Gadgetbridge Android app (synced to the server) | ingest, a few API reads | Raw strap samples. **Never written by Acta.** Opened `mode=ro&immutable=1`. |
| `events.json` | the API (atomic, file-locked appends) | ingest, biocharge, sleep score, API | The event log: things the strap can't see (a coffee, three beers, a gym session with no strap). |
| `acta.db` | ingest (derived tables), API (user-entered tables) | everything | SQLite in WAL mode. Each table has exactly one owner. |

### Table ownership

- **Ingest-owned (derived, safe to recompute):** `sleep_score`, `biocharge` (one row per
  minute), `night_physio`, `daily_energy`, `pai_detection`, `fitness_estimate`,
  `ingest_state`, `ingest_run`.
- **API-owned (user data, never recomputed):** body metrics and config, food library and
  log, meals, workout plan and sessions, soreness, every `finance_*` table, Kanban boards,
  the weekly schedule, the Auspex log, mood check-ins (`user_log`).

The split matters in practice: a full replay can rebuild every derived table from the two
input files, and it can't touch anything a person typed.

## The ingest pipeline

`acta ingest` (`src/acta/pipeline/ingest.py`) runs on a timer. Each run goes through three
gates, cheapest first:

1. **Fingerprint:** `mtime:size` of the strap export and the event log. If both are
   unchanged, stop. This is about 95% of runs and costs one `stat()` call.
2. **Content hash:** copy the export to a temp file and SHA-256 it, together with the
   event log. Syncing often rewrites the file with identical bytes; if the hash matches,
   record the new fingerprint and stop.
3. **Recompute:** load every sleep session (keeping the longest version of each night,
   since a night is re-synced several times), score the new nights plus the last two, then
   replay biocharge forward from the last settled day. After that come the non-fatal leaf
   steps: night physiology, calories, PAI detection, VO₂max, illness fingerprint.

A failed run is recorded in `ingest_run` with its error before it re-raises. A pipeline
that fails silently looks healthy on the dashboard, which is worse than one that fails
loudly.

### Time zones

The strap records in device-local time, and the device travels. Each sleep session's header
holds the local midnight as an absolute epoch, so the UTC offset for that night can be
recovered as `-(epoch mod 86400)`. Days are then anchored to the **device's** midnight, not
the server's. A night slept abroad lands on the right date, and a day that crosses time
zones is its real 23 or 25 hours long. Those nights are kept out of the rolling baselines.

## The API

`src/acta/api/app.py` builds the FastAPI app from one router per area:

| Router | Covers |
|---|---|
| `overview` | home-screen summary, system info, manual refresh, active modifiers |
| `biocharge` | the per-minute curve, naps, the day's workouts |
| `sleep`, `vitals` | nightly scores and hypnograms; RHR, HRV, SpO₂, stress, temperature, respiration |
| `journal` | mood check-ins, coffee/alcohol quick-log, manual workouts |
| `training` | detected workouts (confirm / dismiss), readiness, VO₂max, activity energy |
| `body`, `food`, `workout` | weight and calories, the food log, the Workout tab |
| `finance_auth`, `finance`, `portfolio`, `wishlist` | PIN gate, ledger, holdings and prices, wish list |
| `capture` | write-only endpoints for an external chat assistant |
| `auspex`, `productivity` | analytical questions; Kanban boards and the weekly schedule |

Shared plumbing lives beside the routers: `deps.py` (database handles, ingest trigger),
`days.py` (device-local day boundaries), `event_log.py` (the only writer of `events.json`),
`schema.py` (API-owned tables, created at start-up).

Interactive docs are at `/api/docs` when the server is running.

### Security model

Acta is single-user and meant for your own machine or a private network (I run it
behind Tailscale). Given that:

- **Finance** sits behind a PIN hashed with PBKDF2. Unlocking issues an in-memory session
  token with a sliding 30-minute expiry, and five wrong PINs trigger a lockout.
- **The capture API** (for a chat assistant logging "3.20 at the coffee shop") uses its own
  key, stored only as a hash. That key can **append** and nothing else: no reads, no edits,
  capped amounts, a daily limit, and undo only for its own recent entries. A leaked key
  can't expose or change existing data.
- Everything else is open to whoever can reach the port. Don't expose it to the internet.

## Frontend

`web/` is plain HTML, CSS and JavaScript with no framework and no build step, served as
static files by the same app.

- `css/` has five stylesheets loaded in order: base components, phone layout, Workout tab,
  Auspex, and a final design-refinement layer.
- `js/app/` holds the shell: tabs, i18n, back gesture, the health charts, the "add info"
  modal, and `live.js` (the live-data renderer for the health screens).
- `js/workout.js`, `js/finance.js`, `js/finance_spend.js`, `js/productivity.js` and
  `js/auspex.js` are self-contained modules, one per tab.
- Charts are hand-drawn SVG at their real pixel size, so text stays legible at any width.
- A service worker caches the shell, and the app installs as a PWA on a phone.

## Configuration

Everything comes from environment variables with defaults under `./data`; see
[`src/acta/config.py`](../src/acta/config.py). Modules read `config.*` at call time, never
copying a path into their own constants, so tests and the demo can redirect everything
from one place.
