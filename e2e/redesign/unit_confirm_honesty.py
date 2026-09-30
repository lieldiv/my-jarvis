# The most serious bug found today, reported live by the user with a screenshot: reject (or a
# genuinely failed approve) on a calendar/email confirmation still showed a fixed "success" message,
# because resolve_confirmation() trusted approved_message unconditionally without ever looking at
# what the callback (google_service.delete_calendar_event etc.) actually returned -- and those
# functions catch their own provider errors and return an English sentence instead of raising.
# This proves the fix for all four affected actions (create/update/delete calendar event, send_email)
# using the REAL functions in productivity_service.py + guardrails.py, only the provider clients
# (google_service.*/microsoft_service.*) are swapped for fakes -- so a regression in how
# request_confirmation/resolve_confirmation wire success_prefix through would be caught here even if
# nobody remembers to add it for a future 5th action.
import os, sys
os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # the repo root

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

import guardrails
import productivity_service as ps

ps.google_service.CONFIGURED = True


def resolve(user_id, event_id="e1", summary="Test event", approve=True):
    d = ps.request_delete_calendar_event(user_id, event_id, summary)
    return guardrails.resolve_confirmation(d["token"], approve)


# ---------------------------------------------------------------------------- the exact bug reported
ps.google_service.delete_calendar_event = lambda user_id, event_id: (
    "I couldn't cancel that event, sir — Google Calendar refused the request. Please try again shortly."
)
msg = resolve("u1")
check('a REAL delete failure (the provider function caught its own error and returned a sentence, as it always does) is shown honestly -- NOT "✅ ביטלתי"',
      msg == "I couldn't cancel that event, sir — Google Calendar refused the request. Please try again shortly.", msg)
check("...specifically, the false-success checkmark text never appears", "ביטלתי" not in msg, msg)

# the genuine success case must still show the nice Hebrew message (this is not a security fix that
# should make the happy path worse)
ps.google_service.delete_calendar_event = lambda user_id, event_id: "Cancelled that event on your Google Calendar, sir."
msg = resolve("u1", summary="Real success")
check("a REAL success still shows the nicer approved_message, not the raw provider sentence",
      msg == "✅ ביטלתי את 'Real success'.", msg)

# "not connected" is also a failure sentence from the same function, same bug class
ps.google_service.delete_calendar_event = lambda user_id, event_id: "Google Calendar isn't connected, sir — please sign in again."
msg = resolve("u1")
check('"not connected" is shown honestly too, not as a false success', "isn't connected" in msg and "ביטלתי" not in msg, msg)

# create / update / send_email -- same bug class, same fix, checked independently
ps.google_service.create_calendar_event = lambda *a, **k: "I couldn't create that event on Google Calendar, sir — the request was refused. Please try again shortly."
r = ps.request_create_calendar_event("u1", "Meeting", "2026-10-01T10:00:00+03:00", "2026-10-01T11:00:00+03:00")
msg = guardrails.resolve_confirmation(r["token"], True)
check("create: a real failure is shown honestly, not '✅ יצרתי'", "couldn't create" in msg and "יצרתי" not in msg, msg)
ps.google_service.create_calendar_event = lambda *a, **k: "Created 'Meeting' on your Google Calendar, sir."
r = ps.request_create_calendar_event("u1", "Meeting", "2026-10-01T10:00:00+03:00", "2026-10-01T11:00:00+03:00")
msg = guardrails.resolve_confirmation(r["token"], True)
check("create: a real success still shows the nice message", msg == "✅ יצרתי את 'Meeting'.", msg)

# request_update_calendar_event resolves "the meeting" to a real event via google_service.list_calendar_events
# first, THEN proposes the time change -- mock that lookup so it finds exactly one match.
ps.google_service.list_calendar_events = lambda user_id, tmin, tmax, max_results=20: [
    {"id": "e1", "summary": "Meeting", "start": "2026-10-01T10:00:00+03:00", "end": "2026-10-01T10:30:00+03:00", "location": ""},
]

def resolve_update(approve=True):
    r = ps.request_update_calendar_event("u1", "Meeting", new_start_iso="2026-10-01T11:00:00+03:00", new_end_iso="2026-10-01T11:30:00+03:00")
    assert r["status"] == "confirmation_required", r  # a bug in the mock setup, not the thing under test, if this ever fires
    return guardrails.resolve_confirmation(r["token"], approve)

ps.google_service.update_calendar_event = lambda *a, **k: "I couldn't update that event, sir — Google Calendar refused the request. Please try again shortly."
msg = resolve_update()
check("update: a real failure is shown honestly, not '✅ עדכנתי'", "couldn't update" in msg and "עדכנתי" not in msg, msg)
# the no-op case ("nothing to update") must also not be reported as a success
ps.google_service.update_calendar_event = lambda *a, **k: "Nothing to update, sir — no new time was given."
msg = resolve_update()
check("update: a genuine no-op is shown honestly too, not as a false success", msg == "Nothing to update, sir — no new time was given.", msg)
ps.google_service.update_calendar_event = lambda *a, **k: "Updated that event on your Google Calendar, sir."
msg = resolve_update()
check("update: a real success still shows the nice message", msg == "✅ עדכנתי את 'Meeting'.", msg)

ps.google_service.send_email = lambda *a, **k: "I couldn't send that email, sir — Gmail refused the request. Please try again shortly."
r = ps.request_send_email("u1", "dana@example.com", "Hi", "body")
msg = guardrails.resolve_confirmation(r["token"], True)
check("send_email: a real failure is shown honestly, not '✅ שלחתי'", "couldn't send" in msg and "שלחתי" not in msg, msg)
ps.google_service.send_email = lambda *a, **k: "Sent the email to dana@example.com, sir."
r = ps.request_send_email("u1", "dana@example.com", "Hi", "body")
msg = guardrails.resolve_confirmation(r["token"], True)
check("send_email: a real success still shows the nice message", msg == "✅ שלחתי את הדוא״ל ל-dana@example.com.", msg)

# ---------------------------------------------------------------------------- unaffected paths stay unaffected
# A callback that only ever raises (never returns an error sentence) and has no approved_message at
# all -- file_tools.py's pattern -- must be completely unaffected by this change.
def _raises(*a, **k): raise RuntimeError("disk full")
token = guardrails.request_confirmation("delete a file", _raises, meta={"user_id": "u1"})
msg = guardrails.resolve_confirmation(token, True)
check("a raising callback with no approved_message override still gets the generic honest failure sentence (file_tools.py's pattern, unaffected by this fix)",
      "failed after you approved it" in msg, msg)

def _ok(*a, **k): return "Deleted, sir."
token = guardrails.request_confirmation("delete a file", _ok, meta={"user_id": "u1"})
msg = guardrails.resolve_confirmation(token, True)
check("a plain-string-returning callback with no approved_message override still passes its own result straight through",
      msg == "Deleted, sir.", msg)

# approved_message with NO success_prefix at all (legacy shape, if anything still uses it that way)
# must keep the OLD unconditional-trust behaviour exactly -- this fix is additive, not a breaking change
token = guardrails.request_confirmation("x", lambda: "raw result", approved_message="legacy override, no prefix given")
msg = guardrails.resolve_confirmation(token, True)
check("approved_message with no success_prefix at all is still trusted unconditionally (backward compatible)",
      msg == "legacy override, no prefix given", msg)

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + str(detail)[:300]))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
