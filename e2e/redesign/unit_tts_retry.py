# Reported live, mid-demo: edge-tts (Microsoft's reverse-engineered Read-Aloud backend) hit an
# intermittent 403 on its trusted-client-token handshake, twice in one day -- not constant, most
# calls already succeed. generate_tts_base64() now retries the exact same request once (a fresh
# edge_tts.Communicate() instance gets a fresh token attempt) before falling back to the plain
# default voice, which itself also gets one retry. This proves the retry/fallback state machine
# actually behaves as designed -- happy path untouched, transient failure recovered without
# falling back unnecessarily, sustained failure still degrades to None exactly as before.
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # the repo root

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

import tts


def run(coro):
    return asyncio.run(coro)


def with_synthesize(script):
    """Replaces tts._synthesize with one that pops from `script` each call:
    an Exception entry raises, anything else is returned as the "audio bytes".
    Returns (fake_fn, call_log) so call order/args can be asserted on too."""
    call_log = []
    async def fake(text, voice, prosody):
        call_log.append((voice, tuple(sorted(prosody.items()))))
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    return fake, call_log


real_synthesize = tts._synthesize
try:
    # Case 1: first attempt succeeds outright -- no retry, no fallback, exactly 1 call.
    fake, log = with_synthesize([b"real-audio"])
    tts._synthesize = fake
    result = run(tts.generate_tts_base64("hello", "en-GB-RyanNeural", {}))
    check("a clean first-attempt success returns valid audio", result is not None and len(result) > 0)
    check("...with exactly one synthesis call (no wasted retry on the happy path)", len(log) == 1, log)

    # Case 2: first attempt fails, retry (same voice) succeeds -- must NOT fall back to the default voice.
    fake, log = with_synthesize([RuntimeError("403 Invalid response status"), b"recovered-audio"])
    tts._synthesize = fake
    result = run(tts.generate_tts_base64("hello", "en-GB-RyanNeural", {"pitch": "-20Hz"}))
    check("a transient failure recovers on retry and still returns audio", result is not None)
    check("...using exactly 2 calls, both the ORIGINAL voice+prosody (no unnecessary fallback)",
          log == [("en-GB-RyanNeural", (("pitch", "-20Hz"),))] * 2, log)

    # Case 3: original voice fails twice (initial + retry), falls back to the plain default voice,
    # which succeeds on ITS first try.
    fake, log = with_synthesize([RuntimeError("fail"), RuntimeError("fail"), b"fallback-audio"])
    tts._synthesize = fake
    result = run(tts.generate_tts_base64("hello", tts.ULTRON_VOICE, tts.ULTRON_PROSODY))
    check("original voice failing twice falls back to the plain default voice successfully", result is not None)
    check("...fallback call uses the plain VOICE with empty prosody",
          log[2] == (tts.VOICE, ()), log)

    # Case 4: original voice fails twice, fallback voice ALSO fails twice -- total failure, None.
    fake, log = with_synthesize([RuntimeError("f1"), RuntimeError("f2"), RuntimeError("f3"), RuntimeError("f4")])
    tts._synthesize = fake
    result = run(tts.generate_tts_base64("hello", tts.ULTRON_VOICE, tts.ULTRON_PROSODY))
    check("sustained failure on both the original AND fallback voice returns None (not a crash)", result is None)
    check("...having genuinely tried 4 times (2 original + 2 fallback), not given up early", len(log) == 4, log)

    # Case 5: already the plain default voice (no prosody) -- no fallback target exists, so after
    # the one retry also fails, must give up immediately rather than "falling back" to itself.
    fake, log = with_synthesize([RuntimeError("f1"), RuntimeError("f2")])
    tts._synthesize = fake
    result = run(tts.generate_tts_base64("hello", tts.VOICE, {}))
    check("the default voice failing twice gives up cleanly (nothing left to fall back to)", result is None)
    check("...without a pointless 3rd/4th call to itself", len(log) == 2, log)
finally:
    tts._synthesize = real_synthesize

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else f"\n      {detail}"))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
