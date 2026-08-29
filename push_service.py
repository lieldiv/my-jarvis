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
    pem = v.private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    print(base64.urlsafe_b64encode(pem).decode())
    raw = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    print(base64.urlsafe_b64encode(raw).rstrip(b'=').decode())
    "
The first line printed is VAPID_PRIVATE_KEY: the whole PEM private key,
itself wrapped in urlsafe-base64 into one line with no '+', '/', or
newlines left in it — NOT the raw PEM directly. That wrapping is
deliberate, not cosmetic: a raw multi-line PEM contains '+' characters,
and at least one real hosting dashboard's environment-variable storage
was confirmed (via a live "Could not deserialize key data... ASN.1
parsing error" failure, reproduced locally by simulating '+' -> ' '
corruption) to silently turn '+' into a space somewhere in its own
form/storage pipeline — which is a classic application/x-www-form-
urlencoded bug, not something this project's code can control. Wrapping
the whole PEM in base64 first removes every character that class of bug
touches, at the cost of needing to unwrap it below before use.
The last line printed is VAPID_PUBLIC_KEY, already urlsafe-base64 (the
format PushManager.subscribe's applicationServerKey expects directly) —
handed to the browser via /api/push/vapid-public-key, no unwrapping needed.
"""

import base64
import logging
import os

import cert_bootstrap  # noqa: F401 — see its own docstring; must stay first for any HTTPS-calling module
import users

logger = logging.getLogger("jarvis.push")


def _unwrap_private_key(raw_env_value: str) -> str:
    """VAPID_PRIVATE_KEY is stored as urlsafe-base64(PEM), not raw PEM — see
    this module's docstring for why. Falls back to treating the value as a
    raw PEM directly if it doesn't decode/verify cleanly, so a manually-set
    raw PEM (e.g. in a .env file, which isn't subject to the same
    corruption a web dashboard's form submission is) still works rather
    than breaking.

    The try/except alone (an earlier version of this function) already
    correctly falls back for a plain raw PEM specifically — verified
    directly: base64.urlsafe_b64decode() on real PEM text does produce
    non-UTF-8 garbage bytes, but the chained .decode() step then raises
    UnicodeDecodeError, which the except already catches. The explicit
    "-----BEGIN" check below is an extra guard for the narrower case where
    decoded garbage bytes happen to ALSO be valid UTF-8 (not every random
    byte sequence trips UnicodeDecodeError) — something try/except alone
    can't distinguish from a real successfully-unwrapped key, only content
    inspection can."""
    try:
        decoded = base64.urlsafe_b64decode(raw_env_value.encode()).decode()
    except Exception:
        return raw_env_value
    if decoded.lstrip().startswith("-----BEGIN"):
        return decoded
    return raw_env_value


_raw_private_key_env = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PRIVATE_KEY = _unwrap_private_key(_raw_private_key_env)
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
# mailto: contact required by the Web Push spec so a push provider that
# sees abuse from this server's VAPID identity has somewhere to complain to.
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")

CONFIGURED = bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)

# Diagnostic only, deliberately never prints the actual secret value —
# only its shape. Exists because "Could not deserialize key data" kept
# recurring even after the value was reportedly replaced in the Render
# dashboard, and neither a screenshot (font ambiguity: 0/O/8, l/I/1, m/w)
# nor asking the user to describe it could settle whether the RUNNING
# process actually received the new value or something is still stale/
# different from what the dashboard shows was saved.
if _raw_private_key_env:
    logger.info(
        f"push_service: VAPID_PRIVATE_KEY env var is {len(_raw_private_key_env)} chars, "
        f"starts with {_raw_private_key_env[:12]!r}, ends with {_raw_private_key_env[-12:]!r}, "
        f"unwrap {'succeeded (looks like a PEM)' if VAPID_PRIVATE_KEY.startswith('-----BEGIN') else 'did NOT produce a PEM'}."
    )
else:
    logger.info("push_service: VAPID_PRIVATE_KEY env var is empty or unset.")

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
                # pywebpush defaults ttl to 0 when omitted, which per RFC
                # 8030 means "deliver this instant or discard it" — the
                # push service will NOT hold it for a device that isn't
                # reachable right now (asleep, app fully closed, briefly
                # offline). It still returns success to US either way, so
                # this was a genuinely silent drop: no exception, no log
                # line, nothing — a reminder fired while the phone happened
                # to be locked just vanished between Apple's relay and the
                # device. One hour gives real slack for that without
                # reminders showing up so late they're useless.
                ttl=3600,
            )
            logger.info(f"Push sent for user {user_id} (accepted by provider).")
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
