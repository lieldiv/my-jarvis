"""
users.py — per-user account storage for the multi-tenant cloud build.

Replaces google_service.py's single .google_cache/token.json (one shared
account) with one row per signed-in user, keyed by their Google account's
own id.

Persistence backend: Postgres (via DATABASE_URL, e.g. a free Neon project)
when configured, else local sqlite3 for local dev. Turso was tried first
(hosted, free, SQLite-compatible) but its Python driver doesn't respect
any timeout on network calls -- confirmed directly that a stalled query
could hang long enough to trip gunicorn's own --timeout and SIGKILL the
whole worker, and every attempt at an application-level safety net around
that (a thread wrapper, a subprocess circuit breaker, a hard-fail variant)
introduced a new, worse, immediately-reproducible bug of its own: a
runaway multiprocessing fork bomb, a split-brain where a write landed in
Turso and a later read silently landed in local sqlite instead
(indistinguishable from data loss), and a health-probe false negative
that crashed the app at boot. Postgres via psycopg2 (a mature, 20+ year
old C driver) supports a real server-side statement_timeout that Postgres
itself enforces and cancels a stuck query for -- the client physically
cannot hang waiting on it the way libsql did, so this doesn't need any of
that apparatus.

Query functions below use sqlite's `?` placeholder style throughout
(matching every existing call site in this file and callers of
_connect()'s cursor); _PgCursor.execute() translates that to psycopg2's
`%s` at the boundary so none of those ~30 functions needed to be rewritten
by hand -- only the connection layer differs per backend.
"""

from __future__ import annotations  # PEP 604 `X | None` hints, running on Python 3.9

import logging
import os
import sqlite3
import time
from contextlib import contextmanager

logger = logging.getLogger("jarvis.users")

DB_PATH = os.environ.get("JARVIS_DB_PATH", "users.db")

# Standard env var name Neon (and most Postgres-as-a-service providers)
# auto-generate on their dashboard, so the connection string can be pasted
# in as-is with no reformatting. Local dev with no such env var set is
# completely unaffected -- same sqlite3 file as always.
PG_DATABASE_URL = os.environ.get("DATABASE_URL")
PG_CONFIGURED = bool(PG_DATABASE_URL)

if PG_CONFIGURED:
    import psycopg2


class _PgCursor:
    """Wraps a psycopg2 cursor so callers written against sqlite3's API
    (this file's own query functions, unchanged) work against Postgres
    without modification: translates `?` -> `%s` placeholders, and
    emulates sqlite3's `.lastrowid` via Postgres's lastval() (the value
    most recently produced by any SERIAL column's sequence in this
    session -- exactly what an INSERT into one just did), since psycopg2
    has no such attribute at all."""

    def __init__(self, cur):
        self._cur = cur
        self._is_insert = False

    def execute(self, sql, params=()):
        self._is_insert = sql.lstrip().upper().startswith("INSERT")
        self._cur.execute(sql.replace("?", "%s"), tuple(params))
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        if not self._is_insert:
            return None
        self._cur.execute("SELECT lastval()")
        return self._cur.fetchone()[0]


class _PgConnection:
    """Wraps a psycopg2 connection so `conn.execute(...)` works the same
    way sqlite3.Connection's own convenience method does (this file's
    query functions call .execute() directly on the connection, never on
    an explicit cursor) -- psycopg2 has no such method, only
    conn.cursor().execute()."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return _PgCursor(self._conn.cursor()).execute(sql, params)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _init_db():
    with _connect() as conn:
        if PG_CONFIGURED:
            # A brand-new Postgres database has no pre-recurrence-era
            # history to migrate, unlike the sqlite/Turso branch below --
            # every column just goes straight into CREATE TABLE, no
            # ALTER TABLE ADD COLUMN dance needed at all. SERIAL is
            # Postgres's auto-incrementing integer primary key.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    name TEXT,
                    google_token_json TEXT,
                    created_at REAL NOT NULL,
                    last_login_at REAL NOT NULL,
                    important_suggestions_enabled INTEGER NOT NULL DEFAULT 0,
                    important_senders TEXT,
                    important_suggestions_enabled_at REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS important_email_suggestions (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    gmail_message_id TEXT NOT NULL,
                    sender_name TEXT,
                    sender_email TEXT NOT NULL,
                    subject TEXT,
                    snippet TEXT,
                    date_ms INTEGER,
                    draft_subject TEXT,
                    draft_body TEXT,
                    draft_error TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    resolution TEXT,
                    created_at REAL NOT NULL,
                    resolved_at REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    remind_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    recurrence TEXT,
                    emoji TEXT,
                    flourish TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    category TEXT,
                    recurring_day TEXT,
                    created_at REAL NOT NULL,
                    completed_at REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_completions (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    completed_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS capability_gaps (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    user_request TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    raw_text TEXT,
                    created_at REAL NOT NULL,
                    notified_at REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_notices (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    action_label TEXT,
                    dedupe_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    dismissed_at REAL
                )
                """
            )
            return

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,       -- Google's own 'sub' (stable account id)
                email TEXT NOT NULL,
                name TEXT,
                google_token_json TEXT,    -- same JSON shape creds.to_json() already produces
                created_at REAL NOT NULL,
                last_login_at REAL NOT NULL
            )
            """
        )
        # Reminders: delivered by emailing the user (see daily_briefing.py's
        # scheduler), not by pushing to an open tab — proactive delivery has
        # to work whether or not anyone happens to have the HUD open at
        # remind_at. NOTE: local sqlite is ephemeral on Render's free tier
        # (wiped on restart/spin-down, see render.yaml's own note) -- that's
        # exactly why DATABASE_URL/Postgres is preferred when configured;
        # this whole branch only runs for local dev without it.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                remind_at REAL NOT NULL,   -- epoch seconds, UTC — next (or only) fire time
                created_at REAL NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                recurrence TEXT,            -- NULL for one-time; "weekday:hour:minute"
                                             -- (weekday: Monday=0..Sunday=6, local time) for weekly
                emoji TEXT,                 -- one emoji matching what the reminder is about
                flourish TEXT               -- short contextual line ("בהצלחה באימון!")
            )
            """
        )
        # ADD COLUMN on a table that already existed before recurrence was
        # introduced — CREATE TABLE IF NOT EXISTS above is a no-op on an
        # existing table, so already-provisioned databases need this run
        # once to catch up. SQLite has no ADD COLUMN IF NOT EXISTS; catching
        # the duplicate-column error is the documented way to make this
        # idempotent.
        try:
            conn.execute("ALTER TABLE reminders ADD COLUMN recurrence TEXT")
        except Exception:
            pass  # column already exists

        # Notification flavour, filled in by the LLM at the moment the user
        # ASKS for the reminder (see app.py's set_reminder tool schema), not
        # when it fires: daily_briefing.py's delivery path is deliberately
        # LLM-free so an expired/rate-limited Groq key can't take reminders
        # down, and the model is already in the loop at request time anyway.
        # Both are optional — a reminder with neither still delivers, just
        # without the flourish (also covers rows created before this existed).
        for column in ("emoji", "flourish"):
            try:
                conn.execute(f"ALTER TABLE reminders ADD COLUMN {column} TEXT")
            except Exception:
                pass  # column already exists

        # Important-email suggestions: added after the users table already
        # existed in deployed databases, so these three go through the same
        # idempotent ALTER-TABLE-and-ignore-the-duplicate-column-error dance
        # as reminders.recurrence/emoji/flourish above, not into the
        # CREATE TABLE itself.
        for column, decl in (
            ("important_suggestions_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("important_senders", "TEXT"),
            ("important_suggestions_enabled_at", "REAL"),
        ):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {decl}")
            except Exception:
                pass  # column already exists

        # One row per Gmail message the background scheduler (see
        # daily_briefing.py's _check_important_emails) matched against a
        # user's configured "important sender" list. Detection writes a
        # 'pending' row; the SAME background scheduler then drafts it
        # ('pending' -> 'drafted') -- but never sends it on its own. Sending
        # always goes through the normal confirm-to-act path
        # (productivity_service.request_send_email), the exact same gate
        # every other write in this app already uses: the product owner was
        # explicit that this feature must never send without the user's own
        # approval. The user can edit the draft (in the confirm modal itself,
        # via edit_pending_email) or dismiss it entirely before approving.
        # Nothing about this table's own correctness depends on an SSE push
        # ever reaching an open tab (see this module's docstring).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS important_email_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                gmail_message_id TEXT NOT NULL,
                sender_name TEXT,
                sender_email TEXT NOT NULL,
                subject TEXT,
                snippet TEXT,
                date_ms INTEGER,
                draft_subject TEXT,
                draft_body TEXT,
                draft_error TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                resolution TEXT,
                created_at REAL NOT NULL,
                resolved_at REAL
            )
            """
        )

        # Tasks/to-do items — one row per task a user creates
        # completed is 0/1 (one-time tasks only — see task_completions below
        # for recurring tasks); recurring_day is "Monday".."Sunday", "daily",
        # or NULL for a one-time task.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                category TEXT,
                recurring_day TEXT,
                created_at REAL NOT NULL,
                completed_at REAL
            )
            """
        )

        # One row per time a RECURRING task ("walk the dog", every day/every
        # Wednesday) gets checked off. Recurring tasks never flip tasks.completed
        # to 1 permanently — a daily task marked done today has to reappear
        # unchecked tomorrow, not vanish forever like a one-time task would.
        # This table is both how "is this recurring task done for its current
        # period (today/this week)" gets answered (a row here within that
        # window) and where the completion-count statistics come from — a
        # streak/weekly-chart needs a full timestamped history, not just a
        # single completed_at column that the next occurrence would overwrite.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_completions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                completed_at REAL NOT NULL
            )
            """
        )

        # Browser push subscriptions (see push_service.py) — one row per
        # device/browser a user has granted notification permission on
        # (someone could enable this on both their phone and their laptop,
        # both should get pushed). endpoint is unique per subscription;
        # UNIQUE here means re-subscribing the same browser just overwrites
        # its row instead of accumulating duplicates that would double-send.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )

        # One row per time the model told a user it genuinely couldn't help
        # (see app.py's report_capability_gap tool). Written ONLY from that
        # live tool call -- a plain local insert, nothing else -- and read
        # back later by daily_briefing.py's own scheduler, which is the ONLY
        # code path that ever turns these into an actual email, batched and
        # on its own timer, through the ADMIN's own connected Gmail (same
        # mechanism as reminder/weekly-summary delivery below). That split
        # matters: nothing a live, untrusted conversation does can by itself
        # cause a real email to go out.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_gaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                user_request TEXT NOT NULL,
                reason TEXT NOT NULL,
                raw_text TEXT,
                created_at REAL NOT NULL,
                notified_at REAL
            )
            """
        )

        # Generic, send-free proactive notices -- "something worth noticing,
        # dismiss when seen", no drafting/sending state machine at all
        # (unlike important_email_suggestions). One row per detected thing;
        # dedupe_key is how each detector (daily_briefing.py) avoids writing
        # the same notice twice for the same occurrence (e.g.
        # "holiday:Passover:2027", "conflict:<event1>:<event2>:<date>",
        # "market:<date>") -- the same select-then-insert dedup pattern
        # important_email_suggestions already uses for gmail_message_id.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS system_notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                action_label TEXT,
                dedupe_key TEXT NOT NULL,
                created_at REAL NOT NULL,
                dismissed_at REAL
            )
            """
        )


@contextmanager
def _connect():
    if PG_CONFIGURED:
        # connect_timeout bounds the initial TCP/auth handshake; statement_timeout
        # (Postgres-server-enforced, not client-side) bounds actual QUERY
        # execution -- if a query runs past 8s, Postgres itself cancels it and
        # returns an error to the client, which is what libsql could never do
        # (see module docstring). This is the actual fix the whole Turso saga
        # was trying, and failing, to bolt on after the fact.
        #
        # Set via a SET command after connecting, not the `options=` startup
        # parameter -- confirmed directly that Neon's pooled connection
        # (PgBouncer, the default/recommended connection string) rejects
        # statement_timeout as a startup parameter outright ("unsupported
        # startup parameter"), since a pooler doesn't forward arbitrary
        # startup options to the underlying server the way a direct
        # connection does. A plain SQL SET works with pooled or direct
        # connections either way.
        raw_conn = psycopg2.connect(PG_DATABASE_URL, connect_timeout=10)
        raw_conn.cursor().execute("SET statement_timeout = 8000")
        conn = _PgConnection(raw_conn)
    else:
        # timeout=10: how long a call waits for a lock held by another thread
        # before raising "database is locked", instead of the 5s default —
        # gunicorn's --threads 16 means up to 16 real OS threads can hit this
        # file at once (see render.yaml's own docstring: threads went 2->16
        # after a similar pool-exhaustion incident with /api/stream). A
        # slightly longer wait here trades a bit of latency under contention
        # for not surfacing a raw OperationalError to the user over a write
        # that would have succeeded a moment later.
        conn = sqlite3.connect(DB_PATH, timeout=10)
        # WAL mode lets readers and a writer run concurrently instead of the
        # default rollback-journal mode's "writer blocks every reader" — the
        # actual fix for 16 threads sharing one file, timeout above is just a
        # safety net for the writer-vs-writer case WAL doesn't eliminate.
        # journal_mode is stored in the database file itself (not per
        # connection), so this is a one-time no-op after the first call ever
        # sets it — cheap enough to just always ask for it here rather than
        # only in _init_db(), which guarantees it even if the file already
        # existed from before this line was added.
        conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_user(user_id: str, email: str, name: str = "") -> None:
    """Called right after a successful Google sign-in. Creates the row on
    first login, just bumps last_login_at on every login after that."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, name, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                email = excluded.email,
                name = excluded.name,
                last_login_at = excluded.last_login_at
            """,
            (user_id, email, name, now, now),
        )


def get_user(user_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, email, name, google_token_json FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "email": row[1], "name": row[2], "google_token_json": row[3]}


def get_user_by_email(email: str) -> dict | None:
    """Case-insensitive lookup by the account's own email, for the rare
    server-side job that needs a SPECIFIC person's stored Google connection
    rather than whichever user is signed in right now -- daily_briefing.py's
    capability-gap digest, which always goes out through the admin's own
    consented Gmail regardless of which user's request triggered any given
    entry. Most recently active row wins on the off chance more than one
    exists."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, email, name, google_token_json FROM users "
            "WHERE LOWER(email) = LOWER(?) ORDER BY last_login_at DESC LIMIT 1",
            (email,),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "email": row[1], "name": row[2], "google_token_json": row[3]}


def save_google_token(user_id: str, token_json: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET google_token_json = ? WHERE id = ?", (token_json, user_id))


def get_google_token(user_id: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT google_token_json FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def clear_google_token(user_id: str) -> None:
    """Used by the per-user equivalent of the old disconnect() — drops the
    stored token without deleting the account row itself."""
    with _connect() as conn:
        conn.execute("UPDATE users SET google_token_json = NULL WHERE id = ?", (user_id,))


def list_connected_user_ids() -> list[str]:
    """Every user with a live Google token — i.e. everyone the weekly-summary
    scheduler should actually try to email. A row existing with no token
    (disconnected, or never finished connecting) is deliberately excluded."""
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM users WHERE google_token_json IS NOT NULL").fetchall()
    return [r[0] for r in rows]


def add_reminder(user_id: str, text: str, remind_at: float, recurrence: str | None = None,
                 emoji: str | None = None, flourish: str | None = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, text, remind_at, created_at, delivered, recurrence, emoji, flourish)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (user_id, text, remind_at, time.time(), recurrence, emoji or None, flourish or None),
        )
        return cur.lastrowid


def get_due_reminders(now: float) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, user_id, text, remind_at, recurrence, emoji, flourish"
            " FROM reminders WHERE delivered = 0 AND remind_at <= ?",
            (now,),
        ).fetchall()
    return [
        {"id": r[0], "user_id": r[1], "text": r[2], "remind_at": r[3], "recurrence": r[4],
         "emoji": r[5], "flourish": r[6]}
        for r in rows
    ]


def mark_reminder_delivered(reminder_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE reminders SET delivered = 1 WHERE id = ?", (reminder_id,))


def reschedule_reminder(reminder_id: int, next_remind_at: float) -> None:
    """For recurring reminders: advance to the next occurrence instead of
    marking delivered, so get_due_reminders picks it up again next time
    around rather than it firing once and going silent forever."""
    with _connect() as conn:
        conn.execute("UPDATE reminders SET remind_at = ? WHERE id = ?", (next_remind_at, reminder_id))


def list_active_reminders(user_id: str) -> list[dict]:
    """All not-yet-delivered reminders for a user, for the HUD's own list —
    recurring ones are always 'active' (delivered never goes to 1 for them,
    see reschedule_reminder), one-time ones drop off once sent."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, text, remind_at, recurrence, emoji, flourish"
            " FROM reminders WHERE user_id = ? AND delivered = 0 ORDER BY remind_at",
            (user_id,),
        ).fetchall()
    return [
        {"id": r[0], "text": r[1], "remind_at": r[2], "recurrence": r[3], "emoji": r[4], "flourish": r[5]}
        for r in rows
    ]


def update_reminder(reminder_id: int, user_id: str, text: str, remind_at: float, recurrence: str | None,
                    emoji: str | None = None, flourish: str | None = None) -> bool:
    """Scoped by user_id like delete_reminder. Resets delivered back to 0 —
    editing a one-time reminder that already fired (or is being pushed
    later than its original time) should reactivate it, not leave it
    silently marked done.

    emoji/flourish are passed through as given, including None — callers
    that only change the time are expected to carry the existing values
    forward themselves (see request_update_reminder), since "leave it
    alone" and "clear it" are both legitimate edits and this layer can't
    tell them apart."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE reminders SET text = ?, remind_at = ?, recurrence = ?, emoji = ?, flourish = ?,"
            " delivered = 0 WHERE id = ? AND user_id = ?",
            (text, remind_at, recurrence, emoji or None, flourish or None, reminder_id, user_id),
        )
        return cur.rowcount > 0


def delete_reminder(reminder_id: int, user_id: str) -> bool:
    """Scoped by user_id so one user can't cancel another's reminder by
    guessing an id — there's no ownership check anywhere else in this path
    since reminders never leave the confirm-to-act-free 'note to self'
    category (see request_set_reminder's docstring), but deletion is
    destructive enough that the scope check earns its keep here."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))
        return cur.rowcount > 0


def save_push_subscription(user_id: str, endpoint: str, p256dh: str, auth: str) -> None:
    """INSERT OR REPLACE on the endpoint's own uniqueness (not an explicit
    id) — the browser is the source of truth for what its subscription is;
    if it hands us the same endpoint again (re-registering the service
    worker, e.g. after clearing site data then re-granting permission) this
    just refreshes the keys instead of erroring on the UNIQUE constraint."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, auth=excluded.auth
            """,
            (user_id, endpoint, p256dh, auth, time.time()),
        )


def get_push_subscriptions(user_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return [{"endpoint": r[0], "p256dh": r[1], "auth": r[2]} for r in rows]


def delete_push_subscription(endpoint: str) -> None:
    """Not scoped by user_id, unlike delete_reminder — this is called from
    push_service.py after the push provider itself reports the endpoint is
    gone (410/404, e.g. the user cleared site data or uninstalled the
    browser profile), where the caller only ever has the endpoint on hand,
    not which user it belonged to. The endpoint itself is an unguessable
    provider-issued URL, not a small guessable integer id like reminder ids
    are, so this doesn't carry the same cross-user risk delete_reminder's
    scoping exists to prevent."""
    with _connect() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))


# ---------------------------------------------------------------------------
# Capability gaps — a local log only. See the capability_gaps table comment
# (sqlite branch of _init_db) for why the write side never touches email.
# ---------------------------------------------------------------------------
def log_capability_gap(user_id: str, user_request: str, reason: str, raw_text: str = "") -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO capability_gaps (user_id, user_request, reason, raw_text, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, user_request, reason, raw_text or None, time.time()),
        )
        return cur.lastrowid


def get_unnotified_capability_gaps(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, user_id, user_request, reason, raw_text, created_at FROM capability_gaps"
            " WHERE notified_at IS NULL ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"id": r[0], "user_id": r[1], "user_request": r[2], "reason": r[3], "raw_text": r[4], "created_at": r[5]}
        for r in rows
    ]


def mark_capability_gaps_notified(ids: list[int]) -> None:
    if not ids:
        return
    with _connect() as conn:
        for gap_id in ids:  # small batches (limit=20 above) — a plain loop beats building a per-backend IN (...) list
            conn.execute("UPDATE capability_gaps SET notified_at = ? WHERE id = ?", (time.time(), gap_id))


def add_task(user_id: str, text: str, category: str | None = None, recurring_day: str | None = None) -> int:
    """Add a new task for a user. recurring_day: "Monday", "Tuesday", etc for weekly tasks."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (user_id, text, category, recurring_day, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, text, category or None, recurring_day or None, time.time()),
        )
        return cur.lastrowid


def get_user_tasks(user_id: str, include_completed: bool = False) -> list[dict]:
    """Get all tasks for a user. By default excludes completed ones."""
    completed_filter = "" if include_completed else "AND completed = 0"
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT id, text, completed, category, recurring_day, created_at, completed_at "
            f"FROM tasks WHERE user_id = ? {completed_filter} ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [
        {"id": r[0], "text": r[1], "completed": r[2], "category": r[3], "recurring_day": r[4],
         "created_at": r[5], "completed_at": r[6]}
        for r in rows
    ]


def mark_task_complete(task_id: int, user_id: str) -> bool:
    """Mark a task complete. Scoped by user_id for safety."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET completed = 1, completed_at = ? WHERE id = ? AND user_id = ?",
            (time.time(), task_id, user_id),
        )
        return cur.rowcount > 0


def delete_task(task_id: int, user_id: str) -> bool:
    """Delete a task. Scoped by user_id for safety."""
    with _connect() as conn:
        cur = conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
        return cur.rowcount > 0


def update_task(task_id: int, user_id: str, text: str | None = None,
                category: str | None = None, recurring_day: str | None = None) -> bool:
    """Update a task's details. Omitted fields are left unchanged."""
    updates = []
    params = []
    if text is not None:
        updates.append("text = ?")
        params.append(text)
    if category is not None:
        updates.append("category = ?")
        params.append(category)
    if recurring_day is not None:
        updates.append("recurring_day = ?")
        params.append(recurring_day)
    if not updates:
        return False
    params.extend([task_id, user_id])
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params,
        )
        return cur.rowcount > 0


def get_task(task_id: int, user_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, text, completed, category, recurring_day, created_at, completed_at"
            " FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "text": row[1], "completed": row[2], "category": row[3],
            "recurring_day": row[4], "created_at": row[5], "completed_at": row[6]}


def record_task_completion(task_id: int, user_id: str) -> None:
    """Log one check-off of a RECURRING task. Unlike mark_task_complete,
    this never touches tasks.completed — a recurring task has to reappear
    unchecked next period (tomorrow for daily, next week for weekly), not
    vanish the way a one-time task does."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO task_completions (task_id, user_id, completed_at) VALUES (?, ?, ?)",
            (task_id, user_id, time.time()),
        )


def get_last_completion(task_id: int) -> float | None:
    """Most recent check-off timestamp for a recurring task, or None if it's
    never been done. Used to decide whether it's already done for the
    current period (today/this week) — see productivity_service.py."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT MAX(completed_at) FROM task_completions WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return row[0] if row and row[0] is not None else None


def get_completion_timestamps_since(user_id: str, since: float) -> list[float]:
    """Every individual completion timestamp (one-time tasks via
    tasks.completed_at, recurring check-offs via task_completions) since
    `since`, unsorted and un-bucketed — ONE connection, two queries,
    regardless of how many time windows the caller needs.

    This replaced a version (count_completions_between) that the caller —
    get_task_stats — used to call once per window (today/week/each of the
    last 7 days = 9 separate connections, 18 queries, for a single stats
    request). Under this app's concurrency model (Render free tier,
    gunicorn --workers 1 --threads 16 — one process, sixteen real OS
    threads sharing one SQLite file with no WAL mode and no explicit busy
    timeout), nine sequential connection/lock attempts per request was a
    real contention risk: a burst of concurrent requests (exactly what
    happens when someone is actively trying out a brand-new feature) could
    pile up threads waiting on SQLite's default rollback-journal locking
    long enough to make the whole worker unresponsive — including to
    Render's own internal health check, which is what makes Render's
    routing layer conclude the instance is dead. One connection per stats
    request removes that amplification; the caller buckets these
    timestamps into today/week/daily counts in plain Python instead."""
    with _connect() as conn:
        onetime = conn.execute(
            "SELECT completed_at FROM tasks WHERE user_id = ? AND completed = 1 AND completed_at >= ?",
            (user_id, since),
        ).fetchall()
        recurring = conn.execute(
            "SELECT completed_at FROM task_completions WHERE user_id = ? AND completed_at >= ?",
            (user_id, since),
        ).fetchall()
    return [r[0] for r in onetime] + [r[0] for r in recurring]


# ---------------------------------------------------------------------------
# Important-email suggestions — settings (two columns on `users`, not a
# generic settings table: this is the one feature that needs server-side
# persistence at all, since a background thread can't read the HUD's own
# localStorage-based "settings") plus the per-message suggestion rows.
# ---------------------------------------------------------------------------
def get_important_suggestions_settings(user_id: str) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT important_suggestions_enabled, important_senders, important_suggestions_enabled_at"
            " FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {"enabled": False, "senders": [], "enabled_at": None}
    return {
        "enabled": bool(row[0]),
        "senders": [s for s in (row[1] or "").split(",") if s],
        "enabled_at": row[2],
    }


def set_important_suggestions_settings(user_id: str, enabled: bool, senders: list[str]) -> None:
    """senders: already-validated, lowercased email addresses (app.py
    normalizes and caps at 5 before calling this). important_suggestions_enabled_at
    is re-stamped on every OFF->ON transition, not just the very first time
    ever — so re-enabling after a pause never backfills mail that arrived
    while it was off. See _check_important_emails's own use of this value."""
    current = get_important_suggestions_settings(user_id)
    joined = ",".join(senders[:5]) if senders else None
    newly_enabled = enabled and not current["enabled"]
    with _connect() as conn:
        if newly_enabled:
            conn.execute(
                "UPDATE users SET important_suggestions_enabled = ?, important_senders = ?,"
                " important_suggestions_enabled_at = ? WHERE id = ?",
                (1 if enabled else 0, joined, time.time(), user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET important_suggestions_enabled = ?, important_senders = ? WHERE id = ?",
                (1 if enabled else 0, joined, user_id),
            )


def list_important_suggestion_candidates() -> list[dict]:
    """Every user with the toggle on AND a connected Google token — mirrors
    list_connected_user_ids()'s WHERE clause plus the new enabled flag, so
    the scheduler never needs a separate per-user is-connected check."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, important_senders, important_suggestions_enabled_at FROM users"
            " WHERE important_suggestions_enabled = 1 AND google_token_json IS NOT NULL"
        ).fetchall()
    return [
        {"user_id": r[0], "senders": [s for s in (r[1] or "").split(",") if s], "enabled_at": r[2]}
        for r in rows
    ]


def add_important_suggestion(user_id: str, gmail_message_id: str, sender_name: str, sender_email: str,
                             subject: str, snippet: str, date_ms: int) -> int | None:
    """Silently no-ops (returns None) if this exact message already has a
    row for this user — the scheduler re-scans the same recent-mail window
    every cycle, so this dedup is what keeps it from creating duplicate
    suggestions for a message it's already seen."""
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM important_email_suggestions WHERE user_id = ? AND gmail_message_id = ?",
            (user_id, gmail_message_id),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO important_email_suggestions"
            " (user_id, gmail_message_id, sender_name, sender_email, subject, snippet, date_ms, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, gmail_message_id, sender_name or None, sender_email, subject or None,
             snippet or None, date_ms, time.time()),
        )
        return cur.lastrowid


def list_suggestions_needing_draft() -> list[dict]:
    """Every still-'pending' row, across all users — the scheduler's own
    drafting pass picks these up and either advances them to 'drafted' (see
    set_suggestion_draft) or leaves them 'pending' on a transient failure,
    to retry next cycle."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, user_id, sender_name, sender_email, subject, snippet"
            " FROM important_email_suggestions WHERE status = 'pending'"
        ).fetchall()
    return [
        {"id": r[0], "user_id": r[1], "sender_name": r[2], "sender_email": r[3], "subject": r[4], "snippet": r[5]}
        for r in rows
    ]


def set_suggestion_draft(suggestion_id: int, draft_subject: str, draft_body: str) -> None:
    """Called on a SUCCESSFUL draft only — advances 'pending' -> 'drafted'.
    This never sends anything by itself: the user still has to tap "send"
    in the Inbox tab, which goes through the exact same confirm-to-act path
    (productivity_service.request_send_email) every other write in this app
    already uses — see app.py's /api/inbox/suggestions/<id>/send. Not scoped
    by user_id -- this runs from the background scheduler, not a live
    per-user request."""
    with _connect() as conn:
        conn.execute(
            "UPDATE important_email_suggestions SET draft_subject = ?, draft_body = ?, draft_error = NULL,"
            " status = 'drafted' WHERE id = ?",
            (draft_subject, draft_body, suggestion_id),
        )


def record_suggestion_draft_error(suggestion_id: int, draft_error: str) -> None:
    """A failed drafting attempt (e.g. Groq rate-limited) -- status stays
    'pending' on purpose, so the next scheduler cycle just retries it rather
    than needing its own separate retry bookkeeping. draft_error is kept
    only as a diagnostic breadcrumb."""
    with _connect() as conn:
        conn.execute(
            "UPDATE important_email_suggestions SET draft_error = ? WHERE id = ?",
            (draft_error, suggestion_id),
        )


def list_pending_suggestions(user_id: str) -> list[dict]:
    """Everything NOT YET resolved -- both freshly-detected ('pending', no
    draft yet) and already-drafted ('drafted', awaiting the user's own
    send/dismiss decision) rows, so the Inbox tab can show "preparing a
    draft..." as well as the ready-to-send state."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, sender_name, sender_email, subject, snippet, date_ms,"
            " draft_subject, draft_body, status"
            " FROM important_email_suggestions WHERE user_id = ? AND status != 'resolved'"
            " ORDER BY date_ms DESC",
            (user_id,),
        ).fetchall()
    return [
        {"id": r[0], "sender_name": r[1], "sender_email": r[2], "subject": r[3], "snippet": r[4], "date_ms": r[5],
         "draft_subject": r[6], "draft_body": r[7], "status": r[8]}
        for r in rows
    ]


def get_suggestion(suggestion_id: int, user_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, sender_name, sender_email, subject, snippet, date_ms,"
            " draft_subject, draft_body, status"
            " FROM important_email_suggestions WHERE id = ? AND user_id = ?",
            (suggestion_id, user_id),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "sender_name": row[1], "sender_email": row[2], "subject": row[3],
            "snippet": row[4], "date_ms": row[5], "draft_subject": row[6], "draft_body": row[7],
            "status": row[8]}


def mark_suggestion_resolved(suggestion_id: int, user_id: str, resolution: str) -> bool:
    """resolution records what actually happened ('sent'/'cancelled'/'dismissed'),
    not a guarantee from Gmail's side beyond what google_service.send_email's
    own return string already told the caller (see its docstring -- it never
    raises, it returns an error SENTENCE on failure). The WHERE status !=
    'resolved' guard makes this a safe no-op if called twice for the same
    row (e.g. a double click) without clobbering whichever resolution landed
    first."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE important_email_suggestions SET status = 'resolved', resolution = ?, resolved_at = ?"
            " WHERE id = ? AND user_id = ? AND status != 'resolved'",
            (resolution, time.time(), suggestion_id, user_id),
        )
        return cur.rowcount > 0


def count_suggestions_created_since(user_id: str, since: float) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM important_email_suggestions WHERE user_id = ? AND created_at >= ?",
            (user_id, since),
        ).fetchone()
    return row[0] if row else 0


def add_system_notice(user_id: str, category: str, title: str, body: str, dedupe_key: str,
                       action_label: str | None = None) -> int | None:
    """Generic, send-free suggestion card for any category that's just
    "notice + dismiss" (holiday greetings, calendar conflicts, market
    swings, ...) -- no drafting/sending state machine, unlike
    important_email_suggestions. Silently no-ops (returns None) if this
    exact dedupe_key already has a row for this user, same dedup pattern
    add_important_suggestion already uses for gmail_message_id -- each
    background detector in daily_briefing.py is free to re-scan its
    signal every cycle without ever creating a duplicate notice."""
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM system_notices WHERE user_id = ? AND dedupe_key = ?",
            (user_id, dedupe_key),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO system_notices (user_id, category, title, body, action_label, dedupe_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, category, title, body, action_label, dedupe_key, time.time()),
        )
        return cur.lastrowid


def list_active_notices(user_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, category, title, body, action_label, created_at"
            " FROM system_notices WHERE user_id = ? AND dismissed_at IS NULL"
            " ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [
        {"id": r[0], "category": r[1], "title": r[2], "body": r[3], "action_label": r[4], "created_at": r[5]}
        for r in rows
    ]


def dismiss_notice(notice_id: int, user_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE system_notices SET dismissed_at = ? WHERE id = ? AND user_id = ? AND dismissed_at IS NULL",
            (time.time(), notice_id, user_id),
        )
        return cur.rowcount > 0


_init_db()
