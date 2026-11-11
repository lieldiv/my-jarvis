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
from email.utils import parseaddr

import google_service
import productivity_service
import push_service
import users

logger = logging.getLogger("jarvis.daily_briefing")

WEEKLY_SUMMARY_ENABLED = os.environ.get("JARVIS_WEEKLY_SUMMARY", "true").lower() == "true"
# Python's datetime.weekday(): Monday=0 ... Sunday=6.
WEEKLY_SUMMARY_WEEKDAY = int(os.environ.get("JARVIS_WEEKLY_SUMMARY_WEEKDAY", "6"))  # Sunday
WEEKLY_SUMMARY_HOUR = int(os.environ.get("JARVIS_WEEKLY_SUMMARY_HOUR", "8"))
WEEKLY_SUMMARY_MINUTE = int(os.environ.get("JARVIS_WEEKLY_SUMMARY_MINUTE", "0"))

# Same env var name app.py reads independently (this module can't import
# app.py — app.py imports THIS module, see the top of this file — so each
# side just reads its own copy of the same setting).
ADMIN_ALERT_EMAIL = os.environ.get("JARVIS_ADMIN_EMAIL", "lieldiv28@gmail.com").strip()
CAPABILITY_GAP_DIGEST_LIMIT = 20  # per email; also caps how many rows one cycle marks notified

# Safety cap, not a quality knob: the mitigation for a misconfigured/spoofed
# address suddenly sending a burst, or a mailing list accidentally matching
# one of a user's own configured senders. Checked BEFORE issuing the Gmail
# query for that user, so a maxed-out user doesn't even cost an extra API
# call this cycle.
IMPORTANT_EMAIL_DAILY_LIMIT = int(os.environ.get("JARVIS_IMPORTANT_EMAIL_DAILY_LIMIT", "5"))

_CHECK_INTERVAL_SECONDS = 60


def _send_user_email(user_id: str, subject: str, body: str) -> bool:
    """Returns whether the send actually succeeded. google_service.send_email()
    never raises -- it catches every Gmail-API/network failure itself and
    returns an English error SENTENCE instead (see its own docstring) -- so a
    bare `try/except` around a call to this function can never observe a
    failed send; only this return value can. Most callers here (reminders,
    the weekly summary) deliberately ignore it and mark their own work done
    regardless, on purpose -- see _check_due_reminders' comment on why a
    broken reminder retried forever isn't better than one that silently
    didn't arrive once. _check_capability_gaps is the one caller that must
    NOT ignore it (found by a review agent, reproduced directly: a mocked
    Gmail failure still left a row marked "notified")."""
    user = users.get_user(user_id)
    if not user or not google_service.is_connected(user_id):
        logger.info(f"Skipping email to user {user_id} — not connected.")
        return False
    result = google_service.send_email(user_id, user["email"], subject, body)
    logger.info(f"Email '{subject}' to user {user_id}: {result}")
    return result.startswith("Sent the email")


def _reminder_notification(reminder: dict) -> tuple:
    """(title, body) for one due reminder. The emoji/flourish were written
    by the LLM back when the user ASKED for the reminder (see app.py's
    set_reminder tool schema) — nothing here calls an LLM, deliberately, so
    an expired/rate-limited Groq key can't stop reminders going out. Both
    are optional and fall back cleanly: a reminder saved before this
    existed, or one the model didn't decorate, still delivers as plain
    text rather than showing a stray separator or an empty title."""
    text = reminder["text"]
    emoji = (reminder.get("emoji") or "").strip()
    flourish = (reminder.get("flourish") or "").strip()

    title = f"{emoji} {text}".strip() if emoji else text
    body = flourish or "תזכורת מ-J.A.R.V.I.S"
    return title, body


def _check_due_reminders():
    for reminder in users.get_due_reminders(time.time()):
        # Push if the user has it set up (an actual device notification,
        # what they actually asked for) — email only when push isn't set up
        # at all, OR when it was but silently failed (provider outage, a
        # subscription expired without yet 404ing). This used to be a
        # plain if/else on "has a subscription", which meant a push that
        # was ATTEMPTED but failed left the user with nothing at all —
        # contradicting send_push()'s own stated contract that a push
        # failure should never be the reason a reminder doesn't arrive.
        has_push = push_service.CONFIGURED and bool(users.get_push_subscriptions(reminder["user_id"]))
        title, body = _reminder_notification(reminder)
        push_delivered = False
        if has_push:
            push_delivered = push_service.send_push(
                reminder["user_id"], title, body,
                tag=f"jarvis-reminder-{reminder['id']}",
            )
        if not push_delivered:
            try:
                _send_user_email(
                    reminder["user_id"],
                    f"{title} — J.A.R.V.I.S",
                    f"{title}\n\n{body}",
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


def _check_capability_gaps():
    """The other half of app.py's report_capability_gap tool: that tool only
    ever writes a local row (users.log_capability_gap) from the live,
    untrusted request path. THIS is the only place that turns unread rows
    into an actual email — on this module's own per-minute timer, batched,
    through the ADMIN's own connected Gmail via _send_user_email, exactly
    like a reminder or the weekly summary below. If the admin has never
    signed into this server themselves, there's no stored token to send
    through — skip quietly (matches _send_user_email's own "not connected"
    behavior) rather than erroring every minute forever."""
    gaps = users.get_unnotified_capability_gaps(limit=CAPABILITY_GAP_DIGEST_LIMIT)
    if not gaps:
        return
    admin = users.get_user_by_email(ADMIN_ALERT_EMAIL)
    if not admin or not google_service.is_connected(admin["id"]):
        logger.info(f"Capability-gap digest skipped — {ADMIN_ALERT_EMAIL} isn't connected on this server.")
        return

    lines = []
    for g in gaps:
        reporter = users.get_user(g["user_id"]) or {}
        when = datetime.fromtimestamp(g["created_at"], productivity_service.LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"- {when} — {reporter.get('email', g['user_id'])}\n"
            f"  Asked for: {g['user_request']}\n"
            f"  Why not: {g['reason']}\n"
            f"  Raw message: {(g['raw_text'] or '')[:300]}"
        )
    body = f"{len(gaps)} request(s) JARVIS couldn't help with:\n\n" + "\n\n".join(lines)
    try:
        sent = _send_user_email(admin["id"], "JARVIS: things I couldn't do — digest", body)
    except Exception as e:
        sent = False
        logger.error(f"Capability-gap digest raised: {e}")
    if sent:
        users.mark_capability_gaps_notified([g["id"] for g in gaps])
    else:
        logger.warning("Capability-gap digest did not send — rows left unmarked, retried next cycle.")


def _check_important_emails():
    """Detection only — this NEVER calls the LLM and NEVER sends anything.
    Drafting and sending both stay inside live, user-present requests (see
    app.py's /api/inbox/suggestions/<id>/review, which is where the actual
    confirm-to-act send happens) — this job's entire job is noticing a
    matching message exists and writing one local row for it.

    Why no LLM-based "is this important" judgment: an address match is
    free (zero Groq tokens against the shared rate-limited budget this
    scheduler cycle already runs other work under), deterministic, and
    fully explainable to the user ("this fired because you listed this
    address") — an LLM asked to judge importance from subject/snippet alone
    would either over-trigger or need its own prompt-tuning risk, for a
    job that runs every cycle for every connected user.
    """
    for cand in users.list_important_suggestion_candidates():
        user_id = cand["user_id"]
        senders = cand["senders"]
        if not senders:
            continue
        if users.count_suggestions_created_since(user_id, time.time() - 86400) >= IMPORTANT_EMAIL_DAILY_LIMIT:
            continue
        query = "(" + " OR ".join(f"from:{addr}" for addr in senders) + ") newer_than:2d"
        try:
            matches = google_service.list_recent_emails(user_id, max_results=5, query=query)
        except Exception as e:
            logger.error(f"Important-email check failed for user {user_id}: {e}")
            continue
        if not matches:
            continue
        for m in matches:
            # Never backfill mail that predates (the most recent time) the
            # user turned this on — see users.set_important_suggestions_settings's
            # own docstring. Without this, enabling the toggle for a sender
            # who already emailed 40 times would dump 40 surprise drafts at
            # once on the very first cycle.
            enabled_at = cand["enabled_at"]
            if enabled_at and m.get("date_ms", 0) < enabled_at * 1000:
                continue
            name, addr = parseaddr(m.get("sender", ""))
            try:
                users.add_important_suggestion(
                    user_id,
                    gmail_message_id=m.get("id", ""),
                    sender_name=name or addr,
                    sender_email=addr or m.get("sender", ""),
                    subject=m.get("subject", ""),
                    snippet=m.get("snippet", ""),
                    date_ms=m.get("date_ms", 0),
                )
            except Exception as e:
                logger.error(f"Failed to record important-email suggestion for user {user_id}: {e}")


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

        try:
            _check_capability_gaps()
        except Exception as e:
            logger.error(f"Capability-gap digest check failed: {e}")

        # After the two time-sensitive checks above, not before: this is the
        # one job that can make a Gmail API call per candidate user, so it
        # shouldn't be first in line every single cycle.
        try:
            _check_important_emails()
        except Exception as e:
            logger.error(f"Important-email check failed: {e}")

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
