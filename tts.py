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
ULTRON_VOICE = "en-US-GuyNeural"  # flatter and colder than Ryan; see the fallback below


async def _synthesize(text: str, voice: str) -> bytes:
    communicate = edge_tts.Communicate(text, voice)
    audio_bytes = b""
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_bytes += chunk["data"]
    return audio_bytes


async def generate_tts_base64(text: str, voice: str = VOICE):
    """Falls back to VOICE if a non-default voice fails.

    Microsoft renames and retires edge-tts voices without warning, and the
    previous single try/except turned any such failure into `return None` —
    i.e. the assistant silently went mute, with nothing on screen explaining
    why. That is an acceptable outcome for a total TTS outage, but not for
    "the alternate persona's voice no longer exists": in that case the
    default voice is right there and works.
    """
    try:
        audio_bytes = await _synthesize(text, voice)
    except Exception as e:
        logger.error(f"TTS error with voice '{voice}': {e}")
        if voice == VOICE:
            return None
        logger.info(f"TTS falling back to the default voice '{VOICE}'.")
        try:
            audio_bytes = await _synthesize(text, VOICE)
        except Exception as e2:
            logger.error(f"TTS fallback also failed: {e2}")
            return None
    return base64.b64encode(audio_bytes).decode("utf-8")
