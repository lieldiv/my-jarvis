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
  - important-email suggestions: detects a new email from a user's own
    configured "important" sender (Gmail search, no LLM), and drafts a
    short conservative holding reply for it (a small, independent Groq
    client -- see below). It never sends anything by itself: the draft just
    sits in the Inbox tab until the user taps "send", which goes through
    the exact same confirm-to-act path (productivity_service.request_send_email)
    every other write in this app already uses -- the product owner was
    explicit that this feature must never send without that approval. This
    is the one place in this whole file an LLM call happens; everything
    else in this module is either LLM-free or a plain notification.
  - system notices (holiday greeting, meeting conflicts, market swings):
    all three are zero-LLM and deterministic, each writing a generic,
    dismiss-only `system_notices` row (users.add_system_notice) rendered
    in the HUD's "הצעות מערכת" tab. The holiday greeting is self-facing
    only -- it never sends anything to anyone else, by explicit product-
    owner instruction; it just wishes the signed-in user a happy holiday.

Same free-tier caveat as users.py's docstring: users.db is wiped on every
restart/spin-down, so a reminder set for later can silently vanish if the
server recycles before it fires. Real persistence needs a durable store
(e.g. Render's free Postgres), not this ephemeral SQLite file — worth
knowing before relying on this for anything time-critical.
"""

import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from groq import Groq, RateLimitError

import google_service
import groq_pacer
import productivity_service
import push_service
import stocks_service
import users

logger = logging.getLogger("jarvis.daily_briefing")

# A small, independent Groq client -- can't reuse app.py's (app.py imports
# THIS module, so the reverse would be circular; see the module docstring).
# Same api key/model/timeout/retry shape as app.py's own client, and the
# SAME groq_pacer instance (a plain module, no circularity there) so this
# genuinely shares the one rate-limit budget with every live user request
# rather than quietly exceeding it from a second, uncoordinated client.
_GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
_groq_client = Groq(api_key=_GROQ_API_KEY, timeout=30.0, max_retries=1) if _GROQ_API_KEY else None
_IMPORTANT_EMAIL_MODEL = "openai/gpt-oss-20b"  # matches app.py's own MODEL_NAME

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
    Drafting (_draft_important_suggestions) happens later in this same
    scheduler, automatically; sending only ever happens from a live request
    once the user taps "send" (app.py's /api/inbox/suggestions/<id>/send) —
    this job's entire job is noticing a matching message exists and writing
    one local row for it.

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


# Same small, self-contained pattern as app.py's own _strip_markdown --
# duplicated rather than imported for the same circular-import reason the
# Groq client above is. Kept because the drafting prompt below already asks
# for "no markdown," but an LLM's own habit of adding it anyway would
# otherwise go out in a REAL, auto-sent email with no human proofread step
# to catch it -- this app, not a spoken reply, is where the raw asterisks
# would actually show.
_MARKDOWN_BOLD_ITALIC_RE = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)(.+?)\1")
_MARKDOWN_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MARKDOWN_BULLET_RE = re.compile(r"^\s*[\*\-•]\s+", re.MULTILINE)


def _strip_markdown(text: str) -> str:
    if not text:
        return text
    text = _MARKDOWN_BOLD_ITALIC_RE.sub(r"\2", text)
    text = _MARKDOWN_HEADER_RE.sub("", text)
    text = _MARKDOWN_BULLET_RE.sub("", text)
    return text


def _draft_important_suggestions():
    """Advances every still-'pending' suggestion to 'drafted', or leaves it
    'pending' to retry next cycle on a transient failure -- never raises,
    and never sends anything itself. The draft just sits in the Inbox tab
    until the user explicitly taps "send" (app.py's
    /api/inbox/suggestions/<id>/send), which goes through the real
    confirm-to-act path -- see the deliberately conservative prompt below,
    which exists so a weak first draft never has to be rewritten from
    scratch, not because nothing will review it before it goes out."""
    if not _groq_client:
        return  # GROQ_API_KEY unset (e.g. local dev without a .env) -- nothing to draft with
    for sugg in users.list_suggestions_needing_draft():
        subject = sugg.get("subject") or ""
        draft_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        prompt = (
            f"An email arrived from {sugg.get('sender_name') or sugg.get('sender_email')}. "
            f"Subject: {subject}. Preview: {sugg.get('snippet') or ''}\n\n"
            "Write ONLY a short holding reply as plain text: acknowledge receipt and say "
            "you'll follow up properly soon. Do NOT invent any dates, numbers, commitments, "
            "or decisions that aren't already stated in the preview above -- the user will "
            "review and can edit this before it's ever sent, so leave the real content for "
            "them to add. Write it exactly as the user themselves would write it, in first "
            "person -- never mention JARVIS, an assistant, AI, or anything automated; nothing "
            "in the reply should reveal this wasn't typed by the user personally. A brief "
            "greeting and closing, nothing else. No subject line, no markdown."
        )
        try:
            groq_pacer.wait_if_needed(900)
            completion = _groq_client.chat.completions.create(
                model=_IMPORTANT_EMAIL_MODEL,
                messages=[
                    {"role": "system", "content": "You draft short, deliberately conservative holding replies "
                     "for J.A.R.V.I.S. — the user never asked for this specific reply, so never fabricate "
                     "content beyond acknowledging the email and promising a proper follow-up."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4, max_tokens=250, reasoning_effort="low",
            )
            usage = getattr(completion, "usage", None)
            if usage:
                groq_pacer.record_usage(max(0, getattr(usage, "total_tokens", 0)))
            draft_body = _strip_markdown(completion.choices[0].message.content or "").strip()
            if not draft_body:
                raise ValueError("empty draft")
            users.set_suggestion_draft(sugg["id"], draft_subject, draft_body)
        except RateLimitError as e:
            kind, _ = groq_pacer.classify_rate_limit(e)
            users.record_suggestion_draft_error(sugg["id"], f"rate_limited:{kind}")
            if kind == "day":
                return  # daily quota exhausted -- stop this cycle's pass entirely, not just this one row
        except Exception as e:
            logger.error(f"Important-email draft failed for suggestion {sugg['id']}: {e}")
            users.record_suggestion_draft_error(sugg["id"], str(e))


# ---------------------------------------------------------------------------
# System notices: holiday greeting, meeting conflicts, market swings. All
# three are zero-LLM and deterministic (unlike the important-email drafter
# above) -- each just writes a generic, dismiss-only system_notices row
# (see users.add_system_notice) when its own simple condition is true.
# None of them ever sends anything anywhere; they only ever notify the
# signed-in user themselves.
# ---------------------------------------------------------------------------
HOLIDAY_CALENDAR_ID = "en.jewish#holiday@group.v.calendar.google.com"  # Google's own public calendar
HOLIDAY_LOOKAHEAD_DAYS = 3

# Google's public holiday calendar titles are in English ("Passover",
# "Rosh Hashana", "Hanukkah: 1 Candle", ...) -- this app's whole UI is
# Hebrew, so only a holiday we can confidently translate gets a notice;
# anything unrecognized is silently skipped rather than showing a half-
# English or garbled title.
_HOLIDAY_NAME_HE = [
    ("rosh hashana", "ראש השנה"), ("yom kippur", "יום כיפור"), ("sukkot", "סוכות"),
    ("simchat torah", "שמחת תורה"), ("shmini atzeret", "שמיני עצרת"),
    ("hanukkah", "חנוכה"), ("chanukah", "חנוכה"), ("tu bishvat", 'ט"ו בשבט'),
    ("purim", "פורים"), ("passover", "פסח"), ("pesach", "פסח"),
    ("shavuot", "שבועות"), ("tisha b'av", "תשעה באב"),
]


def _hebrew_holiday_name(summary) -> str | None:
    s = (summary or "").lower()
    for keyword, hebrew in _HOLIDAY_NAME_HE:
        if keyword in s:
            return hebrew
    return None


def _check_holiday_greeting():
    """Self-facing ONLY -- by the product owner's own explicit correction,
    this never sends anything to anyone else (no recipient list, no
    WhatsApp, no email on the user's behalf): it just wishes the signed-in
    user a happy holiday and reminds them to tell their own family.
    Reads Google's public Jewish-holidays calendar -- readable with the
    calendar.readonly scope this app already has, zero new dependency."""
    now_dt = datetime.now(timezone.utc)
    time_min = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now_dt + timedelta(days=HOLIDAY_LOOKAHEAD_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for user_id in users.list_connected_user_ids():
        try:
            events = google_service.list_calendar_events(
                user_id, time_min, time_max, max_results=5, calendar_id=HOLIDAY_CALENDAR_ID)
        except Exception as e:
            logger.error(f"Holiday calendar check failed for user {user_id}: {e}")
            continue
        for ev in events or []:
            hebrew_name = _hebrew_holiday_name(ev.get("summary"))
            if not hebrew_name:
                continue
            year = (ev.get("start") or "")[:4] or "unknown"
            try:
                users.add_system_notice(
                    user_id, "holiday",
                    title=f"{hebrew_name} מתקרב",
                    body=f"חג {hebrew_name} שמח! ואל תשכח לאחל חג שמח גם למשפחה שלך.",
                    dedupe_key=f"holiday:{hebrew_name}:{year}",
                )
            except Exception as e:
                logger.error(f"Failed to record holiday notice for user {user_id}: {e}")


def _check_meeting_conflicts():
    """Zero-LLM, deterministic: flags two of the next 24h's TIMED events
    that overlap. All-day events are excluded -- they don't "conflict" the
    way two scheduled meetings do."""
    now_dt = datetime.now(timezone.utc)
    time_min = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now_dt + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for user_id in users.list_connected_user_ids():
        try:
            events = google_service.list_calendar_events(user_id, time_min, time_max, max_results=20)
        except Exception as e:
            logger.error(f"Meeting-conflict check failed for user {user_id}: {e}")
            continue
        timed = []
        for ev in events or []:
            start, end = ev.get("start"), ev.get("end")
            if not start or not end or "T" not in start or "T" not in end:
                continue  # all-day (or malformed) -- skip rather than guess
            try:
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except ValueError:
                continue
            timed.append((start_dt, end_dt, ev))
        timed.sort(key=lambda t: t[0])
        # Every OVERLAPPING pair, not just adjacent-in-sorted-order ones --
        # adjacent-only misses a real conflict like A 9:00-11:00 containing
        # both B 9:15-9:30 and C 9:45-10:00: B and C don't overlap each
        # other, so they're adjacent in sort order with nothing between
        # them, but each still genuinely overlaps A. Bounded by max_results
        # (20), so the full pairwise sweep is cheap; the inner loop still
        # breaks early once b_start reaches a_end, since nothing later in
        # start-sorted order can overlap a_end either.
        for i in range(len(timed)):
            a_start, a_end, a_ev = timed[i]
            for j in range(i + 1, len(timed)):
                b_start, _b_end, b_ev = timed[j]
                if b_start >= a_end:
                    break  # sorted by start -- no later pair can overlap this a_end either
                date_key = a_start.strftime("%Y-%m-%d")
                try:
                    users.add_system_notice(
                        user_id, "conflict",
                        title="שתי פגישות חופפות",
                        body=(f"\"{a_ev.get('summary') or '(ללא כותרת)'}\" ו-"
                              f"\"{b_ev.get('summary') or '(ללא כותרת)'}\" חופפות היום ביומן שלך."),
                        dedupe_key=f"conflict:{a_ev.get('id','')}:{b_ev.get('id','')}:{date_key}",
                        action_label="פתח יומן",
                    )
                except Exception as e:
                    logger.error(f"Failed to record conflict notice for user {user_id}: {e}")


MARKET_SWING_THRESHOLD_PCT = 3.0


def _check_market_swing():
    """Zero-LLM, zero-personalization: ONE shared market snapshot fetched
    per cycle (not once per user -- this is public market data, not a
    per-user API call), fanned out as a notice to every connected user,
    once per index per day, only when the move exceeds the threshold."""
    try:
        snapshot = stocks_service.get_market_snapshot()
    except Exception as e:
        logger.error(f"Market snapshot fetch failed: {e}")
        return
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for row in snapshot or []:
        pct, label = row.get("today_pct"), row.get("label")
        if pct is None or not label or abs(pct) < MARKET_SWING_THRESHOLD_PCT:
            continue
        direction = "עלה" if pct >= 0 else "ירד"
        dedupe_key = f"market:{label}:{today_key}"
        for user_id in users.list_connected_user_ids():
            try:
                users.add_system_notice(
                    user_id, "market",
                    title=f"תנודה חדה ב-{label}",
                    body=f"מדד {label} {direction} {abs(pct):.1f}% היום.",
                    dedupe_key=dedupe_key,
                )
            except Exception as e:
                logger.error(f"Failed to record market notice for user {user_id}: {e}")


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

        try:
            _draft_important_suggestions()
        except Exception as e:
            logger.error(f"Important-email drafting pass failed: {e}")

        try:
            _check_holiday_greeting()
        except Exception as e:
            logger.error(f"Holiday-greeting check failed: {e}")

        try:
            _check_meeting_conflicts()
        except Exception as e:
            logger.error(f"Meeting-conflict check failed: {e}")

        try:
            _check_market_swing()
        except Exception as e:
            logger.error(f"Market-swing check failed: {e}")

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
