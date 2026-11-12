# Proves the smart-detection redesign of important-email suggestions:
# detection is no longer a sender-list match -- there is no settings UI or
# route left at all -- it's now a per-message LLM classification
# (_classify_important_email) that returns NO / YESNO:<question> / REPLY,
# driven by a per-user high-water mark instead of an enabled/senders toggle.
# Drafting (_draft_important_suggestions) and detection both happen
# automatically in daily_briefing.py's background scheduler, but NEITHER of
# them ever sends anything. A YESNO classification instead parks the row in
# 'awaiting_choice' until the user picks yes/no via a LIVE request
# (app.py's /api/inbox/suggestions/<id>/answer), which drafts synchronously
# so there's no dead air waiting for the next scheduler cycle. Sending only
# ever happens from a live, user-present request
# (app.py's /api/inbox/suggestions/<id>/send), going through the exact same
# confirm-to-act path (productivity_service.request_send_email) every other
# email in this app already uses, pinned to provider="google" since
# detection only ever happens via Gmail. The product owner was explicit,
# more than once, that this feature must never send without that approval.
import os, sys, time, tempfile, types
os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
os.environ["JARVIS_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "suggestions_test.db")  # isolated, throwaway
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # the repo root

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

import users
users._init_db()
UID = "suggestee-1"
users.upsert_user(UID, "person@example.com", "Person")
users.save_google_token(UID, '{"fake": "token"}')

# ---------------------------------------------------------------------------- users.py: suggestion row lifecycle
sid = users.add_important_suggestion(UID, "msg-1", "Boss", "boss@example.com", "Urgent", "please review", 1_700_000_000_000)
check("a new suggestion gets a real row id", sid is not None, sid)
dup = users.add_important_suggestion(UID, "msg-1", "Boss", "boss@example.com", "Urgent", "please review", 1_700_000_000_000)
check("dedup: same gmail_message_id for the same user returns None, no second row", dup is None, dup)
check("list_pending_suggestions shows exactly the one row (status='pending')",
      len(users.list_pending_suggestions(UID)) == 1 and users.list_pending_suggestions(UID)[0]["status"] == "pending")
check("list_suggestions_needing_draft picks up the pending row",
      any(s["id"] == sid for s in users.list_suggestions_needing_draft()))

users.set_suggestion_draft(sid, "Re: Urgent", "Thanks, I'll follow up soon.")
drafted = users.get_suggestion(sid, UID)
check("set_suggestion_draft advances pending -> drafted, with no sending of its own",
      drafted["status"] == "drafted" and drafted["draft_body"] == "Thanks, I'll follow up soon.", drafted)
check("a drafted row no longer shows up in list_suggestions_needing_draft (won't be re-drafted)",
      all(s["id"] != sid for s in users.list_suggestions_needing_draft()))
check("a drafted row still shows up in list_pending_suggestions (status != 'resolved') until the user acts",
      any(s["id"] == sid for s in users.list_pending_suggestions(UID)))

other_uid = "other-person"
users.upsert_user(other_uid, "other@example.com", "Other")
check("ownership scoping: a different user can't read someone else's suggestion",
      users.get_suggestion(sid, other_uid) is None)

resolved = users.mark_suggestion_resolved(sid, UID, "dismissed")
check("resolving a drafted suggestion succeeds", resolved is True)
check("...and it drops out of the pending list", all(s["id"] != sid for s in users.list_pending_suggestions(UID)))
resolved_again = users.mark_suggestion_resolved(sid, UID, "sent")
check("resolving an already-resolved row is a safe no-op (returns False, doesn't clobber the resolution)", resolved_again is False)

sid_err = users.add_important_suggestion(UID, "msg-err", "Boss", "boss@example.com", "Oops", "will fail to draft", int(time.time() * 1000))
users.record_suggestion_draft_error(sid_err, "rate_limited:minute")
check("a failed draft attempt leaves the row 'pending' (status unchanged, so next cycle retries it)",
      users.get_suggestion(sid_err, UID)["status"] == "pending")
check("...and it's still visible to list_suggestions_needing_draft for the retry",
      any(s["id"] == sid_err for s in users.list_suggestions_needing_draft()))
users.mark_suggestion_resolved(sid_err, UID, "dismissed")

# ---------------------------------------------------------------------------- users.py: high-water mark + awaiting_choice
HW_UID = "highwater-1"
users.upsert_user(HW_UID, "hw@example.com", "HW")
check("a user with no scan history yet has no high-water mark", users.get_scan_high_water(HW_UID) is None)
users.set_scan_high_water(HW_UID, 1_700_000_000_000)
check("set_scan_high_water persists and round-trips", users.get_scan_high_water(HW_UID) == 1_700_000_000_000)
users.set_scan_high_water(HW_UID, 1_700_000_500_000)
check("a later call overwrites the earlier mark", users.get_scan_high_water(HW_UID) == 1_700_000_500_000)

sid_choice = users.add_important_suggestion(HW_UID, "msg-choice-1", "Dana", "dana@example.com",
                                             "Dinner?", "want to grab dinner tonight?", int(time.time() * 1000))
users.set_suggestion_awaiting_choice(sid_choice, "Do you want to grab dinner tonight?")
choice_row = users.get_suggestion(sid_choice, HW_UID)
check("set_suggestion_awaiting_choice advances pending -> awaiting_choice and stores the question",
      choice_row["status"] == "awaiting_choice" and choice_row["detected_question"] == "Do you want to grab dinner tonight?",
      choice_row)
check("an awaiting_choice row still shows up in list_pending_suggestions (status != 'resolved')",
      any(s["id"] == sid_choice and s.get("detected_question") for s in users.list_pending_suggestions(HW_UID)))
check("an awaiting_choice row is NOT picked up by list_suggestions_needing_draft (it's not plain 'pending')",
      all(s["id"] != sid_choice for s in users.list_suggestions_needing_draft()))
users.mark_suggestion_resolved(sid_choice, HW_UID, "dismissed")

# ---------------------------------------------------------------------------- daily_briefing.py: detection (LLM-classified, high-water-gated)
import daily_briefing as db

def _scripted_classify(script):
    """Each entry is consumed in call order -- a string verdict is returned
    as-is, an Exception instance is raised. Lets a test script exactly what
    _check_important_emails should see per message without needing a real
    (or Groq-shaped fake) completion object for every scenario."""
    calls = []
    def _f(sender, subject, snippet):
        calls.append((sender, subject, snippet))
        item = script[len(calls) - 1]
        if isinstance(item, Exception):
            raise item
        return item
    return _f, calls

def _mk_list_recent(target_uid, emails):
    """Scoped to ONE user -- _check_important_emails loops over every
    connected user (including ones earlier sections of this file already
    gave a Google token), and without this scoping those other users would
    get swept into the same scan and silently inflate classify-call counts
    this test is trying to pin down exactly."""
    list_calls = []
    def _f(user_id, max_results=8, query="is:unread"):
        list_calls.append((user_id, max_results, query))
        return emails if user_id == target_uid else []
    return _f, list_calls

import httpx
from groq import RateLimitError

def _rate_limit_error(kind):
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(429, headers={"retry-after": "1"}, request=req)
    text = "Rate limit reached ... requests per day (RPD)." if kind == "day" else "Rate limit reached ... tokens per minute (TPM)."
    return RateLimitError(text, response=resp, body={"error": {"message": text}})

db._groq_client = object()  # truthy sentinel -- _check_important_emails only guards on "is there a client at
                             # all," the real classification call is fully replaced by _scripted_classify below

DET_UID = "detectee-1"
users.upsert_user(DET_UID, "det@example.com", "Det")
users.save_google_token(DET_UID, '{"fake": "token"}')

T1, T2, T3 = 1_800_000_000_000, 1_800_000_001_000, 1_800_000_002_000
db.google_service.list_recent_emails, list_calls = _mk_list_recent(DET_UID, [
    {"id": "msg-no1", "sender": "Newsletter <news@example.com>", "subject": "Weekly digest", "snippet": "...", "date_ms": T1, "source": "Gmail"},
])
db._classify_important_email, classify_calls = _scripted_classify(["NO"])
db._check_important_emails()
check("a NO verdict creates no suggestion row", len(users.list_pending_suggestions(DET_UID)) == 0, users.list_pending_suggestions(DET_UID))
check("...but the high-water mark still advances (the message WAS successfully classified)",
      users.get_scan_high_water(DET_UID) == T1, users.get_scan_high_water(DET_UID))
check("the scan used the inbox query, not a from:-address filter (no sender list exists any more)",
      list_calls[-1][2] == "in:inbox", list_calls)

db.google_service.list_recent_emails, list_calls = _mk_list_recent(DET_UID, [
    {"id": "msg-no1", "sender": "Newsletter <news@example.com>", "subject": "Weekly digest", "snippet": "...", "date_ms": T1, "source": "Gmail"},
    {"id": "msg-reply1", "sender": "Boss <boss@example.com>", "subject": "Please review", "snippet": "see attached deck", "date_ms": T2, "source": "Gmail"},
])
db._classify_important_email, classify_calls = _scripted_classify(["REPLY"])
db._check_important_emails()
check("the already-classified (date <= high-water) message is never reclassified", len(classify_calls) == 1, classify_calls)
reply_sugg = next((s for s in users.list_pending_suggestions(DET_UID) if s["sender_email"] == "boss@example.com"), None)
check("a REPLY verdict creates a plain 'pending' suggestion (for the background drafter to pick up)",
      reply_sugg is not None and reply_sugg["status"] == "pending", reply_sugg)
check("high-water advances to the REPLY message's own date", users.get_scan_high_water(DET_UID) == T2)

db.google_service.list_recent_emails, list_calls = _mk_list_recent(DET_UID, [
    {"id": "msg-yesno1", "sender": "Dana <dana@example.com>", "subject": "Dinner?", "snippet": "pizza tonight?", "date_ms": T3, "source": "Gmail"},
])
db._classify_important_email, classify_calls = _scripted_classify(["YESNO: Do you want pizza tonight?"])
db._check_important_emails()
yesno_sugg = next((s for s in users.list_pending_suggestions(DET_UID) if s["sender_email"] == "dana@example.com"), None)
check("a YESNO verdict creates an 'awaiting_choice' row with the detected question stored",
      yesno_sugg is not None and yesno_sugg["status"] == "awaiting_choice"
      and yesno_sugg["detected_question"] == "Do you want pizza tonight?", yesno_sugg)
check("high-water advances to the YESNO message's own date", users.get_scan_high_water(DET_UID) == T3)

# Partial-failure batch: one message fails to classify, one succeeds -- the
# high-water mark must NOT advance past its old value (so the failed
# message gets retried next cycle), even though the succeeding message's
# suggestion row IS still created.
T4, T5 = 1_800_000_003_000, 1_800_000_004_000
db.google_service.list_recent_emails, list_calls = _mk_list_recent(DET_UID, [
    {"id": "msg-fail-1", "sender": "Flaky <flaky@example.com>", "subject": "x", "snippet": "y", "date_ms": T4, "source": "Gmail"},
    {"id": "msg-ok-1", "sender": "Partner <partner@example.com>", "subject": "Contract", "snippet": "ready to sign?", "date_ms": T5, "source": "Gmail"},
])
db._classify_important_email, classify_calls = _scripted_classify([ValueError("transient classify failure"), "REPLY"])
db._check_important_emails()
check("a message that failed classification still lets a LATER message in the same batch get classified",
      len(classify_calls) == 2, classify_calls)
check("the succeeding message's suggestion row is still created despite the earlier failure",
      any(s["sender_email"] == "partner@example.com" for s in users.list_pending_suggestions(DET_UID)))
check("the high-water mark does NOT advance past T3 when any message in the batch failed",
      users.get_scan_high_water(DET_UID) == T3, users.get_scan_high_water(DET_UID))

# Retry cycle: same two messages are still > high-water (T3), so both are
# re-scanned. msg-ok-1 already has a row from last cycle -- the existence
# check skips it BEFORE spending another Groq call on it (that's the whole
# point of users.important_suggestion_exists). msg-fail-1 succeeds this
# time, so the whole batch is clean and the mark finally advances.
db.google_service.list_recent_emails, list_calls = _mk_list_recent(DET_UID, [
    {"id": "msg-fail-1", "sender": "Flaky <flaky@example.com>", "subject": "x", "snippet": "y", "date_ms": T4, "source": "Gmail"},
    {"id": "msg-ok-1", "sender": "Partner <partner@example.com>", "subject": "Contract", "snippet": "ready to sign?", "date_ms": T5, "source": "Gmail"},
])
db._classify_important_email, classify_calls = _scripted_classify(["NO"])
pending_before_retry = len(users.list_pending_suggestions(DET_UID))
db._check_important_emails()
check("the already-recorded message (msg-ok-1) is skipped before ever reaching Groq -- only msg-fail-1 is actually classified",
      len(classify_calls) == 1 and classify_calls[0][1] == "x", classify_calls)
check("the dedup no-op for the already-recorded message creates no second row",
      len(users.list_pending_suggestions(DET_UID)) == pending_before_retry, users.list_pending_suggestions(DET_UID))
check("once the whole batch succeeds, the high-water mark advances to the newest date (T5)",
      users.get_scan_high_water(DET_UID) == T5, users.get_scan_high_water(DET_UID))

for s in users.list_pending_suggestions(DET_UID):
    users.mark_suggestion_resolved(s["id"], DET_UID, "dismissed")

# RateLimitError handling: a per-minute limit skips just that message and
# keeps going; a per-day limit aborts the rest of this user's scan outright.
# Either way had_failure=True means the high-water mark doesn't move.
RL_UID = "ratelimited-1"
users.upsert_user(RL_UID, "rl@example.com", "RL")
users.save_google_token(RL_UID, '{"fake": "token"}')
Ta, Tb = 1_800_000_010_000, 1_800_000_011_000
db.google_service.list_recent_emails, list_calls = _mk_list_recent(RL_UID, [
    {"id": "msg-rl-a", "sender": "A <a@example.com>", "subject": "a", "snippet": "a", "date_ms": Ta, "source": "Gmail"},
    {"id": "msg-rl-b", "sender": "B <b@example.com>", "subject": "b", "snippet": "b", "date_ms": Tb, "source": "Gmail"},
])
db._classify_important_email, classify_calls = _scripted_classify([_rate_limit_error("minute"), "REPLY"])
db._check_important_emails()
check("a per-MINUTE rate limit on one message doesn't abort the rest of the batch",
      len(classify_calls) == 2, classify_calls)
check("...and the message after it still gets a suggestion row",
      any(s["sender_email"] == "b@example.com" for s in users.list_pending_suggestions(RL_UID)))
check("a per-minute rate limit still blocks the high-water mark from advancing this cycle",
      users.get_scan_high_water(RL_UID) is None, users.get_scan_high_water(RL_UID))

RL_UID2 = "ratelimited-2"
users.upsert_user(RL_UID2, "rl2@example.com", "RL2")
users.save_google_token(RL_UID2, '{"fake": "token"}')
db.google_service.list_recent_emails, list_calls = _mk_list_recent(RL_UID2, [
    {"id": "msg-rl2-a", "sender": "C <c@example.com>", "subject": "c", "snippet": "c", "date_ms": Ta, "source": "Gmail"},
    {"id": "msg-rl2-b", "sender": "D <d@example.com>", "subject": "d", "snippet": "d", "date_ms": Tb, "source": "Gmail"},
])
db._classify_important_email, classify_calls = _scripted_classify([_rate_limit_error("day"), "REPLY"])
db._check_important_emails()
check("a per-DAY rate limit aborts the rest of this user's scan this cycle entirely (no auto-retry)",
      len(classify_calls) == 1, classify_calls)
check("the message after the daily-limit hit is never even classified", len(users.list_pending_suggestions(RL_UID2)) == 0)
check("a day-limit hit also blocks the high-water mark from advancing", users.get_scan_high_water(RL_UID2) is None)

# Daily per-user suggestion cap still applies, same as before the redesign.
CAP_UID = "capped-1"
users.upsert_user(CAP_UID, "cap@example.com", "Cap")
users.save_google_token(CAP_UID, '{"fake": "token"}')
db.IMPORTANT_EMAIL_DAILY_LIMIT = 1
db._classify_important_email, classify_calls = _scripted_classify(["REPLY"] * 10)
for i, gid in enumerate(("cap-msg-1", "cap-msg-2", "cap-msg-3")):
    db.google_service.list_recent_emails, _ = _mk_list_recent(CAP_UID, [
        {"id": gid, "sender": "Boss <boss@example.com>", "subject": "x", "snippet": "y",
         "date_ms": 1_800_000_020_000 + i, "source": "Gmail"},
    ])
    db._check_important_emails()
count_created = users.count_suggestions_created_since(CAP_UID, 0)
check(f"daily cap (={db.IMPORTANT_EMAIL_DAILY_LIMIT}) stops new rows even with more matching mail available",
      count_created <= db.IMPORTANT_EMAIL_DAILY_LIMIT, count_created)
db.IMPORTANT_EMAIL_DAILY_LIMIT = 5

# The within-cycle cap: the per-user check before the fetch isn't the only
# guard -- a single cycle that classifies several REPLY/YESNO messages back
# to back must not overshoot the cap by itself before the NEXT cycle's
# check gets a chance to block it.
CAP2_UID = "capped-2"
users.upsert_user(CAP2_UID, "cap2@example.com", "Cap2")
users.save_google_token(CAP2_UID, '{"fake": "token"}')
db.IMPORTANT_EMAIL_DAILY_LIMIT = 2
cap2_emails = [
    {"id": f"cap2-msg-{i}", "sender": f"S{i} <s{i}@example.com>", "subject": f"s{i}", "snippet": "y",
     "date_ms": 1_800_000_060_000 + i, "source": "Gmail"}
    for i in range(4)
]
db.google_service.list_recent_emails, list_calls = _mk_list_recent(CAP2_UID, cap2_emails)
db._classify_important_email, classify_calls = _scripted_classify(["REPLY"] * 4)
db._check_important_emails()
check("the daily cap is enforced WITHIN a single cycle too -- at most 2 rows created even with 4 REPLY-worthy messages available in one fetch",
      users.count_suggestions_created_since(CAP2_UID, 0) == 2, users.count_suggestions_created_since(CAP2_UID, 0))
check("hitting the cap mid-batch blocks the high-water mark from advancing (the rest of the batch was never classified)",
      users.get_scan_high_water(CAP2_UID) is None, users.get_scan_high_water(CAP2_UID))
db.IMPORTANT_EMAIL_DAILY_LIMIT = 5

# Coverage gap: a fetch window that's fully saturated (hits
# IMPORTANT_EMAIL_SCAN_COUNT) AND whose oldest message is still newer than
# the OLD high-water mark proves nothing about whether even-older messages
# exist beyond what was fetched -- the mark must not advance past them, even
# though every fetched message WAS successfully classified this cycle.
GAP_UID = "gapuser-1"
users.upsert_user(GAP_UID, "gap@example.com", "Gap")
users.save_google_token(GAP_UID, '{"fake": "token"}')
OLD_MARK = 1_800_000_050_000
users.set_scan_high_water(GAP_UID, OLD_MARK)
db.IMPORTANT_EMAIL_DAILY_LIMIT = 100  # this scenario is about coverage, not the cap -- keep the cap out of the way
SCAN_COUNT = db.IMPORTANT_EMAIL_SCAN_COUNT
gap_emails = [
    {"id": f"gap-msg-{i}", "sender": f"S{i} <s{i}@example.com>", "subject": f"s{i}", "snippet": "y",
     "date_ms": OLD_MARK + 1000 + i, "source": "Gmail"}
    for i in range(SCAN_COUNT)
]
db.google_service.list_recent_emails, list_calls = _mk_list_recent(GAP_UID, gap_emails)
# REPLY (not NO) on purpose -- only a REPLY/YESNO verdict leaves behind a
# row for important_suggestion_exists to find later. A NO verdict leaves
# NO persisted trace at all, so it has no way to be remembered as "already
# seen" independent of the high-water mark -- while genuinely stuck behind
# an unresolved gap, NO-classified mail in a dormant top-N window WOULD
# still cost a fresh Groq call every cycle. That's a real, narrower residual
# cost this fix doesn't close; what it does close is exactly the data-loss
# bug (and, for the REPLY/YESNO subset, the redundant spend) below.
db._classify_important_email, classify_calls = _scripted_classify(["REPLY"] * SCAN_COUNT)
db._check_important_emails()
check(f"a full ({SCAN_COUNT}-message) fetch window whose OLDEST message is still newer than the old high-water mark is still classified in full (no data loss)",
      len(classify_calls) == SCAN_COUNT, classify_calls)
check("...but the high-water mark does NOT advance -- coverage isn't proven, there could be even older unfetched messages behind this window",
      users.get_scan_high_water(GAP_UID) == OLD_MARK, users.get_scan_high_water(GAP_UID))

# Next cycle, nothing changed in the inbox -- every one of those messages
# already has a suggestion row (they were REPLY-worthy), so NONE of them
# should reach Groq again -- this is what actually protects the shared
# budget while a user is stuck behind a gap, for the subset that CAN be
# remembered at all.
db.google_service.list_recent_emails, list_calls = _mk_list_recent(GAP_UID, gap_emails)
db._classify_important_email, classify_calls = _scripted_classify(["REPLY"] * SCAN_COUNT)
db._check_important_emails()
check("on the next cycle, the same stuck window costs ZERO Groq calls (every message in it already has a suggestion row)",
      len(classify_calls) == 0, classify_calls)

# The window finally stops being truncated (fewer than SCAN_COUNT, e.g. the
# backlog thinned out) -- coverage is now provable, so the mark advances.
db.google_service.list_recent_emails, list_calls = _mk_list_recent(GAP_UID, gap_emails[-3:])
db._classify_important_email, classify_calls = _scripted_classify(["NO"] * 3)
db._check_important_emails()
check("once the fetch is no longer truncated, the mark finally advances to the newest message seen",
      users.get_scan_high_water(GAP_UID) == gap_emails[-1]["date_ms"], users.get_scan_high_water(GAP_UID))
db.IMPORTANT_EMAIL_DAILY_LIMIT = 5

# Verdict matching: an ambiguous/garbled LLM response must be neither
# silently dropped as NO nor silently acted on as REPLY -- it's a logged,
# retryable failure either way.
VERDICT_UID = "verdict-1"
users.upsert_user(VERDICT_UID, "verdict@example.com", "Verdict")
users.save_google_token(VERDICT_UID, '{"fake": "token"}')
verdict_email = [{"id": "verdict-msg-1", "sender": "N <n@example.com>", "subject": "note", "snippet": "x",
                   "date_ms": 1_800_000_070_000, "source": "Gmail"}]

db.google_service.list_recent_emails, list_calls = _mk_list_recent(VERDICT_UID, verdict_email)
db._classify_important_email, classify_calls = _scripted_classify(["NOTE: this looks like spam"])
db._check_important_emails()
check("a verdict starting with 'NOTE' (not an exact 'NO') is NOT silently treated as NO or as REPLY -- it's a safe, logged retry",
      len(users.list_pending_suggestions(VERDICT_UID)) == 0 and users.get_scan_high_water(VERDICT_UID) is None,
      (users.list_pending_suggestions(VERDICT_UID), users.get_scan_high_water(VERDICT_UID)))

db.google_service.list_recent_emails, list_calls = _mk_list_recent(VERDICT_UID, verdict_email)
db._classify_important_email, classify_calls = _scripted_classify([""])
db._check_important_emails()
check("an empty/garbled verdict is never treated as REPLY by default (that would mean acting on noise)",
      len(users.list_pending_suggestions(VERDICT_UID)) == 0 and users.get_scan_high_water(VERDICT_UID) is None,
      (users.list_pending_suggestions(VERDICT_UID), users.get_scan_high_water(VERDICT_UID)))

db.google_service.list_recent_emails, list_calls = _mk_list_recent(VERDICT_UID, verdict_email)
db._classify_important_email, classify_calls = _scripted_classify(["NO"])
db._check_important_emails()
check("a clean, exact 'NO' verdict is still correctly recognized (the tightened match isn't overcorrected)",
      len(users.list_pending_suggestions(VERDICT_UID)) == 0 and users.get_scan_high_water(VERDICT_UID) == 1_800_000_070_000,
      (users.list_pending_suggestions(VERDICT_UID), users.get_scan_high_water(VERDICT_UID)))

# Clean slate: resolve every leftover 'pending'/'awaiting_choice' row the
# detection-section scenarios above left behind, since _draft_important_suggestions
# runs globally across ALL users -- without this, the drafting-section's own
# Groq-call-count assertions below would be inflated by unrelated rows.
for _uid in (DET_UID, RL_UID, RL_UID2, CAP_UID, CAP2_UID, GAP_UID, VERDICT_UID):
    for s in users.list_pending_suggestions(_uid):
        users.mark_suggestion_resolved(s["id"], _uid, "dismissed")

# ---------------------------------------------------------------------------- daily_briefing.py: drafting (Groq, conservative, never sends)
class _FakeMsg:
    def __init__(self, content): self.content = content
class _FakeChoice:
    def __init__(self, content): self.message = _FakeMsg(content)
class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = type("U", (), {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70})()

class _FakeGroqClient:
    """Mirrors just the one call shape _draft_important_suggestions makes:
    self.chat.completions.create(**kwargs) -> an object with .choices[0].message.content."""
    def __init__(self, content="Thanks for your email, I'll follow up properly soon.", raise_exc=None):
        self._content = content
        self._raise_exc = raise_exc
        self.calls = 0
        self.last_kwargs = None
        def _create(**k):
            self.calls += 1
            self.last_kwargs = k
            if self._raise_exc:
                raise self._raise_exc
            return _FakeCompletion(self._content)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))

db.groq_pacer.wait_if_needed = lambda *a, **k: None
db.groq_pacer.record_usage = lambda *a, **k: None

sid_draft = users.add_important_suggestion(UID, "msg-draft-1", "Boss", "boss@example.com", "Please review", "see attached", int(time.time() * 1000))

fake_client = _FakeGroqClient()
db._groq_client = fake_client
db._draft_important_suggestions()
drafted_row = users.get_suggestion(sid_draft, UID)
check("drafting advances the row to 'drafted' and fills in draft_subject/draft_body",
      drafted_row["status"] == "drafted" and drafted_row["draft_body"] == "Thanks for your email, I'll follow up properly soon.", drafted_row)
check("the draft subject is Re:-prefixed from the original subject", drafted_row["draft_subject"] == "Re: Please review", drafted_row)
check("drafting called Groq exactly once for this row", fake_client.calls == 1, fake_client.calls)
check("the drafting prompt asks the LLM to actually engage with the email's content, not just acknowledge it",
      "engage" in fake_client.last_kwargs["messages"][0]["content"]
      or "engage" in fake_client.last_kwargs["messages"][1]["content"], fake_client.last_kwargs)

calls_before_redraft = fake_client.calls
db._draft_important_suggestions()
check("an already-drafted row is never re-drafted (no duplicate Groq spend)",
      fake_client.calls == calls_before_redraft, fake_client.calls)

sid_choice_2 = users.add_important_suggestion(UID, "msg-choice-draft", "Dana", "dana@example.com", "Dinner?", "pizza tonight?", int(time.time() * 1000))
users.set_suggestion_awaiting_choice(sid_choice_2, "Do you want pizza tonight?")
calls_before_choice_draft = fake_client.calls
db._draft_important_suggestions()
check("an 'awaiting_choice' row is never auto-drafted by the background scheduler (it waits on the user's own pick)",
      fake_client.calls == calls_before_choice_draft
      and users.get_suggestion(sid_choice_2, UID)["status"] == "awaiting_choice")
users.mark_suggestion_resolved(sid_choice_2, UID, "dismissed")

sid_fail = users.add_important_suggestion(UID, "msg-draft-fail", "Boss", "boss@example.com", "Will fail", "x", int(time.time() * 1000))
db._groq_client = _FakeGroqClient(raise_exc=ValueError("boom"))
db._draft_important_suggestions()
failed_row = users.get_suggestion(sid_fail, UID)
check("a drafting exception leaves the row 'pending' (status unchanged) instead of crashing the scheduler",
      failed_row["status"] == "pending", failed_row)

db._groq_client = None
users.mark_suggestion_resolved(sid_fail, UID, "dismissed")
sid_nogroq = users.add_important_suggestion(UID, "msg-no-groq", "Boss", "boss@example.com", "x", "y", int(time.time() * 1000))
db._draft_important_suggestions()
check("with no Groq client configured (e.g. GROQ_API_KEY unset), drafting is a safe no-op",
      users.get_suggestion(sid_nogroq, UID)["status"] == "pending")
users.mark_suggestion_resolved(sid_nogroq, UID, "dismissed")
db._groq_client = fake_client

check("daily_briefing.py has no auto-send function left at all (sending is approval-only, from app.py)",
      not hasattr(db, "_send_ready_suggestions"))

# ---------------------------------------------------------------------------- app.py: the live routes
import app as jarvis

def _signed_in_client(uid):
    c = jarvis.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    return c

check("there is no /edit route left on the app (editing happens inside the confirm modal itself)",
      not any(r.rule.endswith("/edit") for r in jarvis.app.url_map.iter_rules() if "suggestions" in r.rule))

client = _signed_in_client(UID)

# The settings route is entirely gone -- this is now unconfigurable, always-on.
check("the old settings route no longer exists at all (404, not just disabled)",
      client.get("/api/settings/important-suggestions").status_code == 404)
check("...and neither does its POST", client.post("/api/settings/important-suggestions", json={}).status_code == 404)

send_calls = []
jarvis.productivity_service.request_send_email = lambda user_id, to, subject, body, **k: (
    send_calls.append((user_id, to, subject, body, k.get("provider"))),
    {"status": "confirmation_required", "token": "tok-123", "message": "Send email...", "kind": "email",
     "details": {"to": to, "subject": subject, "body": body, "provider": "Google", "attachments": []}},
)[-1]

# /send: 404s on a not-yet-drafted row -- nothing to send
sid_send = users.add_important_suggestion(UID, "msg-send-1", "Boss", "boss@example.com", "Send me", "see attached", int(time.time() * 1000))
r_send_too_early = client.post(f"/api/inbox/suggestions/{sid_send}/send")
check("sending a still-'pending' (not yet drafted) row 404s -- there's nothing to send yet",
      r_send_too_early.status_code == 404 and send_calls == [], (r_send_too_early.status_code, send_calls))

users.set_suggestion_draft(sid_send, "Re: Send me", "the drafted reply text")
r_send = client.post(f"/api/inbox/suggestions/{sid_send}/send")
data_send = r_send.get_json()
check("sending a drafted row calls the REAL confirm-to-act send-email path exactly once, pinned to provider=google",
      len(send_calls) == 1 and send_calls[0][1] == "boss@example.com" and send_calls[0][3] == "the drafted reply text"
      and send_calls[0][4] == "google", send_calls)
check("send's response is shaped exactly like every other send-email confirmation_required payload",
      data_send.get("status") == "confirmation_required" and data_send.get("kind") == "email" and "details" in data_send, data_send)
check("send's response carries the suggestion_id so the frontend can resolve it later",
      data_send.get("suggestion_id") == sid_send, data_send)

send_calls.clear()
r_send_again = client.post(f"/api/inbox/suggestions/{sid_send}/send")
check("a still-'drafted' (not yet resolved) row can be sent again if the first attempt wasn't approved/denied yet",
      len(send_calls) == 1, send_calls)

other_client = _signed_in_client(other_uid)
send_calls.clear()
r_send_other = other_client.post(f"/api/inbox/suggestions/{sid_send}/send")
check("a different signed-in user can't send someone else's suggestion",
      r_send_other.status_code == 404 and send_calls == [], (r_send_other.status_code, send_calls))

r_resolve = client.post(f"/api/inbox/suggestions/{sid_send}/resolve", json={"resolution": "sent"})
check("resolve route marks the row resolved", r_resolve.get_json().get("ok") is True, r_resolve.get_json())
check("...and it drops out of the pending list", all(s["id"] != sid_send for s in users.list_pending_suggestions(UID)))

send_calls.clear()
r_send_resolved = client.post(f"/api/inbox/suggestions/{sid_send}/send")
check("a resolved row can no longer be sent", r_send_resolved.status_code == 404 and send_calls == [], (r_send_resolved.status_code, send_calls))

sid_dismiss_pending = users.add_important_suggestion(UID, "msg-dismiss-pending", "Boss", "boss@example.com", "FYI", "no action needed", int(time.time() * 1000))
send_calls.clear()
r_dismiss_pending = client.post(f"/api/inbox/suggestions/{sid_dismiss_pending}/dismiss")
check("dismiss works on a still-'pending' (undrafted) row, with zero send calls",
      r_dismiss_pending.get_json().get("ok") is True and send_calls == [], (r_dismiss_pending.get_json(), send_calls))

sid_dismiss_drafted = users.add_important_suggestion(UID, "msg-dismiss-drafted", "Boss", "boss@example.com", "FYI2", "x", int(time.time() * 1000))
users.set_suggestion_draft(sid_dismiss_drafted, "Re: FYI2", "drafted text")
r_dismiss_drafted = client.post(f"/api/inbox/suggestions/{sid_dismiss_drafted}/dismiss")
check("dismiss also works on an already-'drafted' row", r_dismiss_drafted.get_json().get("ok") is True)
check("...and it drops out of the pending list", all(s["id"] != sid_dismiss_drafted for s in users.list_pending_suggestions(UID)))

anon = jarvis.app.test_client()
r_anon_send = anon.post(f"/api/inbox/suggestions/{sid_dismiss_pending}/send")
check("send route 401s when nobody is signed in", r_anon_send.status_code == 401, r_anon_send.status_code)
r_anon_dismiss = anon.post(f"/api/inbox/suggestions/{sid_dismiss_pending}/dismiss")
check("dismiss route 401s when nobody is signed in", r_anon_dismiss.status_code == 401, r_anon_dismiss.status_code)
r_anon_resolve = anon.post(f"/api/inbox/suggestions/{sid_dismiss_pending}/resolve", json={"resolution": "cancelled"})
check("resolve route 401s when nobody is signed in", r_anon_resolve.status_code == 401, r_anon_resolve.status_code)

# ---------------------------------------------------------------------------- app.py: /answer (the yes/no quick-choice route)
jarvis.groq_pacer.wait_if_needed = lambda *a, **k: None
jarvis.groq_pacer.record_usage = lambda *a, **k: None

sid_answer = users.add_important_suggestion(UID, "msg-answer-1", "Dana", "dana@example.com", "Dinner?", "pizza tonight?", int(time.time() * 1000))
r_answer_too_early = client.post(f"/api/inbox/suggestions/{sid_answer}/answer", json={"answer": True})
check("answering a still-'pending' row (not yet classified as a yes/no question) 404s",
      r_answer_too_early.status_code == 404, r_answer_too_early.status_code)

users.set_suggestion_awaiting_choice(sid_answer, "Do you want pizza tonight?")

r_answer_missing = client.post(f"/api/inbox/suggestions/{sid_answer}/answer", json={})
check("answering with no 'answer' field in the body 400s", r_answer_missing.status_code == 400, r_answer_missing.status_code)

r_answer_anon = anon.post(f"/api/inbox/suggestions/{sid_answer}/answer", json={"answer": True})
check("answer route 401s when nobody is signed in", r_answer_anon.status_code == 401, r_answer_anon.status_code)

r_answer_other = other_client.post(f"/api/inbox/suggestions/{sid_answer}/answer", json={"answer": True})
check("a different signed-in user can't answer someone else's suggestion",
      r_answer_other.status_code == 404, r_answer_other.status_code)

app_fake_client = _FakeGroqClient(content="Hey! Yes, pizza sounds great tonight.")
jarvis.groq_client = app_fake_client
r_answer_yes = client.post(f"/api/inbox/suggestions/{sid_answer}/answer", json={"answer": True})
data_answer_yes = r_answer_yes.get_json()
check("answering 'yes' drafts synchronously (not left to the 60s background scheduler) and returns the draft",
      r_answer_yes.status_code == 200 and data_answer_yes.get("ok") is True
      and data_answer_yes.get("draft_body") == "Hey! Yes, pizza sounds great tonight.", data_answer_yes)
check("the prompt sent to Groq conveys the chosen answer (YES) alongside the original question",
      "YES" in app_fake_client.last_kwargs["messages"][1]["content"]
      and "pizza tonight" in app_fake_client.last_kwargs["messages"][1]["content"], app_fake_client.last_kwargs)
answered_row = users.get_suggestion(sid_answer, UID)
check("after answering, the row itself has advanced all the way to 'drafted' (same as the normal auto-draft path)",
      answered_row["status"] == "drafted" and answered_row["draft_body"] == "Hey! Yes, pizza sounds great tonight.", answered_row)

send_calls.clear()
r_send_after_answer = client.post(f"/api/inbox/suggestions/{sid_answer}/send")
check("once answered and drafted, the row can go through the normal /send confirm-to-act path",
      r_send_after_answer.status_code == 200 and len(send_calls) == 1, (r_send_after_answer.status_code, send_calls))
client.post(f"/api/inbox/suggestions/{sid_answer}/resolve", json={"resolution": "sent"})

sid_answer_no = users.add_important_suggestion(UID, "msg-answer-2", "Dana", "dana@example.com", "Movie?", "movie tomorrow?", int(time.time() * 1000))
users.set_suggestion_awaiting_choice(sid_answer_no, "Do you want to see a movie tomorrow?")
app_fake_client2 = _FakeGroqClient(content="Thanks for asking, but I can't make it tomorrow.")
jarvis.groq_client = app_fake_client2
r_answer_no = client.post(f"/api/inbox/suggestions/{sid_answer_no}/answer", json={"answer": False})
check("answering 'no' also drafts successfully", r_answer_no.get_json().get("ok") is True, r_answer_no.get_json())
check("the prompt for a 'no' answer says NO, not YES",
      "NO" in app_fake_client2.last_kwargs["messages"][1]["content"], app_fake_client2.last_kwargs)
client.post(f"/api/inbox/suggestions/{sid_answer_no}/resolve", json={"resolution": "dismissed"})

sid_answer_err = users.add_important_suggestion(UID, "msg-answer-err", "Dana", "dana@example.com", "Trip?", "trip next week?", int(time.time() * 1000))
users.set_suggestion_awaiting_choice(sid_answer_err, "Are you free for a trip next week?")
jarvis.groq_client = _FakeGroqClient(raise_exc=ValueError("boom"))
r_answer_err = client.post(f"/api/inbox/suggestions/{sid_answer_err}/answer", json={"answer": True})
check("a drafting exception on /answer returns a clean 500 error instead of crashing",
      r_answer_err.status_code == 500, (r_answer_err.status_code, r_answer_err.get_json()))
check("...and the row stays 'awaiting_choice' (not silently advanced) so the user can try again",
      users.get_suggestion(sid_answer_err, UID)["status"] == "awaiting_choice")

jarvis.groq_client = _FakeGroqClient(raise_exc=_rate_limit_error("day"))
r_answer_rl_day = client.post(f"/api/inbox/suggestions/{sid_answer_err}/answer", json={"answer": True})
check("a day-limit RateLimitError on /answer returns 429 with an honest 'quota used up' message",
      r_answer_rl_day.status_code == 429 and "quota" in r_answer_rl_day.get_json().get("error", "").lower(), r_answer_rl_day.get_json())
users.mark_suggestion_resolved(sid_answer_err, UID, "dismissed")

# /api/suggestions carries pending suggestions (both 'pending' and 'drafted') -- NOT /api/inbox,
# which is the Mail tab's own route and was deliberately left untouched by this feature
# (the product owner was explicit the Mail tab itself must not change).
sid_inbox1 = users.add_important_suggestion(UID, "msg-inbox-check-1", "Boss", "boss@example.com", "Check", "x", int(time.time() * 1000))
sid_inbox2 = users.add_important_suggestion(UID, "msg-inbox-check-2", "Boss", "boss@example.com", "Check2", "x", int(time.time() * 1000))
users.set_suggestion_draft(sid_inbox2, "Re: Check2", "drafted body")
sid_inbox3 = users.add_important_suggestion(UID, "msg-inbox-check-3", "Dana", "dana@example.com", "Check3", "x", int(time.time() * 1000))
users.set_suggestion_awaiting_choice(sid_inbox3, "Is this a real question?")
jarvis.productivity_service.get_inbox_structured = lambda user_id, max_results=8: []
jarvis.productivity_service.get_inbox_summary_spoken = lambda emails: "no new mail"
r9 = client.get("/api/inbox")
check("/api/inbox (Mail tab) carries no 'suggestions' field at all -- left untouched", "suggestions" not in r9.get_json(), r9.get_json())
r9b = client.get("/api/suggestions")
data9 = r9b.get_json()
suggestion_ids = {s["id"] for s in data9.get("email_suggestions", [])}
check("/api/suggestions response includes a still-'pending' suggestion", sid_inbox1 in suggestion_ids, data9.get("email_suggestions"))
check("/api/suggestions response includes an already-'drafted' suggestion, with its draft text",
      any(s["id"] == sid_inbox2 and s["status"] == "drafted" and s["draft_body"] == "drafted body"
          for s in data9.get("email_suggestions", [])),
      data9.get("email_suggestions"))
check("/api/suggestions response includes an 'awaiting_choice' suggestion, with its detected question",
      any(s["id"] == sid_inbox3 and s["status"] == "awaiting_choice" and s["detected_question"] == "Is this a real question?"
          for s in data9.get("email_suggestions", [])),
      data9.get("email_suggestions"))

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + str(detail)[:300]))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
