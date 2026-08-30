"""
daily_briefing.py — background email delivery: due reminders + weekly
calendar summary.

MULTI-USER BUILD: proactive notifications can't reuse the live SSE stream
(event_stream.py) the way a single-tenant assistant would — SSE only
reaches a browser tab that happens to be open right now, and by definition
these fire when nobody's necessarily looking at the HUD. Delivered as real
email instead, sent through each user's own Gmail connection
(google_service.send_email), which reaches them whether or not the HUD is
open. This replaces the old single-account version, which pushed one
global agenda over SSE to every connected tab — meaningless once there's
more than one user's calendar involved, and it never actually ran in
production anyway (see start_scheduler's old docstring / git history):
it was only ever started from the `if __name__ == "__main__"` block, which
gunicorn (Render's `gunicorn app:app`) never executes.

One background thread, checked once a minute:
  - reminders: any due (users.get_due_reminders), emailed and marked
    delivered.
  - weekly summary: once a week (JARVIS_WEEKLY_SUMMARY_WEEKDAY/HOUR/MINUTE,
    default Sunday 08:00 local), emailed to every currently-connected user
    (users.list_connected_user_ids) — their own week ahead.

Same free-tier caveat as users.py's docstring: users.db is wiped on every
restart/spin-down, so a reminder set for later can silently vanish if the
server recycles before it fires. Real persistence needs a durable store
(e.g. Render's free Postgres), not this ephemeral SQLite file — worth
knowing before relying on this for anything time-critical.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta

import google_service
import productivity_service
import push_service
import users
import whatsapp_service

logger = logging.getLogger("jarvis.daily_briefing")

WEEKLY_SUMMARY_ENABLED = os.environ.get("JARVIS_WEEKLY_SUMMARY", "true").lower() == "true"
# Python's datetime.weekday(): Monday=0 ... Sunday=6.
WEEKLY_SUMMARY_WEEKDAY = int(os.environ.get("JARVIS_WEEKLY_SUMMARY_WEEKDAY", "6"))  # Sunday
WEEKLY_SUMMARY_HOUR = int(os.environ.get("JARVIS_WEEKLY_SUMMARY_HOUR", "8"))
WEEKLY_SUMMARY_MINUTE = int(os.environ.get("JARVIS_WEEKLY_SUMMARY_MINUTE", "0"))

_CHECK_INTERVAL_SECONDS = 60


def _send_user_email(user_id: str, subject: str, body: str) -> None:
    user = users.get_user(user_id)
    if not user or not google_service.is_connected(user_id):
        logger.info(f"Skipping email to user {user_id} — not connected.")
        return
    result = google_service.send_email(user_id, user["email"], subject, body)
    logger.info(f"Email '{subject}' to user {user_id}: {result}")


def _check_due_reminders():
    for reminder in users.get_due_reminders(time.time()):
        # First channel the user actually has set up wins, never more than
        # one — no new preference to manage, since enabling a channel in
        # Settings is itself what turns the others off for future reminders.
        # Order is by "most likely to actually surface it right now": push
        # is a real device notification, WhatsApp arrives inside an app
        # people already check constantly, email is the fallback for anyone
        # who set up neither.
        user_id = reminder["user_id"]
        has_push = push_service.CONFIGURED and bool(users.get_push_subscriptions(user_id))
        whatsapp_settings = None if has_push else users.get_whatsapp_settings(user_id)

        if has_push:
            push_service.send_push(user_id, "תזכורת מ-J.A.R.V.I.S", reminder["text"])
        elif whatsapp_settings:
            ok, message = whatsapp_service.send_whatsapp(
                whatsapp_settings["phone"], whatsapp_settings["apikey"],
                f"⏰ תזכורת מ-J.A.R.V.I.S: {reminder['text']}",
            )
            if not ok:
                logger.error(f"WhatsApp delivery failed for reminder {reminder['id']}: {message}")
        else:
            try:
                _send_user_email(
                    user_id,
                    "Reminder from J.A.R.V.I.S.",
                    f"Sir, this is your reminder: {reminder['text']}",
                )
            except Exception as e:
                logger.error(f"Failed to deliver reminder {reminder['id']}: {e}")

        # Marked delivered even on total failure (e.g. token revoked
        # meanwhile) — a broken reminder retried forever every minute isn't
        # better than one that silently didn't go out once. Recurring
        # reminders (recurrence set) are the one exception: instead of a
        # dead end, advance remind_at to next week's occurrence so it keeps
        # firing on schedule instead of going silent after the first time.
        if reminder["recurrence"]:
            try:
                weekday, hour, minute = (int(p) for p in reminder["recurrence"].split(":"))
                next_fire = productivity_service.next_weekday_occurrence(weekday, hour, minute)
                users.reschedule_reminder(reminder["id"], next_fire.timestamp())
            except (ValueError, TypeError) as e:
                logger.error(f"Malformed recurrence on reminder {reminder['id']}: {e}")
                users.mark_reminder_delivered(reminder["id"])
        else:
            users.mark_reminder_delivered(reminder["id"])


def _next_weekly_fire(after: datetime) -> datetime:
    candidate = after.replace(hour=WEEKLY_SUMMARY_HOUR, minute=WEEKLY_SUMMARY_MINUTE, second=0, microsecond=0)
    days_ahead = (WEEKLY_SUMMARY_WEEKDAY - candidate.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= after:
        candidate += timedelta(days=7)
    return candidate


def _send_weekly_summaries():
    for user_id in users.list_connected_user_ids():
        try:
            text = productivity_service.get_weekly_summary_text(user_id)
            _send_user_email(user_id, "Your week ahead — J.A.R.V.I.S.", text)
        except Exception as e:
            logger.error(f"Weekly summary failed for user {user_id}: {e}")


def _scheduler_loop():
    next_weekly_fire = None
    if WEEKLY_SUMMARY_ENABLED:
        next_weekly_fire = _next_weekly_fire(datetime.now(productivity_service.LOCAL_TZ))
        logger.info(f"Weekly summary scheduled for {next_weekly_fire.strftime('%Y-%m-%d %H:%M')}.")

    while True:
        time.sleep(_CHECK_INTERVAL_SECONDS)

        try:
            _check_due_reminders()
        except Exception as e:
            logger.error(f"Reminder check failed: {e}")

        if next_weekly_fire is not None:
            now = datetime.now(productivity_service.LOCAL_TZ)
            if now >= next_weekly_fire:
                try:
                    _send_weekly_summaries()
                except Exception as e:
                    logger.error(f"Weekly summary run failed: {e}")
                next_weekly_fire = _next_weekly_fire(now)
                logger.info(f"Next weekly summary scheduled for {next_weekly_fire.strftime('%Y-%m-%d %H:%M')}.")


def start_scheduler():
    """Call once at import time (see app.py — must not be gated behind
    `if __name__ == "__main__"`, gunicorn never runs that). Reminders always
    run since set_reminder is an explicit per-call user request; only the
    unsolicited weekly summary has an opt-out (JARVIS_WEEKLY_SUMMARY=false)."""
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()
    logger.info("Background scheduler started (due reminders" + (" + weekly summary" if WEEKLY_SUMMARY_ENABLED else "") + ").")
