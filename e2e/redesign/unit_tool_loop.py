# run_llm's own control-flow correctness -- the mechanics of the tool-calling loop itself, not any
# one tool. Two review-agent-found-and-reproduced bugs, fixed; this proves the fix, not just that
# the old behaviour changed.
import os, sys, json, types
os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
os.environ["JARVIS_DB_PATH"] = os.path.join(__import__("tempfile").mkdtemp(), "toolloop_test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # the repo root

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

import users
users._init_db()
users.upsert_user("u1", "someone@example.com", "Someone")
import app as jarvis

def _call(call_id, name, args):
    return types.SimpleNamespace(id=call_id, function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))
def _msg(content, tool_calls):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=tool_calls))])

# ---------------------------------------------------------------------------- compound-request fix
# A terminal tool (add_task) and a non-terminal one (get_tasks) in the SAME round used to make the
# non-terminal result vanish -- reproduced directly by a review agent. Two rounds scripted: round 1
# fires both calls, round 2 is where a correctly-fixed loop lets the model speak both results.
calls_seen = []
def _fake_complete(messages):
    calls_seen.append(len(messages))
    if len(calls_seen) == 1:
        return _msg(None, [_call("c1", "get_tasks", {}), _call("c2", "add_task", {"text": "buy milk"})])
    return _msg("You have no tasks yet, and I've added 'buy milk' for you, sir.", None)
jarvis._complete_with_retry = _fake_complete
jarvis.productivity_service.request_add_task = lambda user_id, text, recurring_day=None: f"Added '{text}' to your tasks, sir."
calls_seen.clear()
final = jarvis.run_llm("what's on my task list, and also add buy milk", "u1")
check("a mixed terminal+non-terminal round reaches a SECOND Groq call (doesn't short-circuit)", len(calls_seen) == 2, calls_seen)
check("...and the final answer is the model's own round-2 synthesis of BOTH halves, not just the terminal tool's canned reply",
      final == "You have no tasks yet, and I've added 'buy milk' for you, sir.", final)

# control: a round where EVERY call is terminal still takes the fast path (no wasted second round) --
# confirms the fix didn't regress the original optimization this file's own comment describes.
calls_seen.clear()
jarvis._complete_with_retry = lambda messages: (calls_seen.append(1), _msg(None, [_call("c3", "add_task", {"text": "a"}), _call("c4", "add_task", {"text": "b"})]))[-1]
final2 = jarvis.run_llm("add two tasks", "u1")
check("an ALL-terminal round still short-circuits in one Groq call (fast path preserved)", len(calls_seen) == 1, len(calls_seen))
check("...joining both terminal replies, like before", final2 == "Added 'a' to your tasks, sir. Added 'b' to your tasks, sir.", final2)

# control: a round where EVERY call is non-terminal already worked before this fix and must still work
calls_seen.clear()
def _fake_complete2(messages):
    calls_seen.append(1)
    if len(calls_seen) == 1:
        return _msg(None, [_call("c5", "get_weather", {"location": "Tel Aviv"}), _call("c6", "calculate", {"expression": "2+2"})])
    return _msg("It's sunny in Tel Aviv, and 2+2 is 4, sir.", None)
jarvis._complete_with_retry = _fake_complete2
jarvis._STATIC_TOOL_IMPL["get_weather"] = lambda args: "Sunny in Tel Aviv, sir."
final3 = jarvis.run_llm("weather in Tel Aviv, and what's 2+2", "u1")
check("an all-non-terminal round still reaches round 2 and synthesizes both (unchanged by the fix)",
      len(calls_seen) == 2 and final3 == "It's sunny in Tel Aviv, and 2+2 is 4, sir.", (calls_seen, final3))

# ---------------------------------------------------------------------------- leaked-JSON detection, broadened
leaked_fenced = '```json\n{"name": "set_reminder", "arguments": {"text": "x"}}\n```'
leaked_bare = '{"name": "set_reminder", "arguments": {"text": "x"}}'
check("fenced leak still detected (unchanged)", jarvis._looks_like_leaked_tool_call(leaked_fenced))
check("UNFENCED leak is now also detected (was the gap a review agent found and reproduced)", jarvis._looks_like_leaked_tool_call(leaked_bare))
check("ordinary prose containing a brace is NOT a false positive", not jarvis._looks_like_leaked_tool_call("the set is {1, 2, 3}, sir."))
check("empty/None text is handled", not jarvis._looks_like_leaked_tool_call("") and not jarvis._looks_like_leaked_tool_call(None))

# end to end: an unfenced leak on round 1 now gets the SAME corrective nudge a fenced one already got,
# instead of reaching the user as raw JSON.
calls_seen.clear()
def _fake_complete3(messages):
    calls_seen.append(1)
    if len(calls_seen) == 1:
        return _msg(leaked_bare, None)
    return _msg("I've set a reminder for you, sir.", None)
jarvis._complete_with_retry = _fake_complete3
final4 = jarvis.run_llm("remind me about x", "u1")
check("an unfenced leaked tool call now gets a corrective nudge (2 rounds) instead of reaching the user verbatim",
      len(calls_seen) == 2 and final4 == "I've set a reminder for you, sir.", (calls_seen, final4))
check("...specifically, the raw JSON text itself never became the final answer", "arguments" not in final4 and "{" not in final4, final4)

# ---------------------------------------------------------------------------- TERMINAL_TOOLS additions
for name in ("calculate", "get_market_summary", "write_workspace_file", "delete_workspace_path"):
    check(f"'{name}' is now in TERMINAL_TOOLS (was a wasted extra Groq round every call before)", name in jarvis.TERMINAL_TOOLS)

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + str(detail)[:300]))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
