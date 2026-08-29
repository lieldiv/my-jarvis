"""
push_service.py — real browser push notifications (Web Push), so reminders
can reach the user without relying on them noticing an email.

Uses VAPID (Voluntary Application Server Identification) — a keypair that
identifies this server to push providers (Chrome's FCM endpoint, etc.)
without needing a Google/Apple developer account. CONFIGURED is False (and
sends are silently skipped) until both env vars are set, same degrade-
gracefully pattern as google_service.py/microsoft_service.py's CONFIGURED
flags — a reminder should still get emailed even if push was never set up.

Generating a keypair (one-time, do this once and keep the values):
    python -c "
    from py_vapid import Vapid
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
    import base64
    v = Vapid(); v.generate_keys()
    print(v.private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode())
    raw = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    print(base64.urlsafe_b64encode(raw).rstrip(b'=').decode())
    "
The first line printed is VAPID_PRIVATE_KEY (the whole PEM block, including
the BEGIN/END lines — set it as one env var with real newlines, not escaped
\\n). The last line is VAPID_PUBLIC_KEY, handed to the browser via
/api/push/vapid-public-key so it can call PushManager.subscribe().
"""

import logging
import os

import cert_bootstrap  # noqa: F401 — see its own docstring; must stay first for any HTTPS-calling module
import users

logger = logging.getLogger("jarvis.push")

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
# mailto: contact required by the Web Push spec so a push provider that
# sees abuse from this server's VAPID identity has somewhere to complain to.
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")

CONFIGURED = bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)

if CONFIGURED:
    from pywebpush import webpush, WebPushException
else:
    logger.info("push_service: VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY not set — push notifications disabled, reminders still email.")


def send_push(user_id: str, title: str, body: str) -> None:
    """Best-effort — never raises. Called from daily_briefing.py alongside
    (not instead of) the existing email delivery, so a push failure here
    should never be the reason a reminder doesn't reach the user at all."""
    if not CONFIGURED:
        return

    import json
    payload = json.dumps({"title": title, "body": body})

    for sub in users.get_push_subscriptions(user_id):
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                # Provider is telling us this subscription is dead (browser
                # profile removed, site data cleared, etc.) — not a
                # transient failure, so clean it up instead of retrying it
                # forever on every future reminder.
                users.delete_push_subscription(sub["endpoint"])
            else:
                logger.error(f"Push failed for user {user_id} ({status}): {e}")
        except Exception as e:
            logger.error(f"Push failed for user {user_id} (transport error): {e}")
