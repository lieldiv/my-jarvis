"""groq_pacer.py — shared rate-limit pacing for every Groq call this process
makes against openai/gpt-oss-20b.

run_llm's main tool-calling loop, /api/inbox/draft-reply, and /api/compose/draft
all draw from the SAME account-level Groq free-tier budget (8,000 tokens/minute,
confirmed from Groq's own docs) — and this app is MULTI-USER (deployed on Render,
every signed-in user shares the one Groq account/key), so that 8,000/minute isn't
even one person's budget, it's split across however many users are active right
now. A tracker scoped to just one call site would undercount real usage.

Investigated after a real client reported "Groq runs out of usage fast, even with
light use, feels random." Root cause measured directly: the system prompt + time/
shortcuts context + ACTIVE_TOOLS schema alone cost ~6,275 tokens on EVERY round of
EVERY request (run_llm resends the full messages+tools payload unchanged each of
up to MAX_TOOL_ROUNDS=3 rounds) — close to or exceeding the entire shared 8,000 TPM
budget from a single round of a single user's single turn, before counting history,
the user's own message, or completion tokens. "Random" because it depends on
whether that particular request needed 1 round or 3, not on how much any user is
actually using the assistant.
"""
import logging
import threading
import time
from collections import deque

logger = logging.getLogger("jarvis.groq_pacer")

WINDOW_SECONDS = 60
SAFE_BUDGET = 6800    # stays under Groq's 8,000 TPM free-tier cap with headroom --
                      # needed because gpt-oss's hidden reasoning-channel tokens make
                      # a call's real cost hard to predict before it's actually made
MAX_WAIT_SECONDS = 15  # never freeze a live request for longer than this

_lock = threading.Lock()
_usage = deque()  # (monotonic_timestamp, tokens) pairs, oldest first


def record_usage(total_tokens: int) -> None:
    # A negative value (a caller mis-computing a "cached tokens" discount, say) must never
    # reach the deque: it would silently SUBTRACT from the tracked sliding-window sum,
    # under-counting real usage -- the dangerous direction, since this tracker existing at
    # all is to avoid under-estimating how close the shared budget is to its real ceiling.
    if not total_tokens or total_tokens < 0:
        return
    with _lock:
        _usage.append((time.monotonic(), total_tokens))


def _prune_locked(now: float) -> int:
    while _usage and now - _usage[0][0] > WINDOW_SECONDS:
        _usage.popleft()
    return sum(t for _, t in _usage)


def wait_if_needed(estimated_tokens: int) -> None:
    """Sleeps (capped at MAX_WAIT_SECONDS) if firing a call of roughly
    `estimated_tokens` would likely push the trailing 60s over the free-tier
    budget — stays short of the budget proactively instead of reacting to a
    real 429 after the fact. Shared across every user hitting this process's
    one Groq account, not per-user."""
    with _lock:
        now = time.monotonic()
        used = _prune_locked(now)
        if not _usage or used + estimated_tokens <= SAFE_BUDGET:
            return
        oldest_ts = _usage[0][0]
        wait = min(WINDOW_SECONDS - (now - oldest_ts) + 0.5, MAX_WAIT_SECONDS)
    if wait > 0:
        logger.info(f"Groq pacer: pausing {wait:.1f}s to stay under the free-tier rate limit")
        time.sleep(wait)


def classify_rate_limit(e) -> tuple:
    """(kind, retry_after_seconds) from a groq.RateLimitError. kind is
    "minute" (TPM/RPM — resets in seconds, safe to auto-retry once) or "day"
    (RPD/TPD — resets in hours, must fail honestly instead of retrying).
    Groq's 429 body names the limit type in its message text (confirmed
    against the real groq SDK's exception fields: .response.headers,
    .body/.message — not guessed). Falls back to "day" — the safer, more
    honest failure — if the text can't be read at all, since an
    unrecognized limit type should never be silently retried."""
    retry_after = 5.0
    try:
        retry_after = float(e.response.headers.get("retry-after", retry_after))
    except Exception:
        pass
    text = ""
    try:
        text = str(getattr(e, "body", None) or getattr(e, "message", None) or "").lower()
    except Exception:
        pass
    if "per day" in text or "rpd" in text or "tpd" in text:
        return "day", retry_after
    if "per minute" in text or "rpm" in text or "tpm" in text:
        return "minute", retry_after
    return "day", retry_after
