"""Tables owned by the API (ingest never writes them), created once at start-up."""

from acta.api.deps import open_db_rw
from acta.engine import fitness, pai
from acta.finance import holdings as fh
from acta.finance import spend as fs
from acta.insights import auspex
from acta.productivity import boards
from acta.tracking import body, nutrition


def init_workout_tables() -> None:
    """Create the API-owned workout tables. Runs once at service start.
    These are NOT ingest-owned — ingest.py never touches them."""
    con = open_db_rw()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS workout_plan (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                plan_json  TEXT NOT NULL,
                week_key   TEXT,
                updated_at TEXT NOT NULL
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS workout_session (
                id             TEXT PRIMARY KEY,
                date_iso       TEXT NOT NULL,
                day_key        TEXT NOT NULL,
                title          TEXT NOT NULL,
                duration_sec   INTEGER NOT NULL,
                sets_done      INTEGER NOT NULL,
                sets_planned   INTEGER NOT NULL,
                volume         REAL NOT NULL,
                avg_rpe        REAL,
                exercises_json TEXT NOT NULL,
                created_at     TEXT NOT NULL
            )""")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_workout_session_date "
            "ON workout_session(date_iso)")
        # Migration: post-workout notes + pain flags (added 2026-07-20).
        # Older DBs predate these columns — add them if missing.
        cols = {r[1] for r in con.execute("PRAGMA table_info(workout_session)")}
        if "notes" not in cols:
            con.execute("ALTER TABLE workout_session ADD COLUMN notes TEXT")
        if "pain_flags" not in cols:
            con.execute("ALTER TABLE workout_session ADD COLUMN pain_flags TEXT")
        # Body-map soreness (added 2026-07-22). One row per area per log; the
        # newest row inside the decay window is what counts as "current".
        con.execute("""
            CREATE TABLE IF NOT EXISTS muscle_soreness (
                ts       INTEGER NOT NULL,   -- epoch ms
                area     TEXT    NOT NULL,   -- body-map slug, e.g. 'chest'
                severity INTEGER NOT NULL,   -- intensity: 1 mild · 2 moderate · 3 severe
                note     TEXT,
                PRIMARY KEY (ts, area)
            )""")
        # Type of feeling (added 2026-09-24): ache · tight · heavy · sharp. Rows from before
        # it (and callers that send a bare severity) have NULL and read as 'ache'.
        sore_cols = {r[1] for r in con.execute("PRAGMA table_info(muscle_soreness)")}
        if "kind" not in sore_cols:
            con.execute("ALTER TABLE muscle_soreness ADD COLUMN kind TEXT")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_muscle_soreness_ts "
            "ON muscle_soreness(ts)")
        con.commit()
    finally:
        con.close()


def init_auspex_tables() -> None:
    """Auspex owns its own log table, same posture as the workout tables:
    created by the API at start, never touched by ingest."""
    auspex.open_rw().close()   # open_rw() runs ensure(); close it rather than leak


def init_body_tables() -> None:
    """Body config + weight log. Schema lives in body.py so calories.py (which
    runs from ingest, without the API) can create it too."""
    con = open_db_rw()
    try:
        body.ensure(con)
        nutrition.ensure(con)
        nutrition.seed_builtins(con)
        fitness.ensure(con)   # fitness_estimate — ingest-owned, created here too
        con.commit()
    finally:
        con.close()


def init_finance_tables() -> None:
    """Create the API-owned finance tables. Runs once at service start.
    Snapshot model: finance_balance rows are the only source of truth for
    money — nothing else in the system ever mutates a balance. `amount_eur`
    is stored alongside native `amount` so a later FX rate change never
    silently rewrites history (FX conversion itself lands in a later phase;
    Phase 1 is EUR-only so amount_eur == amount)."""
    con = open_db_rw()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS finance_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS finance_account (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                kind       TEXT NOT NULL,   -- bank | cash | stocks | crypto | other
                currency   TEXT NOT NULL DEFAULT 'EUR',
                active     INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS finance_balance (
                account_id INTEGER NOT NULL,
                as_of      TEXT    NOT NULL,   -- YYYY-MM-DD
                amount     REAL    NOT NULL,   -- native currency
                amount_eur REAL    NOT NULL,   -- converted at snapshot time
                note       TEXT,
                created_at TEXT    NOT NULL,
                PRIMARY KEY (account_id, as_of),
                FOREIGN KEY (account_id) REFERENCES finance_account(id)
            )""")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_finance_balance_asof "
            "ON finance_balance(as_of)")
        # Phase 4: holdings for investment accounts. These do not compete with
        # finance_balance — they feed it. An investment account's balance is
        # derived from quantity x price and written back as an ordinary
        # snapshot, so net worth, history, sparklines and the projection keep
        # reading exactly one source of truth. Purely additive: an account with
        # no holdings keeps its entered balance untouched.
        fh.ensure(con)
        # Phase 2: recurring subscriptions/income. Monthly cadence only — no
        # cadence field yet, "input once, appears every month on that day".
        con.execute("""
            CREATE TABLE IF NOT EXISTS finance_sub (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                amount       REAL NOT NULL,   -- always positive; direction carries the sign
                direction    TEXT NOT NULL,   -- 'in' | 'out'
                day_of_month INTEGER NOT NULL,  -- 1-31, clamped to the last day in short months
                account_id   INTEGER,           -- nullable: sub may not be tied to one account
                active       INTEGER NOT NULL DEFAULT 1,
                note         TEXT,
                created_at   TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES finance_account(id)
            )""")
        # Phase 3: wish list. want/need are never stored "current" on the item
        # itself — finance_wish_rating is the only place they live, so there is
        # exactly one place to update and "current" is always just its latest
        # row per wish_id. That's what makes the want-decay trend possible.
        con.execute("""
            CREATE TABLE IF NOT EXISTS finance_wish (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                price      REAL NOT NULL,
                link       TEXT,
                status     TEXT NOT NULL DEFAULT 'active',   -- active | bought | dropped
                added_on   TEXT NOT NULL,                    -- YYYY-MM-DD
                bought_on  TEXT,
                created_at TEXT NOT NULL
            )""")
        con.execute("""
            CREATE TABLE IF NOT EXISTS finance_wish_rating (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                wish_id INTEGER NOT NULL,
                ts      INTEGER NOT NULL,   -- epoch ms
                want    INTEGER NOT NULL,   -- 1-10
                need    INTEGER NOT NULL,   -- 1-10
                FOREIGN KEY (wish_id) REFERENCES finance_wish(id)
            )""")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_finance_wish_rating_wish "
            "ON finance_wish_rating(wish_id)")
        # Phase 4: ad-hoc transactions (a purchase, a transfer) — the things the
        # subs schedule cannot predict.
        #
        # A transaction NEVER writes finance_balance. It only proposes what the
        # balance should now be, exactly like a sub occurrence does, and stays
        # `pending` until the user confirms the balance it implies. That keeps
        # the founding rule intact — a balance is only ever a number the user
        # vouched for — and keeps `unaccounted` meaningful: if logging a
        # transaction silently moved the balance, the drift signal it exists to
        # produce would be zero by construction.
        con.execute("""
            CREATE TABLE IF NOT EXISTS finance_tx (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id  INTEGER NOT NULL,
                amount_eur  REAL NOT NULL,     -- signed: negative spend, positive income
                description TEXT,
                occurred_on TEXT NOT NULL,     -- YYYY-MM-DD
                status      TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|dismissed
                created_at  TEXT NOT NULL,
                applied_at  TEXT,
                FOREIGN KEY (account_id) REFERENCES finance_account(id)
            )""")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_finance_tx_pending "
            "ON finance_tx(account_id, status, occurred_on)")
        # Transfers: two linked legs sharing a transfer_id.
        #
        # `target` is the interesting column. Money moved into a plain account
        # changes the balance you vouch for. Money moved into a holdings-derived
        # account has not bought anything yet, so it lands as uninvested CASH —
        # a different input, confirmed on a different card. Recording that on the
        # leg keeps the two paths from having to re-derive it from the account
        # kind at every read (and getting it wrong once an account gains its
        # first holding and silently becomes derived).
        tx_cols = {r[1] for r in con.execute("PRAGMA table_info(finance_tx)")}
        for col, decl in (("transfer_id", "TEXT"),
                          ("target", "TEXT NOT NULL DEFAULT 'balance'")):
            if col not in tx_cols:
                con.execute(f"ALTER TABLE finance_tx ADD COLUMN {col} {decl}")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_finance_tx_transfer "
            "ON finance_tx(transfer_id)")
        # Spending ledger: category / source / note on the same rows.
        fs.ensure(con)
        con.commit()
    finally:
        con.close()


def init_productivity_tables() -> None:
    """Productivity tab: native Kanban boards + the weekly-schedule template.
    API-owned, never touched by ingest. Schema for the Kanban side lives in
    boards.py so it stays next to the CRUD that uses it."""
    con = open_db_rw()
    try:
        boards.ensure(con)
        con.execute("""
            CREATE TABLE IF NOT EXISTS schedule_block (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                dow        INTEGER NOT NULL,   -- 0=Mon .. 6=Sun
                start_min  INTEGER NOT NULL,   -- minutes from local 00:00
                end_min    INTEGER NOT NULL,
                title      TEXT NOT NULL,
                category   TEXT NOT NULL DEFAULT 'deep',
                note       TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_schedule_block_dow "
                    "ON schedule_block(dow, start_min)")
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    """Everything the API needs before serving its first request, in order."""
    pai.ensure_schema()  # DDL once at start, so the read paths stay read-only
    # Device day-grid into biocharge's registry. ingest does this per run; the
    # API process must too, or BC.load_activity() here would use the home zone's
    # grid while ingest used the device's, and the same date could yield a
    # start_min an hour apart depending on which process computed it.
    pai.register_tz()
    init_workout_tables()
    init_auspex_tables()
    init_body_tables()
    init_finance_tables()
    init_productivity_tables()
