"""
tts.py — shared edge-tts helper.

Pulled out of app.py so the daily-briefing scheduler (daily_briefing.py) can
generate spoken audio the same way the live command flow does, without
importing app.py itself (which would be circular: app.py starts the
scheduler thread).
"""

import asyncio
import base64
import logging

import cert_bootstrap  # noqa: F401 — must run before edge_tts.communicate's first import
# anywhere in the process: aiohttp's SSL context is a module-level constant evaluated
# once, at that first import, so cert_bootstrap's certifi monkeypatch only takes effect
# if it's already applied by then. Every other HTTPS-calling module in this repo
# self-imports it for the same reason (google_service.py, microsoft_service.py,
# push_service.py, stocks_service.py, tavily_service.py) -- this one relied on app.py
# happening to import cert_bootstrap first, which holds today by accident, not by
# design. Found by a review agent; fixed before it could ever actually matter.
import edge_tts
import edge_tts.communicate as _edge_tts_communicate

logger = logging.getLogger("jarvis.tts")

# Microsoft is migrating the Edge Read-Aloud backend off speech.platform.bing.com
# to api.msedgeservices.com, rolling it out region by region -- confirmed live,
# not guessed: reproduced the 403 directly against the old host, then confirmed
# this exact patch gets real audio back (github.com/rany2/edge-tts#490, 40+
# independent reports across multiple countries as of Oct 2026, same token still
# accepted on the new host, no other change needed; unmerged fix: PR #491).
#
# MUST patch communicate.WSS_URL specifically, not constants.WSS_URL: edge_tts's
# own communicate.py does `from .constants import WSS_URL`, which binds its own
# local name at import time -- patching the constants module after that point
# silently does nothing, because communicate.py never looks at it again. Checked
# directly against the installed 7.2.8 package source before relying on this.
_edge_tts_communicate.WSS_URL = (
    "wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1"
    f"?Ocp-Apim-Subscription-Key={edge_tts.constants.TRUSTED_CLIENT_TOKEN}"
)

VOICE = "en-GB-RyanNeural"  # run `edge-tts --list-voices` to see others

# Ultron speaks with the SAME larynx, deliberately. It was en-US-GuyNeural,
# which came out flat and robotic rather than menacing — what was wanted is
# J.A.R.V.I.S. with an edge, and an edge is prosody, not a different voice.
# Dropping the pitch and easing the pace keeps Ryan's British delivery and
# just makes it heavier and more deliberate.
#
# Verified against the pinned edge-tts 7.2.8: Communicate takes rate/volume/
# pitch and validates them strictly — "-20Hz" passes, "-20hz" and "-20" both
# raise ValueError. Dial to taste: -12Hz is subtle, -35Hz is pantomime.
ULTRON_VOICE = VOICE
ULTRON_PROSODY = {"rate": "-7%", "pitch": "-20Hz"}


async def _synthesize(text: str, voice: str, prosody: dict) -> bytes:
    communicate = edge_tts.Communicate(text, voice, **prosody)
    audio_bytes = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_bytes += chunk["data"]
    return audio_bytes


async def _try_synthesize(text: str, voice: str, prosody: dict):
    """bytes on success, None on failure (logged) -- never raises."""
    try:
        return await _synthesize(text, voice, prosody)
    except Exception as e:
        logger.error(f"TTS error (voice={voice!r}, prosody={prosody!r}): {e}")
        return None


async def generate_tts_base64(text: str, voice: str = VOICE, prosody=None):
    """Retries once, then falls back to the plain default voice if that still fails.

    Microsoft renames and retires edge-tts voices without warning, and the
    prosody arguments are validated strictly enough that a future version
    tightening the format would reject them outright. The previous single
    try/except turned either into `return None` — i.e. the assistant silently
    went mute, with nothing on screen explaining why. Acceptable for a total
    TTS outage; not acceptable for "the persona's voice settings went stale",
    where the plain default is right there and works.

    The retry is new: observed live (403 "Invalid response status" on
    Microsoft's trusted-client-token handshake, twice in one day, not
    constant) that this specific failure is usually a one-off hiccup on that
    one connection attempt rather than a sustained outage -- a fresh
    edge_tts.Communicate() instance on retry gets a fresh token attempt, and
    most calls already succeed outright, so this only adds latency on the
    minority that would otherwise have gone silent.
    """
    prosody = prosody or {}

    audio_bytes = await _try_synthesize(text, voice, prosody)
    if audio_bytes is None:
        await asyncio.sleep(0.5)
        logger.info("TTS retrying the same request once...")
        audio_bytes = await _try_synthesize(text, voice, prosody)

    if audio_bytes is None:
        if voice == VOICE and not prosody:
            return None
        logger.info(f"TTS falling back to the plain default voice '{VOICE}'.")
        audio_bytes = await _try_synthesize(text, VOICE, {})
        if audio_bytes is None:
            await asyncio.sleep(0.5)
            logger.info("TTS retrying the fallback voice once...")
            audio_bytes = await _try_synthesize(text, VOICE, {})

    if audio_bytes is None:
        return None
    return base64.b64encode(audio_bytes).decode("utf-8")
