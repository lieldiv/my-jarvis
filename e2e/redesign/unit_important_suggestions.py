# Proves the split that makes important-email suggestions safe: detection
# (daily_briefing.py's scheduler) only ever reads Gmail and writes a local
# row -- it never calls Groq and never sends anything. Drafting and sending
# both stay inside live, user-present requests (app.py's
# /api/inbox/suggestions/<id>/review), going through the exact same
# confirm-to-act path (productivity_service.request_send_email) every other
# email in this app already uses.
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

# ---------------------------------------------------------------------------- users.py: suggestion rows
sid = users.add_important_suggestion(UID, "msg-1", "Boss", "boss@example.com", "Urgent", "please review", 1_700_000_000_000)
check("a new suggestion gets a real row id", sid is not None, sid)
dup = users.add_important_suggestion(UID, "msg-1", "Boss", "boss@example.com", "Urgent", "please review", 1_700_000_000_000)
check("dedup: same gmail_message_id for the same user returns None, no second row", dup is None, dup)
check("list_pending_suggestions shows exactly the one row", len(users.list_pending_suggestions(UID)) == 1)

resolved = users.mark_suggestion_resolved(sid, UID, "dismissed")
check("resolving a pending suggestion succeeds", resolved is True)
check("...and it drops out of the pending list", users.list_pending_suggestions(UID) == [])
resolved_again = users.mark_suggestion_resolved(sid, UID, "sent")
check("resolving an already-resolved row is a safe no-op (returns False, doesn't clobber the resolution)", resolved_again is False)

other_uid = "other-person"
users.upsert_user(other_uid, "other@example.com", "Other")
check("ownership scoping: a different user can't read someone else's suggestion",
      users.get_suggestion(sid, other_uid) is None)

# ---------------------------------------------------------------------------- daily_briefing.py: detection only
import daily_briefing as db

users.set_important_suggestions_settings(UID, True, ["boss@example.com"])
settings_now = users.get_important_suggestions_settings(UID)

groq_calls = []
# If _check_important_emails ever calls anything Groq-shaped, this would be it --
# it isn't imported here at all, so this just documents "no such call site exists".
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
check("sender name/email parsed correctly from the From header",
      after[0]["sender_name"] == "Boss" and after[0]["sender_email"] == "boss@example.com", after[0])

calls_before = len(list_calls)
db._check_important_emails()
pending_after_rescan = users.list_pending_suggestions(UID)
check("re-scanning the SAME message (dedup by gmail_message_id) does not duplicate it",
      len(pending_after_rescan) == len(after), pending_after_rescan)

# backfill guard: a message dated BEFORE enabled_at must never become a suggestion
users.mark_suggestion_resolved(after[0]["id"], UID, "dismissed")
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
      count_created <= db.IMPORTANT_EMAIL_DAILY_LIMIT + 1,  # +1 accounts for the one row already created earlier in this script
      count_created)
db.IMPORTANT_EMAIL_DAILY_LIMIT = 5

# ---------------------------------------------------------------------------- app.py: the live routes
import app as jarvis

def _signed_in_client(uid):
    c = jarvis.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    return c

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

# fresh suggestion for the review/dismiss/resolve route tests
sid2 = users.add_important_suggestion(UID, "msg-review", "Boss", "boss@example.com", "Please review", "see attached", int(time.time() * 1000))

send_calls = []
jarvis.productivity_service.request_send_email = lambda user_id, to, subject, body, **k: (
    send_calls.append((user_id, to, subject, body)),
    {"status": "confirmation_required", "token": "tok-123", "message": "Send email...", "kind": "email",
     "details": {"to": to, "subject": subject, "body": body, "provider": "Google", "attachments": []}},
)[-1]

class _FakeMsg:
    def __init__(self, content): self.content = content
class _FakeChoice:
    def __init__(self, content): self.message = _FakeMsg(content)
class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]
        self.usage = type("U", (), {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70,
                                     "prompt_tokens_details": None})()
jarvis.groq_client.chat.completions.create = lambda **k: _FakeCompletion("Thanks for your email, I'll follow up properly soon.")
jarvis.groq_pacer.wait_if_needed = lambda *a, **k: None

r5 = client.post(f"/api/inbox/suggestions/{sid2}/review")
data5 = r5.get_json()
check("review drafts lazily then calls the REAL confirm-to-act send-email path exactly once",
      len(send_calls) == 1 and send_calls[0][1] == "boss@example.com", send_calls)
check("review's response is shaped exactly like every other send-email confirmation_required payload",
      data5.get("status") == "confirmation_required" and data5.get("kind") == "email" and "details" in data5, data5)
check("review's response carries the suggestion_id so the frontend can resolve it later",
      data5.get("suggestion_id") == sid2, data5)
check("the conservative draft got persisted onto the row (so re-reviewing doesn't redraft/re-spend Groq)",
      users.get_suggestion(sid2, UID)["draft_body"] is not None, users.get_suggestion(sid2, UID))

send_calls.clear()
r6 = client.post(f"/api/inbox/suggestions/{sid2}/review")
check("reviewing an already-drafted suggestion does NOT draft again (no second Groq call needed) but still sends",
      len(send_calls) == 1, send_calls)

r7 = client.post(f"/api/inbox/suggestions/{sid2}/resolve", json={"resolution": "sent"})
check("resolve route marks the row resolved", r7.get_json().get("ok") is True, r7.get_json())
check("...and it drops out of the pending list", all(s["id"] != sid2 for s in users.list_pending_suggestions(UID)))

sid3 = users.add_important_suggestion(UID, "msg-dismiss", "Boss", "boss@example.com", "FYI", "no action needed", int(time.time() * 1000))
send_calls.clear()
r8 = client.post(f"/api/inbox/suggestions/{sid3}/dismiss")
check("dismiss route resolves without ever drafting (zero Groq/send calls)",
      r8.get_json().get("ok") is True and send_calls == [], (r8.get_json(), send_calls))

# /api/inbox carries pending suggestions alongside the normal mail list, no new polling
sid4 = users.add_important_suggestion(UID, "msg-inbox-check", "Boss", "boss@example.com", "Check", "x", int(time.time() * 1000))
jarvis.productivity_service.get_inbox_structured = lambda user_id, max_results=8: []
jarvis.productivity_service.get_inbox_summary_spoken = lambda emails: "no new mail"
r9 = client.get("/api/inbox")
data9 = r9.get_json()
check("/api/inbox response includes the pending suggestion",
      any(s["id"] == sid4 for s in data9.get("suggestions", [])), data9.get("suggestions"))

# auth gate: no session -> 401, not a leak of someone else's suggestion
anon = jarvis.app.test_client()
r10 = anon.post(f"/api/inbox/suggestions/{sid4}/review")
check("review route 401s when nobody is signed in", r10.status_code == 401, r10.status_code)

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + str(detail)[:300]))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
