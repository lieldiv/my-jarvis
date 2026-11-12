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
  - important-email suggestions: no settings, no configured sender list any
    more (removed per explicit product-owner instruction) -- this is now a
    core, always-on part of the system. Scans every connected user's own
    most recent inbox mail and asks Groq a short classification question
    per new message (a small, independent Groq client -- see below): NO
    (ignore), YESNO (poses a clear yes/no question -- the Suggestions tab
    shows Yes/No buttons; once the user picks, a live request drafts the
    real reply), or REPLY (needs a genuine, content-aware reply, drafted
    here automatically). It never sends anything by itself: the draft just
    sits in the Suggestions tab until the user taps "send", which goes
    through the exact same confirm-to-act path
    (productivity_service.request_send_email) every other write in this
    app already uses -- the product owner was explicit that this feature
    must never send without that approval. This is the one job in this
    whole file an LLM call happens for; everything else in this module is
    either LLM-free or a plain notification.
  - system notices (holiday greeting, meeting conflicts, market swings,
    inbox backlog, missing event location, rain-vs-outdoor-plans, a daily
    weather ambient card, a stale-task nudge): all eight are zero-LLM and
    deterministic (the weather-vs-calendar check's "does this sound like
    an outdoor event" match is a fixed keyword list, not an LLM judgment),
    each writing a generic, dismiss-only `system_notices` row
    (users.add_system_notice) rendered in the HUD's "הצעות מערכת" tab.
    The holiday greeting is self-facing only -- it never sends anything
    to anyone else, by explicit product-owner instruction; it just wishes
    the signed-in user a happy holiday. The daily weather card is
    deliberately UNCONDITIONAL (fires once a day regardless of
    conditions) -- unlike every other notice here, which only appears
    when something is actually true, this one exists so the Suggestions
    tab isn't empty on an ordinary day with nothing else to flag.

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

import cert_bootstrap  # noqa: F401 — must run before any HTTPS-making import below (the weather check's own requests.get call)
import requests
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

# Safety cap, not a quality knob: the mitigation for a burst of important-
# looking mail (a hijacked/spoofed sender, a noisy thread) running up the
# shared Groq budget on one user. Checked once BEFORE issuing the Gmail
# query for that user (so a maxed-out user doesn't even cost an extra API
# call that cycle) AND again inside the per-message loop below (so a single
# cycle that classifies several messages back to back can't overshoot the
# cap by itself before the next cycle's check gets a chance to block it).
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


IMPORTANT_EMAIL_SCAN_COUNT = int(os.environ.get("JARVIS_IMPORTANT_EMAIL_SCAN_COUNT", "8"))


def _classify_important_email(sender: str, subject: str, snippet: str) -> str:
    """ONE cheap Groq call per new email -- there's no sender list to match
    against any more (removed per explicit product-owner instruction: this
    is now a core, always-on part of the system, not something the user
    configures), so this is the only way left to judge "does this look
    important." Replies with exactly one of:
      NO                 -- doesn't need a reply (spam/newsletter/notification/FYI)
      YESNO: <question>  -- asks ONE clear yes/no question, in the email's own language
      REPLY              -- needs a real reply, not a simple yes/no
    Kept deliberately short (small prompt, tiny max_tokens) since this runs
    on every new message in every connected user's inbox every cycle."""
    groq_pacer.wait_if_needed(150)
    completion = _groq_client.chat.completions.create(
        model=_IMPORTANT_EMAIL_MODEL,
        messages=[
            {"role": "system", "content": (
                "You triage incoming emails for a busy person's assistant. Reply with "
                "EXACTLY one of: NO (this doesn't need a reply -- spam, a newsletter, an "
                "automated notification, FYI-only, already resolved); YESNO: <question> "
                "(the email asks ONE clear yes/no question the person could answer "
                "directly -- put that exact question, in the email's own language, right "
                "after the colon); REPLY (this needs a real reply but isn't a simple "
                "yes/no question). No other text, no explanation, no markdown."
            )},
            {"role": "user", "content": f"From: {sender}\nSubject: {subject}\nPreview: {snippet}"},
        ],
        temperature=0.2, max_tokens=60, reasoning_effort="low",
    )
    usage = getattr(completion, "usage", None)
    if usage:
        groq_pacer.record_usage(max(0, getattr(usage, "total_tokens", 0)))
    return (completion.choices[0].message.content or "").strip()


def _check_important_emails():
    """Detection: for EVERY connected user (no settings/sender-list gate any
    more), scans their IMPORTANT_EMAIL_SCAN_COUNT most recent inbox emails
    and classifies each message newer than that user's own high-water mark
    (users.get_scan_high_water -- so the same already-seen mail is never
    reclassified, and re-billed against the shared Groq budget, every
    single 60s cycle forever). A message classified NO is just remembered
    as seen; YESNO/REPLY get a row written (YESNO additionally stores the
    detected question -- see users.set_suggestion_awaiting_choice). Never
    drafts anything itself -- a 'REPLY' row is picked up by
    _draft_important_suggestions below; an 'awaiting_choice' row waits for
    the user's own pick (app.py's .../answer route).

    Two correctness guards that are easy to get wrong with a fixed-size,
    newest-first scan window instead of a real backlog sweep:

    1. COVERAGE GAP: Gmail's "most recent N" fetch can be truncated -- if a
       user is frozen at IMPORTANT_EMAIL_DAILY_LIMIT (or just offline/
       unscanned) for long enough that MORE than IMPORTANT_EMAIL_SCAN_COUNT
       new messages pile up, this cycle's fetch only ever sees the newest
       few of them. Advancing the mark to the newest of THOSE anyway would
       make the older, never-fetched ones permanently unreachable -- next
       cycle's "> high_water" filter would exclude them forever,
       indistinguishable from "already seen" even though they were never
       classified. So the mark only advances once a cycle's fetch window
       demonstrably overlaps the OLD mark (oldest fetched message's date <=
       high_water) or the inbox is small enough that the fetch wasn't
       truncated at all. On a brand-new user (high_water == 0) this guard
       is skipped on purpose -- there's no backlog to protect against yet,
       just the intended "start watching from the most recent mail" baseline.
    2. REPEAT COST while stuck behind a real gap: since the mark can't
       always advance, the same already-recorded messages would otherwise
       get re-classified (and re-billed) every cycle for as long as the gap
       persists. users.important_suggestion_exists short-circuits those
       before they ever reach Groq.

    The high-water mark only advances when EVERY message in this cycle's
    batch was successfully classified (or already recorded) AND the
    coverage guard above is satisfied -- if any one of them hit a
    transient error, or the daily cap was hit mid-batch, the whole
    remainder is left to retry next cycle rather than risking silently
    skipping a message that failed right next to ones that succeeded."""
    if not _groq_client:
        return  # GROQ_API_KEY unset (e.g. local dev without a .env) -- nothing to classify with
    for user_id in users.list_connected_user_ids():
        if users.count_suggestions_created_since(user_id, time.time() - 86400) >= IMPORTANT_EMAIL_DAILY_LIMIT:
            continue
        try:
            emails = google_service.list_recent_emails(user_id, max_results=IMPORTANT_EMAIL_SCAN_COUNT, query="in:inbox")
        except Exception as e:
            logger.error(f"Important-email scan failed for user {user_id}: {e}")
            continue
        if not emails:
            continue
        high_water = users.get_scan_high_water(user_id) or 0
        to_process = [m for m in emails if m.get("date_ms", 0) > high_water]
        if not to_process:
            continue
        covered_fully = (
            high_water == 0
            or len(emails) < IMPORTANT_EMAIL_SCAN_COUNT
            or min(m.get("date_ms", 0) for m in emails) <= high_water
        )
        had_failure = False
        max_date_ms = high_water
        for m in to_process:
            date_ms = m.get("date_ms", 0)
            gmail_id = m.get("id", "")
            if users.important_suggestion_exists(user_id, gmail_id):
                max_date_ms = max(max_date_ms, date_ms)  # already classified in an earlier cycle -- no need to re-spend Groq on it
                continue
            if users.count_suggestions_created_since(user_id, time.time() - 86400) >= IMPORTANT_EMAIL_DAILY_LIMIT:
                had_failure = True  # cap hit mid-batch -- the rest of this batch is untouched, not "seen"
                break
            subject, snippet = m.get("subject", ""), m.get("snippet", "")
            try:
                verdict = _classify_important_email(m.get("sender", ""), subject, snippet)
            except RateLimitError as e:
                kind, _ = groq_pacer.classify_rate_limit(e)
                had_failure = True
                if kind == "day":
                    break  # daily quota exhausted -- stop this user's scan entirely this cycle
                continue  # per-minute limit -- skip this message, the whole batch retries next cycle anyway
            except Exception as e:
                logger.error(f"Important-email classify failed for user {user_id}: {e}")
                had_failure = True
                continue
            verdict_upper = (verdict or "").upper().strip()
            first_word = verdict_upper.split()[0] if verdict_upper.split() else ""
            is_yesno = verdict_upper.startswith("YESNO")
            is_reply = first_word == "REPLY"
            if first_word != "NO" and not is_yesno and not is_reply:
                # Garbled/empty/unrecognized model output -- NOT a safe
                # default to REPLY (that would mean acting on noise), and
                # NOT a safe default to NO either (that would silently
                # drop a possibly-important message). Treat it as a
                # transient failure so the whole message gets a clean
                # retry next cycle instead.
                logger.error(f"Important-email classify returned an unrecognized verdict for user {user_id}: {verdict!r}")
                had_failure = True
                continue
            max_date_ms = max(max_date_ms, date_ms)
            if first_word == "NO":
                continue
            name, addr = parseaddr(m.get("sender", ""))
            try:
                sid = users.add_important_suggestion(
                    user_id, gmail_message_id=gmail_id, sender_name=name or addr,
                    sender_email=addr or m.get("sender", ""), subject=subject, snippet=snippet, date_ms=date_ms,
                )
            except Exception as e:
                logger.error(f"Failed to record important-email suggestion for user {user_id}: {e}")
                continue
            if sid is None:
                continue
            if is_yesno:
                question = verdict.split(":", 1)[1].strip() if ":" in verdict else subject
                try:
                    users.set_suggestion_awaiting_choice(sid, question or subject)
                except Exception as e:
                    logger.error(f"Failed to record yes/no question for suggestion {sid}: {e}")
        if not had_failure and covered_fully and max_date_ms > high_water:
            try:
                users.set_scan_high_water(user_id, max_date_ms)
            except Exception as e:
                logger.error(f"Failed to update scan high-water mark for user {user_id}: {e}")


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
    and never sends anything itself. The draft just sits in the Suggestions
    tab until the user explicitly taps "send" (app.py's
    /api/inbox/suggestions/<id>/send), which goes through the real
    confirm-to-act path. Unlike the first version of this feature, the
    draft now genuinely engages with the email's real content (per
    explicit product-owner instruction) rather than being a generic
    holding reply -- the "don't fabricate" guardrail below is about never
    inventing facts/commitments that aren't in the preview, not about
    avoiding real content altogether."""
    if not _groq_client:
        return  # GROQ_API_KEY unset (e.g. local dev without a .env) -- nothing to draft with
    for sugg in users.list_suggestions_needing_draft():
        subject = sugg.get("subject") or ""
        draft_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        prompt = (
            f"An email arrived from {sugg.get('sender_name') or sugg.get('sender_email')}. "
            f"Subject: {subject}. Preview: {sugg.get('snippet') or ''}\n\n"
            "Write a short, natural reply as plain text that actually engages with what "
            "this email says -- respond to its real content, don't just acknowledge "
            "receipt. Do NOT invent any dates, numbers, commitments, or decisions that "
            "aren't already stated in the preview above -- the user will review and can "
            "edit this before it's ever sent, so leave anything you're unsure of for them "
            "to fill in rather than guessing. Write it exactly as the user themselves would "
            "write it, in first person -- never mention JARVIS, an assistant, AI, or "
            "anything automated; nothing in the reply should reveal this wasn't typed by "
            "the user personally. A brief greeting and closing, nothing else. No subject "
            "line, no markdown."
        )
        try:
            groq_pacer.wait_if_needed(900)
            completion = _groq_client.chat.completions.create(
                model=_IMPORTANT_EMAIL_MODEL,
                messages=[
                    {"role": "system", "content": "You draft short, natural plain-text email replies for "
                     "J.A.R.V.I.S. that genuinely engage with the original email's content — never fabricate "
                     "facts, dates, numbers or commitments that aren't already stated in what you were given."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5, max_tokens=300, reasoning_effort="low",
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
    """Zero-LLM, zero-personalization: TWO shared snapshots fetched per
    cycle (not once per user -- this is public market data, not a
    per-user API call) -- broad indices, AND (added per explicit request,
    after a real individual stock moved sharply and nothing flagged it,
    since only indices were tracked before) a fixed list of the biggest,
    most widely-recognized individual stocks. Each fans out a notice to
    every connected user, once per index/stock per day, only when the
    move exceeds the threshold."""
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        index_snapshot = stocks_service.get_market_snapshot()
    except Exception as e:
        logger.error(f"Market snapshot fetch failed: {e}")
        index_snapshot = []
    for row in index_snapshot or []:
        pct, label = row.get("today_pct"), row.get("label")
        if pct is None or not label or abs(pct) < MARKET_SWING_THRESHOLD_PCT:
            continue
        direction = "עלה" if pct >= 0 else "ירד"
        for user_id in users.list_connected_user_ids():
            try:
                users.add_system_notice(
                    user_id, "market",
                    title=f"תנודה חדה ב-{label}",
                    body=f"מדד {label} {direction} {abs(pct):.1f}% היום.",
                    dedupe_key=f"market:{label}:{today_key}",
                )
            except Exception as e:
                logger.error(f"Failed to record market notice for user {user_id}: {e}")

    try:
        stock_snapshot = stocks_service.get_watched_stocks_snapshot()
    except Exception as e:
        logger.error(f"Watched-stocks snapshot fetch failed: {e}")
        stock_snapshot = []
    for row in stock_snapshot or []:
        pct, label = row.get("today_pct"), row.get("label")
        if pct is None or not label or abs(pct) < MARKET_SWING_THRESHOLD_PCT:
            continue
        direction = "עלתה" if pct >= 0 else "ירדה"
        for user_id in users.list_connected_user_ids():
            try:
                users.add_system_notice(
                    user_id, "stock",
                    title=f"תנודה חדה במניית {label}",
                    body=f"מניית {label} {direction} {abs(pct):.1f}% היום.",
                    dedupe_key=f"stock:{label}:{today_key}",
                )
            except Exception as e:
                logger.error(f"Failed to record stock notice for user {user_id}: {e}")


INBOX_BACKLOG_THRESHOLD = int(os.environ.get("JARVIS_INBOX_BACKLOG_THRESHOLD", "15"))


def _check_inbox_backlog():
    """Zero-LLM: a plain unread-count threshold (Gmail's own
    resultSizeEstimate, see google_service.get_unread_count), reusing the
    same Gmail read this app already does elsewhere. Deliberately does NOT
    draft or suggest anything about specific emails -- that's the
    important-email feature's own job; this is only a backlog nudge, and
    only once per day so it can't re-fire every cycle while the backlog
    stays above the threshold."""
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for user_id in users.list_connected_user_ids():
        try:
            count = google_service.get_unread_count(user_id)
        except Exception as e:
            logger.error(f"Inbox-backlog check failed for user {user_id}: {e}")
            continue
        if count is None or count < INBOX_BACKLOG_THRESHOLD:
            continue
        try:
            users.add_system_notice(
                user_id, "inbox_backlog",
                title="יש לך הרבה דואר שלא נקרא",
                body=f"{count} הודעות שלא נקראו מחכות לך.",
                dedupe_key=f"inbox_backlog:{user_id}:{today_key}",
                action_label="עבור לדואר",
            )
        except Exception as e:
            logger.error(f"Failed to record inbox-backlog notice for user {user_id}: {e}")


def _check_missing_location():
    """Zero-LLM: flags an imminent (next 2h) TIMED event with no location
    and nothing that looks like a video-call link already in its own
    summary/location -- a plain presence check, not a guess about which
    meetings "need" one. All-day events are excluded, same reasoning as
    the conflict check."""
    now_dt = datetime.now(timezone.utc)
    time_min = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now_dt + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for user_id in users.list_connected_user_ids():
        try:
            events = google_service.list_calendar_events(user_id, time_min, time_max, max_results=10)
        except Exception as e:
            logger.error(f"Missing-location check failed for user {user_id}: {e}")
            continue
        for ev in events or []:
            start = ev.get("start")
            if not start or "T" not in start:
                continue  # all-day (or malformed) -- skip rather than guess
            location = (ev.get("location") or "").strip()
            summary = ev.get("summary") or ""
            hangout_link = (ev.get("hangout_link") or "").strip()
            if location or hangout_link or "http://" in summary or "https://" in summary:
                continue  # already has a place, a Google Meet link, or already looks like a video-call link
            try:
                users.add_system_notice(
                    user_id, "missing_location",
                    title="לפגישה הקרובה אין מיקום",
                    body=f"\"{summary or '(ללא כותרת)'}\" מתחילה בקרוב בלי מיקום או קישור לשיחה.",
                    dedupe_key=f"missing_location:{ev.get('id','')}:{start[:10]}",
                    action_label="פתח יומן",
                )
            except Exception as e:
                logger.error(f"Failed to record missing-location notice for user {user_id}: {e}")


WEATHER_LOCATION = os.environ.get("JARVIS_WEATHER_LOCATION", "Tel Aviv")
RAIN_CHANCE_THRESHOLD_PCT = 50
# Deliberately a fixed keyword list, not an LLM judgment -- same "no
# fabrication, no guessing" bar as the rest of this file. Low recall by
# design (an event titled just "עם דנה" never matches, even if it really
# is outdoors) is the accepted tradeoff for zero false "this is outdoors"
# claims about a meeting that never said so.
_OUTDOOR_EVENT_KEYWORDS = [
    "ריצה", "run", "טיול", "hike", "hiking", "פיקניק", "picnic", "בחוץ", "outdoor",
    "גינה", "גן", "garden", "ים", "beach", "חוף", "פארק", "park", "אופניים", "bike",
    "cycling", "שטח", "קמפינג", "camping", "בר-בי-קיו", "bbq", "barbecue",
]


def _looks_outdoor(summary: str, location: str) -> bool:
    text = f"{summary or ''} {location or ''}".lower()
    return any(kw in text for kw in _OUTDOOR_EVENT_KEYWORDS)


def _tomorrow_rain_chance(location: str):
    """Returns the highest hourly chance-of-rain (0-100) in tomorrow's
    forecast, or None if the call failed -- wttr.in's free j1 (JSON)
    endpoint, no API key. Index 1 of "weather" is always "tomorrow"
    relative to whatever day wttr.in itself thinks it is."""
    try:
        res = requests.get(f"https://wttr.in/{requests.utils.quote(location)}", params={"format": "j1"}, timeout=6)
        res.raise_for_status()
        data = res.json()
        tomorrow = data.get("weather", [])[1]
        hours = tomorrow.get("hourly") or []
        chances = [int(h.get("chanceofrain", 0)) for h in hours if h.get("chanceofrain") is not None]
        return max(chances) if chances else None
    except Exception as e:
        logger.warning(f"Tomorrow's rain forecast fetch failed for '{location}': {e}")
        return None


def _check_weather_vs_calendar():
    """Zero-LLM: if tomorrow's forecast has a real chance of rain AND the
    user has an outdoor-sounding event tomorrow (fixed keyword match, see
    _looks_outdoor), flag it. One shared forecast fetch per cycle (weather
    isn't per-user), only fanned out to users who actually have a matching
    event."""
    rain_chance = _tomorrow_rain_chance(WEATHER_LOCATION)
    if rain_chance is None or rain_chance < RAIN_CHANCE_THRESHOLD_PCT:
        return
    # Unlike the other checks' plain rolling now..now+N windows (TZ-
    # irrelevant), this one is calendar-DAY-aligned -- "tomorrow" has to be
    # tomorrow in the app's own local day (productivity_service.LOCAL_TZ,
    # Asia/Jerusalem), the same convention every other "today"/"tomorrow"
    # concept in this app already uses. Building this in UTC instead would
    # shift the window by the local UTC offset (+2/+3h), missing early-
    # morning local-tomorrow events and pulling in early-morning
    # day-after-tomorrow ones instead.
    tomorrow_local = datetime.now(productivity_service.LOCAL_TZ) + timedelta(days=1)
    day_start_local = tomorrow_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = tomorrow_local.replace(hour=23, minute=59, second=59, microsecond=0)
    time_min = day_start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = day_end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tomorrow_key = tomorrow_local.strftime("%Y-%m-%d")
    for user_id in users.list_connected_user_ids():
        try:
            events = google_service.list_calendar_events(user_id, time_min, time_max, max_results=10)
        except Exception as e:
            logger.error(f"Weather-vs-calendar check failed for user {user_id}: {e}")
            continue
        for ev in events or []:
            if not _looks_outdoor(ev.get("summary"), ev.get("location")):
                continue
            try:
                users.add_system_notice(
                    user_id, "weather",
                    title="צפוי גשם מחר",
                    body=f"יש {rain_chance}% סיכוי לגשם מחר, ויש לך \"{ev.get('summary') or '(ללא כותרת)'}\" ביומן.",
                    dedupe_key=f"weather:{ev.get('id','')}:{tomorrow_key}",
                )
            except Exception as e:
                logger.error(f"Failed to record weather notice for user {user_id}: {e}")


# Common wttr.in condition strings translated to Hebrew; anything NOT in
# this dict is left out of the notice entirely (never show raw English,
# same bar as the holiday-name translator above) -- the temperature range
# alone still makes the card worth showing.
_WEATHER_DESC_HE = {
    "sunny": "שמשי", "clear": "בהיר", "partly cloudy": "מעונן חלקית",
    "cloudy": "מעונן", "overcast": "מעונן מלא", "mist": "ערפל", "fog": "ערפל",
    "patchy rain possible": "ייתכן גשם מקומי", "light rain": "גשם קל",
    "moderate rain": "גשם בינוני", "heavy rain": "גשם כבד",
    "thundery outbreaks possible": "ייתכנו סופות רעמים", "light snow": "שלג קל",
}


def _check_daily_weather():
    """Zero-LLM, UNCONDITIONAL -- unlike _check_weather_vs_calendar (which
    only ever fires on a rain+outdoor-event match), this is a plain "here's
    today's weather" ambient card that fires once a day regardless of
    conditions, so the Suggestions tab isn't only ever populated on days
    something is actually wrong. Reuses the same wttr.in j1 endpoint, but
    its own independent fetch (kept separate from the rain-check's, for
    the same reason every other detector in this file is independent:
    isolated failure, easy to reason about on its own)."""
    today_key = datetime.now(productivity_service.LOCAL_TZ).strftime("%Y-%m-%d")
    try:
        res = requests.get(f"https://wttr.in/{requests.utils.quote(WEATHER_LOCATION)}", params={"format": "j1"}, timeout=6)
        res.raise_for_status()
        data = res.json()
        today = data.get("weather", [])[0]
        max_c, min_c = today.get("maxtempC"), today.get("mintempC")
        hours = today.get("hourly") or []
        desc_en = None
        if hours:
            noonish = hours[min(len(hours) - 1, len(hours) // 2)]
            desc_list = noonish.get("weatherDesc") or []
            desc_en = (desc_list[0].get("value") or "").strip() if desc_list else None
    except Exception as e:
        logger.warning(f"Daily weather fetch failed for '{WEATHER_LOCATION}': {e}")
        return
    if max_c is None or min_c is None:
        return
    body = f"היום: {min_c}°–{max_c}°"
    desc_he = _WEATHER_DESC_HE.get((desc_en or "").lower())
    if desc_he:
        body += f", {desc_he}"
    for user_id in users.list_connected_user_ids():
        try:
            users.add_system_notice(
                user_id, "daily_weather",
                title="מזג האוויר היום",
                body=body,
                dedupe_key=f"daily_weather:{today_key}",
            )
        except Exception as e:
            logger.error(f"Failed to record daily-weather notice for user {user_id}: {e}")


STALE_TASK_DAYS = int(os.environ.get("JARVIS_STALE_TASK_DAYS", "14"))


def _check_stale_tasks():
    """Zero-LLM: a one-time (non-recurring) task that's sat open for
    STALE_TASK_DAYS with no progress gets a gentle nudge. A recurring task
    is deliberately excluded -- it's supposed to stay open indefinitely,
    that's not staleness. Checked across all users at once (one query),
    unlike the calendar-based checks which have to go per-user."""
    cutoff = time.time() - STALE_TASK_DAYS * 86400
    try:
        stale = users.list_stale_tasks(cutoff)
    except Exception as e:
        logger.error(f"Stale-task check failed: {e}")
        return
    for task in stale or []:
        try:
            users.add_system_notice(
                task["user_id"], "stale_task",
                title="משימה ישנה עדיין פתוחה",
                body=f"\"{task['text']}\" עדיין פתוחה כבר יותר מ-{STALE_TASK_DAYS} יום.",
                dedupe_key=f"stale_task:{task['id']}",
                action_label="פתח משימות",
            )
        except Exception as e:
            logger.error(f"Failed to record stale-task notice for task {task.get('id')}: {e}")


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

        try:
            _check_inbox_backlog()
        except Exception as e:
            logger.error(f"Inbox-backlog check failed: {e}")

        try:
            _check_missing_location()
        except Exception as e:
            logger.error(f"Missing-location check failed: {e}")

        try:
            _check_weather_vs_calendar()
        except Exception as e:
            logger.error(f"Weather-vs-calendar check failed: {e}")

        try:
            _check_daily_weather()
        except Exception as e:
            logger.error(f"Daily-weather check failed: {e}")

        try:
            _check_stale_tasks()
        except Exception as e:
            logger.error(f"Stale-task check failed: {e}")

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
