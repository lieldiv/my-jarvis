# Verifies the fix for a real production problem: a paying client's JARVIS (deployed on Render,
# shared by every signed-in user through ONE Groq account) kept saying "I've hit my usage limit
# with Groq" even on light use. Root cause measured directly: the system prompt + time/shortcuts
# context + ACTIVE_TOOLS schema alone cost ~6,275 tokens on EVERY round of EVERY request -- close
# to or exceeding the entire shared 8,000-tokens/minute free-tier budget from a single round of a
# single user's single turn, before counting history, the user's own message, or completion
# tokens. This proves (1) groq_pacer's sliding-window logic actually paces calls, (2) it correctly
# tells a brief per-minute throttle (safe to auto-retry) apart from genuine daily quota exhaustion
# (must fail honestly), and (3) that classification is wired into app.py's real retry/fallback/
# error-message logic, using mocked Groq errors built from the real SDK classes (not hand-rolled
# lookalikes) so the actual `except RateLimitError`/`except BadRequestError` branches are exercised.
import os
import sys
import time

os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
os.environ["JARVIS_DB_PATH"] = os.path.join(__import__("tempfile").mkdtemp(), "groqpacer_test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # the repo root

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

import users
users._init_db()
users.upsert_user("u1", "someone@example.com", "Someone")

import groq_pacer
import app as jarvis_app

# ---------------------------------------------------------------------------- groq_pacer in isolation
groq_pacer._usage.clear()
groq_pacer.record_usage(3000)
t0 = time.monotonic()
groq_pacer.wait_if_needed(1000)  # 3000+1000 well under SAFE_BUDGET (6800) -- must not sleep
check("wait_if_needed does not sleep when comfortably under budget", time.monotonic() - t0 < 0.3)

groq_pacer._usage.clear()
groq_pacer.record_usage(6500)
t0 = time.monotonic()
groq_pacer.wait_if_needed(1000)  # 6500+1000 > 6800 -- must sleep, but bounded
elapsed = time.monotonic() - t0
check("wait_if_needed sleeps when a call would push over budget", elapsed > 0.05, elapsed)
check("...but never beyond MAX_WAIT_SECONDS", elapsed <= groq_pacer.MAX_WAIT_SECONDS + 1, elapsed)

groq_pacer._usage.clear()
old_ts = time.monotonic() - 61  # older than the 60s window
groq_pacer._usage.append((old_ts, 9999))
t0 = time.monotonic()
groq_pacer.wait_if_needed(1000)
check("entries older than the 60s window are pruned and ignored", time.monotonic() - t0 < 0.3)

groq_pacer._usage.clear()
groq_pacer.record_usage(0)
check("record_usage(0) is a no-op, not a zero-token entry", len(groq_pacer._usage) == 0)


# ---------------------------------------------------------------------------- classify_rate_limit
# Real groq.RateLimitError/BadRequestError instances (not a hand-rolled lookalike) -- app.py's
# `except RateLimitError`/`except BadRequestError` only catch the real classes, and groq's own
# __init__ requires a real httpx.Response, so building one properly is the only way this test
# exercises the real except-branches instead of silently testing nothing.
import httpx
from groq import RateLimitError as RealRateLimitError, BadRequestError as RealBadRequestError

def _make_rate_limit_error(message, retry_after=5):
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(429, headers={"retry-after": str(retry_after)}, request=req)
    return RealRateLimitError(message, response=resp, body={"error": {"message": message}})

e_minute = _make_rate_limit_error(
    "Rate limit reached for model `openai/gpt-oss-20b` ... on tokens per minute (TPM): "
    "Limit 8000, Used 7800, Requested 500. Please try again in 2.4s.", retry_after=2.4,
)
kind, retry_after = groq_pacer.classify_rate_limit(e_minute)
check("classifies a real-shaped TPM message as 'minute'", kind == "minute", kind)
check("extracts the real retry-after value from the response headers", retry_after == 2.4, retry_after)

e_day = _make_rate_limit_error(
    "Rate limit reached for model `openai/gpt-oss-20b` ... on requests per day (RPD): "
    "Limit 1000, Used 1000, Requested 1. Please try again in 14h32m.", retry_after=52320,
)
kind, _ = groq_pacer.classify_rate_limit(e_day)
check("classifies a real-shaped RPD message as 'day'", kind == "day", kind)

e_unknown = _make_rate_limit_error("Something Groq has never said before")
kind, _ = groq_pacer.classify_rate_limit(e_unknown)
check("an unrecognized message falls back to 'day' (fail honestly, never silently retry a mystery limit)", kind == "day", kind)

class _BrokenError(Exception):
    pass
kind, retry_after = groq_pacer.classify_rate_limit(_BrokenError("no .response/.body at all"))
check("classify_rate_limit never raises even when the exception has none of the expected attributes", kind == "day", kind)
check("...and still returns a usable numeric retry_after fallback", isinstance(retry_after, float), retry_after)


# ---------------------------------------------------------------------------- app.py integration (mocked Groq client)
class _FakeUsage:
    def __init__(self, total=500):
        self.prompt_tokens = total - 50
        self.completion_tokens = 50
        self.total_tokens = total
        self.prompt_tokens_details = None

class _FakeMessage:
    tool_calls = None
    content = "ok"

class _FakeChoice:
    message = _FakeMessage()

class _FakeCompletion:
    def __init__(self, total=500):
        self.choices = [_FakeChoice()]
        self.usage = _FakeUsage(total)

class _ScriptedCompletions:
    """Returns/raises whatever's next in `script` each call; records call count."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
    def create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

class _FakeChat:
    def __init__(self, completions):
        self.completions = completions

class _FakeGroqClient:
    def __init__(self, script):
        self.chat = _FakeChat(_ScriptedCompletions(script))


groq_pacer._usage.clear()
real_client = jarvis_app.groq_client
real_sleep = time.sleep
real_gemini_configured = jarvis_app.GEMINI_CONFIGURED
real_gemini_api_key = jarvis_app.GEMINI_API_KEY
try:
    time.sleep = lambda s: None  # don't actually block the test suite for retry_after seconds
    # Default to "no Gemini fallback available" so Cases 1-3 test Groq-only behavior in
    # isolation, regardless of whether a real GEMINI_API_KEY happens to be in .env right
    # now -- Case 4/5 below explicitly opt back in within their own narrower scope.
    jarvis_app.GEMINI_CONFIGURED = False

    # Case 1: a per-minute throttle with a short retry-after -- must auto-retry once and succeed.
    fake = _FakeGroqClient([e_minute, _FakeCompletion()])
    jarvis_app.groq_client = fake
    result = jarvis_app._complete_with_retry([{"role": "user", "content": "hi"}])
    check("a per-minute rate limit auto-retries once and returns the retry's result", result.choices[0].message.content == "ok")
    check("...and the retry actually happened (2 real calls, not 1)", fake.chat.completions.calls == 2, fake.chat.completions.calls)

    # Case 2: a daily-quota exhaustion -- must raise immediately, no retry (no Gemini configured).
    fake2 = _FakeGroqClient([e_day])
    jarvis_app.groq_client = fake2
    raised = False
    try:
        jarvis_app._complete_with_retry([{"role": "user", "content": "hi"}])
    except jarvis_app.RateLimitError:
        raised = True
    check("a daily-quota rate limit raises immediately instead of retrying", raised)
    check("...and did NOT waste a second call attempting a retry", fake2.chat.completions.calls == 1, fake2.chat.completions.calls)

    # Case 3: existing tool_use_failed retry behavior must be completely unaffected by this change.
    req400 = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp400 = httpx.Response(400, request=req400)
    bad_request = RealBadRequestError("malformed tool call", response=resp400, body={"error": {"code": "tool_use_failed"}})
    fake3 = _FakeGroqClient([bad_request, _FakeCompletion()])
    jarvis_app.groq_client = fake3
    result3 = jarvis_app._complete_with_retry([{"role": "user", "content": "hi"}])
    check("the pre-existing tool_use_failed retry still works unchanged", result3.choices[0].message.content == "ok")

    # Case 4: Gemini fallback -- configured and Groq exhausted (daily quota) -- must succeed via Gemini.
    class _FakeHTTPResponse:
        def __init__(self, json_body):
            self._json = json_body
        def raise_for_status(self):
            pass
        def json(self):
            return self._json

    gemini_success_body = {
        "choices": [{"message": {"role": "assistant", "content": "ok via gemini", "tool_calls": None}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }
    real_requests_post = jarvis_app.requests.post
    jarvis_app.GEMINI_CONFIGURED = True
    jarvis_app.GEMINI_API_KEY = "fake-gemini-key"
    try:
        jarvis_app.requests.post = lambda *a, **k: _FakeHTTPResponse(gemini_success_body)
        fake4 = _FakeGroqClient([e_day])
        jarvis_app.groq_client = fake4
        result4 = jarvis_app._complete_with_retry([{"role": "user", "content": "hi"}])
        check("when Groq is exhausted and Gemini IS configured, falls back to Gemini instead of raising",
              result4.choices[0].message.content == "ok via gemini")
        check("the Gemini response's usage is reachable with the same attribute shape as a real groq completion",
              result4.usage.total_tokens == 120, result4.usage.total_tokens)

        # Case 5: Gemini configured but ALSO fails -- the original Groq error must still surface, not be swallowed.
        def _raising_post(*a, **k):
            raise ConnectionError("gemini unreachable too")
        jarvis_app.requests.post = _raising_post
        fake5 = _FakeGroqClient([e_day])
        jarvis_app.groq_client = fake5
        raised5 = False
        try:
            jarvis_app._complete_with_retry([{"role": "user", "content": "hi"}])
        except jarvis_app.RateLimitError:
            raised5 = True
        check("if the Gemini fallback itself fails, the original Groq rate-limit error still propagates", raised5)
    finally:
        jarvis_app.requests.post = real_requests_post
finally:
    jarvis_app.groq_client = real_client
    time.sleep = real_sleep
    jarvis_app.GEMINI_CONFIGURED = real_gemini_configured
    jarvis_app.GEMINI_API_KEY = real_gemini_api_key

# The outer run_llm RateLimitError handler and the draft-reply/compose-draft routes all branch on
# classify_rate_limit the same way -- proven once here rather than by re-running the whole LLM loop
# or Flask routes (those are exercised by the browser e2e suites; this proves the classification
# itself is correct and real, which is the part specific to today's fix).
kind_for_day_message, _ = jarvis_app.groq_pacer.classify_rate_limit(e_day)
check("the outer handlers have a real classify() result to branch on for the 'day' case", kind_for_day_message == "day")

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else f"\n      {detail}"))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
