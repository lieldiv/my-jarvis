# -*- coding: utf-8 -*-
"""Runs JARVIS locally for the browser tests, already signed in.

Writes session.txt (a validly signed cookie for a test user) and mic.wav (a
tone for Chromium's fake microphone) next to itself, then serves until killed.
"""
import math
import os
import struct
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
# Optional escape hatch: point at a directory of installed dependencies when
# they aren't already importable (JARVIS_E2E_PKGS=/path/to/site-packages).
EXTRA_PKGS = os.environ.get("JARVIS_E2E_PKGS", "")
PORT = int(os.environ.get("E2E_PORT", "5099"))
SECRET = "e2e-fixed-secret-so-the-cookie-verifies"

os.environ.update(
    JARVIS_DB_PATH=os.path.join(HERE, "_e2e.db"),
    JARVIS_DESKTOP_TOOLS="false",
    JARVIS_WEEKLY_SUMMARY="false",
    GROQ_API_KEY="e2e-not-called",     # every Groq call is intercepted in the browser
    FLASK_SECRET_KEY=SECRET,
)
if EXTRA_PKGS:
    sys.path.insert(0, EXTRA_PKGS)
sys.path.insert(0, os.path.dirname(HERE))   # the repo, wherever it happens to live

import app as jarvis          # noqa: E402
import users                  # noqa: E402
from flask.sessions import SecureCookieSessionInterface  # noqa: E402

USER = "e2e-user"
users.upsert_user(USER, "e2e@example.com", "E2E")

# A real signed cookie, so the browser is past the Google gate without needing
# Google. Same mechanism the app uses; only the secret is fixed for the test.
serializer = SecureCookieSessionInterface().get_signing_serializer(jarvis.app)
cookie = serializer.dumps({"user_id": USER, "user_email": "e2e@example.com"})
with open(os.path.join(HERE, "session.txt"), "w") as fh:
    fh.write(cookie)

# A one-second 440 Hz tone. Silence would make MediaRecorder emit almost
# nothing, and "did it record?" is one of the things under test.
wav_path = os.path.join(HERE, "mic.wav")
if not os.path.exists(wav_path):
    rate, secs = 48000, 1.0
    with wave.open(wav_path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * (i / rate))))
            for i in range(int(rate * secs))))

print(f"E2E_READY http://127.0.0.1:{PORT}", flush=True)
jarvis.app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)
