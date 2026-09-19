"""
users.py — per-user account storage for the multi-tenant cloud build.

Replaces google_service.py's single .google_cache/token.json (one shared
account) with one row per signed-in user, keyed by their Google account's
own id. SQLite via the stdlib — no new dependency, and Render/Railway/Fly
all support a small persistent-disk SQLite file for a project at this scale
(a real Postgres is a reasonable later upgrade, not a phase-1 requirement).
"""

from __future__ import annotations  # PEP 604 `X | None` hints, running on Python 3.9

import logging
import os
import sqlite3
import time
from contextlib import contextmanager

logger = logging.getLogger("jarvis.users")

DB_PATH = os.environ.get("JARVIS_DB_PATH", "users.db")

# Render's free tier (see render.yaml's own note) has no persistent disk —
# the sqlite3 file above gets wiped on every restart/spin-down, which is
# what made tasks/reminders vanish unpredictably rather than any per-device
# bug. TURSO_DATABASE_URL/TURSO_AUTH_TOKEN switch to Turso (hosted, free,
# no card, SQLite-compatible) when set; local dev with no such env vars is
# completely unaffected — same sqlite3 file as always.
TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
TURSO_CONFIGURED = bool(TURSO_URL and TURSO_AUTH_TOKEN)

if TURSO_CONFIGURED:
    import libsql

# A network-level timeout guard was tried here (three separate attempts:
# a thread-based wrapper, a subprocess-based circuit breaker, then a
# hard-fail variant of that breaker) and every one of them introduced a
# new, worse, immediately-reproducible bug of its own -- a runaway
# multiprocessing fork bomb, a split-brain where a write landed in Turso
# and a later read silently landed in local sqlite instead (indistinguishable
# from data loss), and finally a health-probe false negative that crashed
# the entire app at boot. Reverted to the simplest possible version: a
# direct connection, no probe, no circuit breaker, no fallback. This
# accepts the original, small, well-understood risk (a genuine multi-
# minute Turso network stall can still hang a request long enough to trip
# gunicorn's own --timeout and take the worker down) in exchange for
# removing three compounding, actively-worse bugs the "fix" for that risk
# kept introducing. The keep-alive ping (see app.py's /health route +
# .github/workflows/keep-alive.yml) already addresses the specific trigger
# that caused the one real incident of this (the service going idle, then
# cold-starting under a real request) — the residual risk here is now a
# genuine, rare Turso-side outage, not a routine occurrence.


def _init_db():
    with _connect() as conn:
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
        # remind_at. NOTE same caveat as the users table: on Render's free
        # tier this file is wiped on every restart/spin-down, so a reminder
        # set for later can silently vanish if the server recycles before
        # it fires. Real persistence needs a durable store (e.g. Render's
        # free Postgres), not this ephemeral SQLite file.
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
        # idempotent. Catches plain Exception, not sqlite3.OperationalError
        # specifically — confirmed directly that Turso's libsql raises a
        # bare ValueError (wrapping a Hrana protocol error) for the exact
        # same "duplicate column" condition, not sqlite3's own exception
        # type, so a narrower catch here would crash _init_db() on first
        # boot against a fresh Turso database.
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
                pass  # column already exists — same cross-backend note as above

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


@contextmanager
def _connect():
    if TURSO_CONFIGURED:
        # Remote HTTP connection — no local file, so no lock-contention
        # timeout or journal-mode PRAGMA applies (those are local-file
        # SQLite concepts; Turso's server handles concurrency itself).
        # libsql.Connection implements the same .execute/.commit/.close
        # DB-API surface as sqlite3.Connection, so every query elsewhere
        # in this file works unchanged. See the module docstring above for
        # why this is a plain, unprotected connect — every attempt to add
        # a safety net around it caused a worse problem than the one it
        # was meant to prevent.
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_AUTH_TOKEN)
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


_init_db()
