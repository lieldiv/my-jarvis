"""
whatsapp_service.py — reminder delivery over WhatsApp via CallMeBot's free,
personal-use API (https://www.callmebot.com/blog/free-api-whatsapp-messages/).

This is NOT Meta's official WhatsApp Business Platform — that requires
business verification and pre-approved message templates, far too much
friction for "send myself a reminder." CallMeBot is an unofficial personal
bridge instead: a one-time setup (add CallMeBot's number as a contact, send
it a fixed activation text) gets a per-phone-number apikey back over
WhatsApp itself, and after that any server can deliver a message with a
single HTTP GET carrying phone+text+apikey.

Unlike push_service.py's VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY (one global
server identity, set once as an env var), there are no credentials here at
the module level — phone+apikey are per-user, entered once in Settings and
stored on the users row (see users.py's whatsapp_phone/whatsapp_apikey
columns), exactly like a Google token rather than a global secret.
"""

import logging

import cert_bootstrap  # noqa: F401 — see its own docstring; must stay first for any HTTPS-calling module
import requests

logger = logging.getLogger("jarvis.whatsapp")

API_URL = "https://api.callmebot.com/whatsapp.php"


def send_whatsapp(phone: str, apikey: str, text: str) -> tuple[bool, str]:
    """Returns (ok, message) rather than push_service.send_push's silent
    best-effort pattern — this is called both from the background scheduler
    (where a log line is enough) and directly from a user-facing "save +
    send test message" button in Settings, where the person needs an actual
    reason on failure instead of a notification that just never shows up."""
    if not (phone and apikey):
        return False, "לא הוגדר מספר טלפון או מפתח API."
    try:
        resp = requests.get(
            API_URL,
            params={"phone": phone, "text": text, "apikey": apikey},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"WhatsApp send failed (transport error): {e}")
        return False, "שגיאת רשת בשליחה לוואטסאפ."

    # CallMeBot reports most failures (bad apikey, phone never activated the
    # bot, etc.) as HTTP 200 with an error message in the body, not a non-200
    # status — so status alone can't be trusted as the success signal.
    body = resp.text or ""
    if resp.status_code != 200 or "error" in body.lower():
        logger.error(f"WhatsApp send rejected ({resp.status_code}): {body[:200]}")
        return False, "CallMeBot דחה את השליחה — ודא שהמספר והמפתח נכונים ושהפעלת את הבוט בוואטסאפ."

    logger.info(f"WhatsApp message accepted by CallMeBot for {phone[-4:]}.")
    return True, "נשלח בהצלחה."
