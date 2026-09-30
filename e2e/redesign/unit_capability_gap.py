# Proves the split that makes report_capability_gap safe to expose to untrusted conversations:
# the LIVE tool call (app.py) only ever writes a local row -- never touches google_service /
# credentials / network -- and the DIGEST sender (daily_briefing.py) only marks a row notified
# once it has actually gone out through the ADMIN's own connected account.
import os, sys, time, tempfile
os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
os.environ["JARVIS_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "capgap_test.db")  # isolated, throwaway
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # the repo root

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

import users
users._init_db()
users.upsert_user("reporter-1", "someone@example.com", "Someone")
users.upsert_user("admin-1", "lieldiv28@gmail.com", "Admin")

# ---------------------------------------------------------------------------- the live path (app.py)
import app as jarvis

send_email_calls = []
jarvis.google_service.send_email = lambda *a, **k: (send_email_calls.append((a, k)), "Sent the email to x, sir.")[-1]
jarvis.google_service.CONFIGURED = True

before = len(users.get_unnotified_capability_gaps(limit=999))
result = jarvis._report_capability_gap("reporter-1", "can you order me a pizza on Wolt",
                                        {"user_request": "order pizza on Wolt", "reason": "no delivery-app tool exists"})
check("the tool call returns without ever calling google_service.send_email", send_email_calls == [], send_email_calls)
check("...and its own return value says so, not a spoken sentence", result == "(logged for the developer)", result)
gaps = users.get_unnotified_capability_gaps(limit=999)
check("...exactly one new row was logged", len(gaps) - before == 1, len(gaps) - before)
check("...carrying the reporter's user_id and the model's own wording", gaps[-1]["user_id"] == "reporter-1" and gaps[-1]["reason"] == "no delivery-app tool exists", gaps[-1])

# cooldown: a second call from the SAME user within the window logs nothing new
before2 = len(users.get_unnotified_capability_gaps(limit=999))
r2 = jarvis._report_capability_gap("reporter-1", "again", {"user_request": "x", "reason": "y"})
check("cooldown blocks an immediate repeat from the same user (no spam loop)", r2 == "(already logged recently)" and len(users.get_unnotified_capability_gaps(limit=999)) == before2)
jarvis._last_gap_logged_at.clear()  # reset for the next section

check("report_capability_gap is NOT a terminal tool (the model still composes its own final answer)",
      "report_capability_gap" not in jarvis.TERMINAL_TOOLS)

# tool dispatch actually reaches this function with the real user_text (not just the model's paraphrase)
impl = jarvis._build_tool_impl("reporter-1", [], "jarvis", "call my dentist and book an appointment")
out = impl["report_capability_gap"]({"user_request": "book a dentist appointment", "reason": "no phone/booking tool"})
check("registered in the live dispatch table with the turn's real user_text closed over", out == "(logged for the developer)", out)

# ---------------------------------------------------------------------------- the digest (daily_briefing.py)
import daily_briefing as db
db.ADMIN_ALERT_EMAIL = "lieldiv28@gmail.com"

# admin not connected yet: nothing should be marked notified, even though rows exist
db.google_service.is_connected = lambda uid: False
pending_before = len(users.get_unnotified_capability_gaps(limit=999))
db._check_capability_gaps()
check("admin not connected -> digest skipped, NOTHING marked notified (rows survive for later)",
      len(users.get_unnotified_capability_gaps(limit=999)) == pending_before, "rows lost despite no send!")

# now connect the admin and actually "send"
db.google_service.is_connected = lambda uid: True
sent = []
db.google_service.send_email = lambda uid, to, subject, body: (sent.append((uid, to, subject, body)), "Sent the email to x, sir.")[-1]
db._check_capability_gaps()
check("admin connected -> exactly one digest email sent, to the admin's own address", len(sent) == 1 and sent[0][1] == "lieldiv28@gmail.com", sent)
check("...sent THROUGH the admin's own stored connection, not the reporting user's", sent[0][0] == "admin-1", sent[0])
body = sent[0][3]
check("...the digest names the actual reporting user and their request", "someone@example.com" in body and "order pizza on Wolt" in body, body[:300])
check("...every previously-unnotified row is now marked notified", users.get_unnotified_capability_gaps(limit=999) == [], users.get_unnotified_capability_gaps(limit=999))

# a send that raises must NOT mark rows notified (retried next cycle, same as the "not connected" case)
users.log_capability_gap("reporter-1", "translate this PDF", "no translation tool", "raw")
def _boom(*a, **k): raise RuntimeError("Gmail API down")
db.google_service.send_email = _boom
pending_before2 = len(users.get_unnotified_capability_gaps(limit=999))
db._check_capability_gaps()
check("a failed send raises and leaves the row unmarked (picked up again next cycle, not lost)",
      len(users.get_unnotified_capability_gaps(limit=999)) == pending_before2, "row silently lost on a failed send")

# ---------------------------------------------------------------------------- end to end through run_llm itself
# Not just the dispatch table in isolation: the real loop in run_llm, with a scripted two-round
# Groq exchange, proving what actually reaches the user is the model's OWN second-round answer --
# never the tool's internal "(logged for the developer)" string -- and that logging one more gap
# mid-conversation still never touches google_service.
import json, types

def _tool_call(call_id, name, args):
    return types.SimpleNamespace(id=call_id, function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))

_responses = [
    types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
        content=None,
        tool_calls=[_tool_call("c1", "report_capability_gap", {"user_request": "call my dentist", "reason": "no phone/booking tool"})],
    ))]),
    types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(
        content="I'm not able to place phone calls, sir — you'll need to call the dentist yourself.",
        tool_calls=None,
    ))]),
]
jarvis._complete_with_retry = lambda messages: _responses.pop(0)
jarvis.google_service.send_email = lambda *a, **k: results.append(("!! send_email called during run_llm !!", False, str((a, k)))) or "Sent the email to x, sir."
jarvis._last_gap_logged_at.clear()

before3 = len(users.get_unnotified_capability_gaps(limit=999))
final = jarvis.run_llm("can you call my dentist and book me an appointment for tomorrow", "reporter-1")
check("the user-facing reply is the model's own round-2 sentence, not internal bookkeeping text",
      final == "I'm not able to place phone calls, sir — you'll need to call the dentist yourself.", final)
check("...and the gap was logged as a real row through the actual run_llm loop",
      len(users.get_unnotified_capability_gaps(limit=999)) - before3 == 1)

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + str(detail)[:300]))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
