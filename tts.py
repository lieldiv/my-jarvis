"""
tts.py — shared edge-tts helper.

Pulled out of app.py so the daily-briefing scheduler (daily_briefing.py) can
generate spoken audio the same way the live command flow does, without
importing app.py itself (which would be circular: app.py starts the
scheduler thread).
"""

import base64
import logging

import edge_tts

logger = logging.getLogger("jarvis.tts")

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


async def generate_tts_base64(text: str, voice: str = VOICE, prosody=None):
    """Falls back to the plain default voice if anything non-default fails.

    Microsoft renames and retires edge-tts voices without warning, and the
    prosody arguments are validated strictly enough that a future version
    tightening the format would reject them outright. The previous single
    try/except turned either into `return None` — i.e. the assistant silently
    went mute, with nothing on screen explaining why. Acceptable for a total
    TTS outage; not acceptable for "the persona's voice settings went stale",
    where the plain default is right there and works.
    """
    prosody = prosody or {}
    try:
        audio_bytes = await _synthesize(text, voice, prosody)
    except Exception as e:
        logger.error(f"TTS error (voice={voice!r}, prosody={prosody!r}): {e}")
        if voice == VOICE and not prosody:
            return None
        logger.info(f"TTS falling back to the plain default voice '{VOICE}'.")
        try:
            audio_bytes = await _synthesize(text, VOICE, {})
        except Exception as e2:
            logger.error(f"TTS fallback also failed: {e2}")
            return None
    return base64.b64encode(audio_bytes).decode("utf-8")
