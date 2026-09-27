import os, sys
from datetime import datetime, timezone
os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))   # the repo root
import productivity_service as ps

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

# --- reproduce the production bug: a UTC server treats a naive date as UTC midnight
naive_as_utc = datetime(2026, 9, 27).replace(tzinfo=timezone.utc).astimezone(ps.LOCAL_TZ)
check("the OLD behaviour on a UTC server really produced 'Sun 03:00'", naive_as_utc.strftime("%a %H:%M") == "Sun 03:00", naive_as_utc.strftime("%a %H:%M"))

dt, all_day = ps._parse_event_when("2026-09-27")
check("all-day date parses as midnight LOCAL time, flagged all_day", all_day and (dt.hour, dt.minute) == (0, 0) and dt.tzinfo is not None and dt.strftime("%Y-%m-%d") == "2026-09-27", str(dt))
check("_format_time for an all-day event says so instead of a time", ps._format_time("2026-09-27") == "Sun (all day)", ps._format_time("2026-09-27"))
check("_format_time for a timed event is unchanged", ps._format_time("2026-09-27T10:30:00+03:00") == "Sun 10:30", ps._format_time("2026-09-27T10:30:00+03:00"))
check("_format_time converts UTC 'Z' to local", ps._format_time("2026-09-27T07:30:00Z") == "Sun 10:30", ps._format_time("2026-09-27T07:30:00Z"))
check("_format_time empty -> '?'", ps._format_time("") == "?")
check("_format_time garbage returns the raw string", ps._format_time("not-a-date") == "not-a-date")
check("date-shaped garbage does not crash", ps._parse_event_when("2026-99-99") == (None, False))

sample = [
    {"id": "a", "summary": "נופש", "start": "2026-09-27", "end": "2026-09-30", "location": "", "source": "Google"},          # 3 days, end exclusive
    {"id": "b", "summary": "יום הולדת", "start": "2026-09-28", "end": "2026-09-29", "location": "", "source": "Google"},       # 1 day
    {"id": "c", "summary": "פגישה עם נימרוד", "start": "2026-09-27T10:30:00+03:00", "end": "2026-09-27T11:30:00+03:00", "location": "קפה גרג", "source": "Google"},
    {"id": "d", "summary": "utc timed", "start": "2026-09-27T21:30:00Z", "end": "2026-09-27T22:30:00Z", "source": "Google"},   # 00:30 next day local
    {"id": "e", "start": "2026-09-29T09:00:00+03:00", "end": "2026-09-29T09:30:00+03:00", "source": "Google"},                   # no title
]
ps._collect_events = lambda user_id, days_ahead, max_results=15: list(sample)
out = ps.get_upcoming_events_structured("u1", max_results=30)
by = {e["id"]: e for e in out}
check("all events come back", len(out) == 5)
check("multi-day all-day: day/end_day are inclusive local dates", (by["a"]["all_day"], by["a"]["day"], by["a"]["end_day"]) == (True, "2026-09-27", "2026-09-29"), str(by["a"]))
check("single-day all-day: end_day == day", (by["b"]["day"], by["b"]["end_day"]) == ("2026-09-28", "2026-09-28"), str(by["b"]))
check("all-day events have no start_iso/end_iso", by["a"]["start_iso"] == "" and by["a"]["end_iso"] == "")
check("timed event keeps local iso with offset", by["c"]["start_iso"].startswith("2026-09-27T10:30:00") and by["c"]["end_iso"].startswith("2026-09-27T11:30:00") and not by["c"]["all_day"], str(by["c"]))
check("location is carried", by["c"]["location"] == "קפה גרג")
check("UTC event late evening lands on the NEXT local day", by["d"]["day"] == "2026-09-28" and by["d"]["start_iso"][11:16] == "00:30", str(by["d"]))
check("missing title gets the placeholder", by["e"]["summary"] == "(no title)")
check("time_label stays populated for older callers", by["a"]["time_label"] == "Sun (all day)" and by["c"]["time_label"] == "Sun 10:30", (by["a"]["time_label"], by["c"]["time_label"]))

ps._collect_events = lambda *a, **k: None
check("not configured -> None (not an empty list)", ps.get_upcoming_events_structured("u1") is None)

# --- the text the model/emails see also stops lying about all-day events
ps._collect_events = lambda user_id, days_ahead, max_results=15: list(sample)[:2]
text = ps.get_calendar_events_text("u1", 7)
check("calendar text for the model says 'all day'", "Sun (all day): נופש" in text and "03:00" not in text, text)

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else f"\n      {detail}"))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
