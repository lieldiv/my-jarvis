# Follow-up to the false-success bug (see unit_confirm_honesty.py): once a calendar/email failure is
# shown honestly instead of as a fake "success", the next question is WHY it failed -- and every
# HttpError branch in google_service.py used to collapse every possible reason (wrong/stale event id,
# not the organizer, the event already gone, a malformed request, ...) into the identical "refused the
# request" sentence. Reported live: a user's calendar event kept failing to cancel with no way for
# either of us to tell which of those it actually was. This proves google_service._http_status() pulls
# the real HTTP status (+ reason) out of a realistic HttpError, and that it actually reaches the
# generic failure sentences end-to-end -- not just that the helper function works in isolation.
import json
import os
import sys

os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # the repo root

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

import google_service as gs
from googleapiclient.errors import HttpError


def fake_http_error(status: int, message: str) -> HttpError:
    """Same shape googleapiclient itself builds: resp.status + a JSON body
    under "error"/"message" that HttpError._get_reason() parses into .reason
    -- not a hand-rolled string, so this proves the real parsing path, the
    one an actual Google API failure will go through."""
    class _Resp:
        def __init__(self, status):
            self.status = status
            self.reason = ""
    content = json.dumps({"error": {"message": message}}).encode("utf-8")
    return HttpError(_Resp(status), content, uri="https://www.googleapis.com/calendar/v3/x")


# ---------------------------------------------------------------------------- the helper in isolation
e404 = fake_http_error(404, "Not Found")
check("_http_status formats a 404 with its real reason", gs._http_status(e404) == "404: Not Found", gs._http_status(e404))

e403 = fake_http_error(403, "Forbidden")
check("_http_status formats a 403 with its real reason", gs._http_status(e403) == "403: Forbidden", gs._http_status(e403))

class _NotAnHttpError:
    pass
check("_http_status never raises on something that isn't a real HttpError (defensive fallback)",
      gs._http_status(_NotAnHttpError()) == "?", gs._http_status(_NotAnHttpError()))


# ---------------------------------------------------------------------------- end-to-end: the real status reaches the user
class _FakeEvents:
    def __init__(self, error):
        self._error = error
    def delete(self, calendarId, eventId):
        return self
    def insert(self, calendarId, body):
        return self
    def patch(self, calendarId, eventId, body):
        return self
    def execute(self):
        raise self._error

class _FakeCalendarService:
    def __init__(self, error):
        self._events = _FakeEvents(error)
    def events(self):
        return self._events

gs._calendar_service = lambda user_id: _FakeCalendarService(fake_http_error(404, "Not Found"))
msg = gs.delete_calendar_event("u1", "stale-or-wrong-id")
check("delete_calendar_event surfaces the real 404 instead of a one-size-fits-all sentence",
      "404" in msg and "Not Found" in msg, msg)

gs._calendar_service = lambda user_id: _FakeCalendarService(fake_http_error(403, "Forbidden"))
msg = gs.delete_calendar_event("u1", "not-mine")
check("delete_calendar_event surfaces a 403 distinctly from a 404 (different root cause, different number)",
      "403" in msg and "Forbidden" in msg, msg)

gs._calendar_service = lambda user_id: _FakeCalendarService(fake_http_error(400, "Bad Request"))
msg = gs.create_calendar_event("u1", "Meeting", "2026-10-01T10:00:00+03:00", "2026-10-01T11:00:00+03:00")
check("create_calendar_event surfaces its real status too, not just delete",
      "400" in msg and "Bad Request" in msg, msg)

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + str(detail)[:300]))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
