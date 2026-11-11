# Proves the split that makes important-email suggestions safe: detection
# (_check_important_emails) and drafting (_draft_important_suggestions)
# both happen automatically in daily_briefing.py's background scheduler --
# but NEITHER of them ever sends anything. Sending only ever happens from a
# live, user-present request (app.py's /api/inbox/suggestions/<id>/send),
# going through the exact same confirm-to-act path
# (productivity_service.request_send_email) every other email in this app
# already uses, pinned to provider="google" since detection only ever
# happens via Gmail -- the reply must always go out through that SAME
# connected account, never a guessed one. The product owner was explicit,
# more than once, that this feature must never send without that approval.
import os, sys, time, tempfile
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
users.save_google_token(UID, '{"fake": "token"}')  # list_important_suggestion_candidates requires a connected token

# ---------------------------------------------------------------------------- users.py: settings round-trip
s0 = users.get_important_suggestions_settings(UID)
check("disabled by default, no senders", s0 == {"enabled": False, "senders": [], "enabled_at": None}, s0)

users.set_important_suggestions_settings(UID, True, ["boss@example.com", "hr@example.com"])
s1 = users.get_important_suggestions_settings(UID)
check("enabling stores senders and stamps enabled_at",
      s1["enabled"] is True and s1["senders"] == ["boss@example.com", "hr@example.com"] and s1["enabled_at"] is not None, s1)
enabled_at_1 = s1["enabled_at"]

users.set_important_suggestions_settings(UID, True, ["boss@example.com"])
s2 = users.get_important_suggestions_settings(UID)
check("ON->ON does NOT restamp enabled_at", s2["enabled_at"] == enabled_at_1, (s2["enabled_at"], enabled_at_1))

users.set_important_suggestions_settings(UID, False, ["boss@example.com"])
time.sleep(0.01)
users.set_important_suggestions_settings(UID, True, ["boss@example.com"])
s3 = users.get_important_suggestions_settings(UID)
check("OFF->ON DOES restamp enabled_at (never backfills mail from while it was off)",
      s3["enabled_at"] > enabled_at_1, (s3["enabled_at"], enabled_at_1))

cands = users.list_important_suggestion_candidates()
check("candidates list includes this enabled+connected user", len(cands) == 1 and cands[0]["user_id"] == UID, cands)

disconnected_uid = "no-token-user"
users.upsert_user(disconnected_uid, "nt@example.com", "NoToken")
users.set_important_suggestions_settings(disconnected_uid, True, ["x@example.com"])
cands2 = users.list_important_suggestion_candidates()
check("a user without a connected Google token is excluded even if enabled",
      all(c["user_id"] != disconnected_uid for c in cands2), cands2)

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

# ---------------------------------------------------------------------------- daily_briefing.py: detection (zero LLM calls)
import daily_briefing as db

users.set_important_suggestions_settings(UID, True, ["boss@example.com"])
settings_now = users.get_important_suggestions_settings(UID)

list_calls = []
def _fake_list_recent_emails(user_id, max_results=8, query="is:unread"):
    list_calls.append((user_id, query))
    return [
        {"id": "msg-new", "sender": "Boss <boss@example.com>", "subject": "Please review",
         "snippet": "see attached", "date_ms": int(time.time() * 1000), "source": "Gmail"},
    ]
db.google_service.list_recent_emails = _fake_list_recent_emails

before = len(users.list_pending_suggestions(UID))
db._check_important_emails()
after = users.list_pending_suggestions(UID)
check("a matching NEW message creates exactly one new pending suggestion",
      len(after) - before == 1, after)
check("the Gmail query used the user's own configured sender (from:)",
      list_calls and "from:boss@example.com" in list_calls[-1][1], list_calls)
check("the new row starts out 'pending' with no draft yet",
      len(after) == 1 and after[0]["status"] == "pending" and not after[0]["draft_body"], after)

calls_before = len(list_calls)
db._check_important_emails()
pending_after_rescan = users.list_pending_suggestions(UID)
check("re-scanning the SAME message (dedup by gmail_message_id) does not duplicate it",
      len(pending_after_rescan) == len(after), pending_after_rescan)

# backfill guard: a message dated BEFORE enabled_at must never become a suggestion
for s in after:
    users.mark_suggestion_resolved(s["id"], UID, "dismissed")
def _fake_list_old(user_id, max_results=8, query="is:unread"):
    return [{"id": "msg-old", "sender": "Boss <boss@example.com>", "subject": "Old",
              "snippet": "from before the toggle was on", "date_ms": int((settings_now["enabled_at"] - 999) * 1000), "source": "Gmail"}]
db.google_service.list_recent_emails = _fake_list_old
before_old = len(users.list_pending_suggestions(UID))
db._check_important_emails()
check("a message dated before enabled_at is never turned into a suggestion (no surprise backfill)",
      len(users.list_pending_suggestions(UID)) == before_old, users.list_pending_suggestions(UID))

# daily cap
db.google_service.list_recent_emails = _fake_list_recent_emails  # back to the "new matching message" fake
db.IMPORTANT_EMAIL_DAILY_LIMIT = 1
count_before_cap = users.count_suggestions_created_since(UID, 0)
for gid in ("cap-msg-1", "cap-msg-2", "cap-msg-3"):
    def _mk(gid):
        def _f(user_id, max_results=8, query="is:unread"):
            return [{"id": gid, "sender": "Boss <boss@example.com>", "subject": "x", "snippet": "y",
                      "date_ms": int(time.time() * 1000), "source": "Gmail"}]
        return _f
    db.google_service.list_recent_emails = _mk(gid)
    db._check_important_emails()
count_created = users.count_suggestions_created_since(UID, 0)
check(f"daily cap (={db.IMPORTANT_EMAIL_DAILY_LIMIT}) stops new rows even with more matching mail available",
      count_created - count_before_cap <= db.IMPORTANT_EMAIL_DAILY_LIMIT, (count_before_cap, count_created))
db.IMPORTANT_EMAIL_DAILY_LIMIT = 5

# ---------------------------------------------------------------------------- daily_briefing.py: drafting (Groq, conservative, never sends)
import types

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
        def _create(**k):
            self.calls += 1
            if self._raise_exc:
                raise self._raise_exc
            return _FakeCompletion(self._content)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=_create))

db.groq_pacer.wait_if_needed = lambda *a, **k: None
db.groq_pacer.record_usage = lambda *a, **k: None

for s in users.list_pending_suggestions(UID):
    users.mark_suggestion_resolved(s["id"], UID, "dismissed")  # clean slate before the drafting tests
sid_draft = users.add_important_suggestion(UID, "msg-draft-1", "Boss", "boss@example.com", "Please review", "see attached", int(time.time() * 1000))

fake_client = _FakeGroqClient()
db._groq_client = fake_client
db._draft_important_suggestions()
drafted_row = users.get_suggestion(sid_draft, UID)
check("drafting advances the row to 'drafted' and fills in draft_subject/draft_body",
      drafted_row["status"] == "drafted" and drafted_row["draft_body"] == "Thanks for your email, I'll follow up properly soon.", drafted_row)
check("the draft subject is Re:-prefixed from the original subject", drafted_row["draft_subject"] == "Re: Please review", drafted_row)
check("drafting called Groq exactly once for this row", fake_client.calls == 1, fake_client.calls)

calls_before_redraft = fake_client.calls
db._draft_important_suggestions()
check("an already-drafted row is never re-drafted (no duplicate Groq spend)",
      fake_client.calls == calls_before_redraft, fake_client.calls)

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

# ---------------------------------------------------------------------------- app.py: the live routes (send / dismiss / resolve)
import app as jarvis

def _signed_in_client(uid):
    c = jarvis.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    return c

check("there is no /edit route left on the app (editing happens inside the confirm modal itself)",
      not any(r.rule.endswith("/edit") for r in jarvis.app.url_map.iter_rules() if "suggestions" in r.rule))

# settings GET/POST round-trip through the real route
client = _signed_in_client(UID)
r = client.get("/api/settings/important-suggestions")
check("settings GET returns 200", r.status_code == 200, r.status_code)
r2 = client.post("/api/settings/important-suggestions", json={"enabled": True, "senders": "Manager@Example.com, HR@example.com"})
check("settings POST accepts a comma-separated string and lowercases it",
      r2.get_json()["senders"] == ["manager@example.com", "hr@example.com"], r2.get_json())
r3 = client.post("/api/settings/important-suggestions", json={"enabled": True, "senders": "not-an-email"})
check("settings POST rejects an invalid address with 400", r3.status_code == 400, r3.get_json())
r4 = client.post("/api/settings/important-suggestions", json={"enabled": True, "senders": ",".join(f"a{i}@example.com" for i in range(6))})
check("settings POST rejects more than 5 addresses", r4.status_code == 400, r4.get_json())

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

# /resolve: called after the confirm modal's own approve/deny resolves
r_resolve = client.post(f"/api/inbox/suggestions/{sid_send}/resolve", json={"resolution": "sent"})
check("resolve route marks the row resolved", r_resolve.get_json().get("ok") is True, r_resolve.get_json())
check("...and it drops out of the pending list", all(s["id"] != sid_send for s in users.list_pending_suggestions(UID)))

send_calls.clear()
r_send_resolved = client.post(f"/api/inbox/suggestions/{sid_send}/send")
check("a resolved row can no longer be sent", r_send_resolved.status_code == 404 and send_calls == [], (r_send_resolved.status_code, send_calls))

# /dismiss: cancels outright at any stage, no draft required, never calls send
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

# auth gate: no session -> 401, not a leak of someone else's suggestion
anon = jarvis.app.test_client()
r_anon_send = anon.post(f"/api/inbox/suggestions/{sid_dismiss_pending}/send")
check("send route 401s when nobody is signed in", r_anon_send.status_code == 401, r_anon_send.status_code)
r_anon_dismiss = anon.post(f"/api/inbox/suggestions/{sid_dismiss_pending}/dismiss")
check("dismiss route 401s when nobody is signed in", r_anon_dismiss.status_code == 401, r_anon_dismiss.status_code)
r_anon_resolve = anon.post(f"/api/inbox/suggestions/{sid_dismiss_pending}/resolve", json={"resolution": "cancelled"})
check("resolve route 401s when nobody is signed in", r_anon_resolve.status_code == 401, r_anon_resolve.status_code)

# /api/suggestions carries pending suggestions (both 'pending' and 'drafted') -- NOT /api/inbox,
# which is the Mail tab's own route and was deliberately left untouched by this feature
# (the product owner was explicit the Mail tab itself must not change).
sid_inbox1 = users.add_important_suggestion(UID, "msg-inbox-check-1", "Boss", "boss@example.com", "Check", "x", int(time.time() * 1000))
sid_inbox2 = users.add_important_suggestion(UID, "msg-inbox-check-2", "Boss", "boss@example.com", "Check2", "x", int(time.time() * 1000))
users.set_suggestion_draft(sid_inbox2, "Re: Check2", "drafted body")
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

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + str(detail)[:300]))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
