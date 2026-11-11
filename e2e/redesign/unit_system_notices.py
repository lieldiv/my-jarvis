# Proves the generic, send-free "system notices" layer (users.py's
# system_notices table) and the three zero-LLM, deterministic detectors
# that write to it (daily_briefing.py's holiday greeting, meeting-conflict,
# and market-swing checks) -- none of these ever send anything anywhere;
# each is just "notice appears in the Suggestions tab, user dismisses it."
# Also proves /api/suggestions (combining email suggestions + notices) and
# that /api/inbox (the Mail tab) was left untouched by this whole feature.
import os, sqlite3, sys, time, tempfile
os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
os.environ["JARVIS_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "notices_test.db")  # isolated, throwaway
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # the repo root

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

import users
users._init_db()
UID = "notice-user-1"
users.upsert_user(UID, "person@example.com", "Person")
users.save_google_token(UID, '{"fake": "token"}')  # list_connected_user_ids requires a connected token

# ---------------------------------------------------------------------------- users.py: system_notices CRUD + dedup
check("no active notices yet", users.list_active_notices(UID) == [])

# Dedupe_keys here are deliberately distinct from anything the real
# detectors below would ever generate (prefixed "test-generic-") -- using
# a real-looking key like "holiday:פסח:2027" here would collide with the
# holiday detector's own dedupe_key for the same test data later in this
# file, silently swallowing that notice as "already exists" even after
# being dismissed (dedup checks ANY row with that key, dismissed or not)
# and making a real bug look identical to this kind of test self-collision.
nid = users.add_system_notice(UID, "holiday", "פסח מתקרב", "חג פסח שמח!", "test-generic-dedupe-key-1")
check("a new notice gets a real row id", nid is not None, nid)
dup = users.add_system_notice(UID, "holiday", "פסח מתקרב", "חג פסח שמח!", "test-generic-dedupe-key-1")
check("dedup: same dedupe_key for the same user returns None, no second row", dup is None, dup)

active = users.list_active_notices(UID)
check("list_active_notices shows exactly the one notice", len(active) == 1 and active[0]["id"] == nid, active)
check("notice carries its category/title/body", active[0]["category"] == "holiday" and active[0]["title"] == "פסח מתקרב", active[0])

nid_with_action = users.add_system_notice(
    UID, "conflict", "שתי פגישות חופפות", "\"א\" ו-\"ב\" חופפות.", "test-generic-dedupe-key-2", action_label="פתח יומן")
check("action_label round-trips when present",
      any(n["id"] == nid_with_action and n["action_label"] == "פתח יומן" for n in users.list_active_notices(UID)))

ok = users.dismiss_notice(nid, UID)
check("dismissing an active notice succeeds", ok is True)
check("...and it drops out of the active list", all(n["id"] != nid for n in users.list_active_notices(UID)))
ok_again = users.dismiss_notice(nid, UID)
check("dismissing an already-dismissed notice is a safe no-op", ok_again is False)

other_uid = "other-notice-user"
users.upsert_user(other_uid, "other@example.com", "Other")
check("ownership scoping: a different user can't dismiss someone else's notice",
      users.dismiss_notice(nid_with_action, other_uid) is False)
check("...and the original owner's notice is still active", any(n["id"] == nid_with_action for n in users.list_active_notices(UID)))

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)  # clean slate before the detector tests

# ---------------------------------------------------------------------------- daily_briefing.py: holiday greeting (self-facing only)
import daily_briefing as db

def _fake_holiday_events(user_id, time_min, time_max, max_results=5, calendar_id=None):
    check("holiday check reads from Google's public Jewish-holidays calendar, not the user's own",
          calendar_id == db.HOLIDAY_CALENDAR_ID, calendar_id)
    return [{"id": "h1", "summary": "Passover", "start": "2027-04-21", "end": "2027-04-22", "location": "", "source": "Google"}]
db.google_service.list_calendar_events = _fake_holiday_events

db._check_holiday_greeting()
holiday_notices = [n for n in users.list_active_notices(UID) if n["category"] == "holiday"]
check("a recognized holiday creates exactly one notice", len(holiday_notices) == 1, holiday_notices)
check("the notice title is translated to Hebrew (not left as the raw English calendar title)",
      holiday_notices and holiday_notices[0]["title"] == "פסח מתקרב", holiday_notices)
check("the notice body never names a recipient or mentions sending -- self-facing only",
      holiday_notices and "שלח" not in holiday_notices[0]["body"] and "@" not in holiday_notices[0]["body"],
      holiday_notices)

db._check_holiday_greeting()
check("re-scanning the SAME upcoming holiday does not duplicate the notice (dedup by name+year)",
      len([n for n in users.list_active_notices(UID) if n["category"] == "holiday"]) == 1)

def _fake_unrecognized_holiday(user_id, time_min, time_max, max_results=5, calendar_id=None):
    return [{"id": "h2", "summary": "Some Unrecognized Observance", "start": "2027-05-01", "end": "2027-05-02", "location": "", "source": "Google"}]
db.google_service.list_calendar_events = _fake_unrecognized_holiday
before_unrec = len(users.list_active_notices(UID))
db._check_holiday_greeting()
check("an unrecognized/untranslatable holiday title is silently skipped (never shows raw English or garbled text)",
      len(users.list_active_notices(UID)) == before_unrec)

def _raising_calendar(user_id, time_min, time_max, max_results=5, calendar_id=None):
    raise RuntimeError("Calendar API exploded")
db.google_service.list_calendar_events = _raising_calendar
try:
    db._check_holiday_greeting()
    check("a Calendar API exception during the holiday check doesn't raise out of the function", True)
except Exception as e:
    check("a Calendar API exception during the holiday check doesn't raise out of the function", False, str(e))

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

# ---------------------------------------------------------------------------- daily_briefing.py: meeting conflicts
def _fake_overlapping_events(user_id, time_min, time_max, max_results=20, calendar_id="primary"):
    return [
        {"id": "e1", "summary": "פגישת צוות", "start": "2027-01-01T10:00:00+02:00", "end": "2027-01-01T11:00:00+02:00", "location": "", "source": "Google"},
        {"id": "e2", "summary": "שיחה עם לקוח", "start": "2027-01-01T10:30:00+02:00", "end": "2027-01-01T11:30:00+02:00", "location": "", "source": "Google"},
    ]
db.google_service.list_calendar_events = _fake_overlapping_events
db._check_meeting_conflicts()
conflict_notices = [n for n in users.list_active_notices(UID) if n["category"] == "conflict"]
check("two overlapping timed events create exactly one conflict notice", len(conflict_notices) == 1, conflict_notices)
check("the conflict notice names both events", conflict_notices and "פגישת צוות" in conflict_notices[0]["body"] and "שיחה עם לקוח" in conflict_notices[0]["body"], conflict_notices)
check("the conflict notice carries an action_label", conflict_notices and conflict_notices[0]["action_label"], conflict_notices)

db._check_meeting_conflicts()
check("re-scanning the SAME overlapping pair does not duplicate the notice",
      len([n for n in users.list_active_notices(UID) if n["category"] == "conflict"]) == 1)

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

def _fake_nonoverlapping_events(user_id, time_min, time_max, max_results=20, calendar_id="primary"):
    return [
        {"id": "e3", "summary": "פגישה א", "start": "2027-01-01T09:00:00+02:00", "end": "2027-01-01T09:30:00+02:00", "location": "", "source": "Google"},
        {"id": "e4", "summary": "פגישה ב", "start": "2027-01-01T10:00:00+02:00", "end": "2027-01-01T10:30:00+02:00", "location": "", "source": "Google"},
    ]
db.google_service.list_calendar_events = _fake_nonoverlapping_events
db._check_meeting_conflicts()
check("back-to-back (non-overlapping) events do NOT create a conflict notice",
      len([n for n in users.list_active_notices(UID) if n["category"] == "conflict"]) == 0)

def _fake_allday_and_timed(user_id, time_min, time_max, max_results=20, calendar_id="primary"):
    return [
        {"id": "e5", "summary": "חופשה", "start": "2027-01-01", "end": "2027-01-02", "location": "", "source": "Google"},
        {"id": "e6", "summary": "פגישה", "start": "2027-01-01T10:00:00+02:00", "end": "2027-01-01T10:30:00+02:00", "location": "", "source": "Google"},
    ]
db.google_service.list_calendar_events = _fake_allday_and_timed
db._check_meeting_conflicts()
check("an all-day event is excluded from conflict-checking (never flagged as 'overlapping' a timed event)",
      len([n for n in users.list_active_notices(UID) if n["category"] == "conflict"]) == 0)

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

# Regression: A (9:00-11:00) contains both B (9:15-9:30) and C (9:45-10:00).
# B and C don't overlap EACH OTHER, so in start-sorted order they're
# adjacent with nothing between them -- an adjacent-pairs-only sweep would
# check (A,B) and (B,C) and miss the real (A,C) overlap entirely.
def _fake_nested_overlaps(user_id, time_min, time_max, max_results=20, calendar_id="primary"):
    return [
        {"id": "eA", "summary": "פגישה א", "start": "2027-01-01T09:00:00+02:00", "end": "2027-01-01T11:00:00+02:00", "location": "", "source": "Google"},
        {"id": "eB", "summary": "פגישה ב", "start": "2027-01-01T09:15:00+02:00", "end": "2027-01-01T09:30:00+02:00", "location": "", "source": "Google"},
        {"id": "eC", "summary": "פגישה ג", "start": "2027-01-01T09:45:00+02:00", "end": "2027-01-01T10:00:00+02:00", "location": "", "source": "Google"},
    ]
db.google_service.list_calendar_events = _fake_nested_overlaps
db._check_meeting_conflicts()
nested_notices = [n for n in users.list_active_notices(UID) if n["category"] == "conflict"]
check("non-adjacent overlaps are all caught, not just adjacent-in-sort-order pairs (A-B, A-C, and B does NOT overlap C)",
      len(nested_notices) == 2
      and any("פגישה א" in n["body"] and "פגישה ב" in n["body"] for n in nested_notices)
      and any("פגישה א" in n["body"] and "פגישה ג" in n["body"] for n in nested_notices)
      and not any("פגישה ב" in n["body"] and "פגישה ג" in n["body"] for n in nested_notices),
      nested_notices)
check("a conflict notice carries the 'open calendar' action_label", all(n["action_label"] == "פתח יומן" for n in nested_notices), nested_notices)

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

# ---------------------------------------------------------------------------- daily_briefing.py: market swing (indices)
for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

db.stocks_service.get_watched_stocks_snapshot = lambda: []  # no individual-stock moves in this sub-section

db.stocks_service.get_market_snapshot = lambda: [
    {"label": "S&P 500", "today_pct": -3.2, "week_pct": -4.0},
    {"label": "Dow Jones", "today_pct": 0.5, "week_pct": 1.0},
]
db._check_market_swing()
market_notices = [n for n in users.list_active_notices(UID) if n["category"] == "market"]
check("only the index that crossed the threshold (3.0%) gets a notice", len(market_notices) == 1, market_notices)
check("the notice names the index and direction", market_notices and "S&P 500" in market_notices[0]["title"] and "ירד" in market_notices[0]["body"], market_notices)

db._check_market_swing()
check("re-running the same day's snapshot does not duplicate the notice",
      len([n for n in users.list_active_notices(UID) if n["category"] == "market"]) == 1)

db.stocks_service.get_market_snapshot = lambda: []
before_empty = len(users.list_active_notices(UID))
db._check_market_swing()
check("an empty snapshot (API returned nothing) is a safe no-op", len(users.list_active_notices(UID)) == before_empty)

def _raising_snapshot():
    raise RuntimeError("Yahoo Finance exploded")
db.stocks_service.get_market_snapshot = _raising_snapshot
try:
    db._check_market_swing()
    check("a stocks_service exception doesn't raise out of the market-swing check", True)
except Exception as e:
    check("a stocks_service exception doesn't raise out of the market-swing check", False, str(e))

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

# ---------------------------------------------------------------------------- daily_briefing.py: market swing (individual stocks)
# Added per explicit user feedback: Nvidia dropping ~4% should surface a
# notice too, not just broad-index moves -- a separate, fixed watched-stocks
# list (stocks_service._WATCHED_STOCKS), not a per-user configurable one.
db.stocks_service.get_market_snapshot = lambda: []  # no index moves in this sub-section

db.stocks_service.get_watched_stocks_snapshot = lambda: [
    {"label": "Nvidia", "today_pct": -4.1, "week_pct": -6.0},
    {"label": "Apple", "today_pct": 0.3, "week_pct": 1.2},
]
db._check_market_swing()
stock_notices = [n for n in users.list_active_notices(UID) if n["category"] == "stock"]
check("only the stock that crossed the threshold (3.0%) gets a notice, not every watched stock", len(stock_notices) == 1, stock_notices)
check("the stock notice names the company and direction (feminine form for 'מניה')", stock_notices and "Nvidia" in stock_notices[0]["title"] and "ירדה" in stock_notices[0]["body"], stock_notices)
check("a stock notice is a DIFFERENT category from an index notice (never collides/conflates)", stock_notices and stock_notices[0]["category"] != "market", stock_notices)

db._check_market_swing()
check("re-running the same day's stock snapshot does not duplicate the notice",
      len([n for n in users.list_active_notices(UID) if n["category"] == "stock"]) == 1)
for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

db.stocks_service.get_watched_stocks_snapshot = lambda: []
before_empty_stock = len(users.list_active_notices(UID))
db._check_market_swing()
check("an empty watched-stocks snapshot is a safe no-op", len(users.list_active_notices(UID)) == before_empty_stock)

# Both snapshots raising in the SAME cycle: confirms the stock-fetch
# exception is caught independently and doesn't prevent the index half
# (checked first) from still running and recording its own notice.
db.stocks_service.get_market_snapshot = lambda: [{"label": "Nasdaq", "today_pct": 3.5, "week_pct": 4.0}]
def _raising_stock_snapshot():
    raise RuntimeError("Yahoo Finance exploded")
db.stocks_service.get_watched_stocks_snapshot = _raising_stock_snapshot
try:
    db._check_market_swing()
    check("a get_watched_stocks_snapshot exception doesn't raise out of the market-swing check", True)
except Exception as e:
    check("a get_watched_stocks_snapshot exception doesn't raise out of the market-swing check", False, str(e))
check("...and the index half (checked first) still ran and recorded its own notice despite the stock half failing",
      any(n["category"] == "market" and "Nasdaq" in n["title"] for n in users.list_active_notices(UID)),
      users.list_active_notices(UID))

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

# ---------------------------------------------------------------------------- daily_briefing.py: inbox backlog
db.google_service.get_unread_count = lambda user_id: 20
db.INBOX_BACKLOG_THRESHOLD = 15
db._check_inbox_backlog()
backlog_notices = [n for n in users.list_active_notices(UID) if n["category"] == "inbox_backlog"]
check("a backlog over the threshold creates exactly one notice, naming the real count", len(backlog_notices) == 1 and "20" in backlog_notices[0]["body"], backlog_notices)
check("the backlog notice carries a 'go to mail' action_label", backlog_notices and backlog_notices[0]["action_label"] == "עבור לדואר", backlog_notices)

db._check_inbox_backlog()
check("re-running the same day's backlog check does not duplicate the notice",
      len([n for n in users.list_active_notices(UID) if n["category"] == "inbox_backlog"]) == 1)

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

db.google_service.get_unread_count = lambda user_id: 5
before_low = len(users.list_active_notices(UID))
db._check_inbox_backlog()
check("a backlog below the threshold creates no notice", len(users.list_active_notices(UID)) == before_low)

db.google_service.get_unread_count = lambda user_id: None
try:
    db._check_inbox_backlog()
    check("an unconnected/failed unread-count check (None) doesn't raise", True)
except Exception as e:
    check("an unconnected/failed unread-count check (None) doesn't raise", False, str(e))

# ---------------------------------------------------------------------------- daily_briefing.py: missing location
def _fake_event_no_location(user_id, time_min, time_max, max_results=10, calendar_id="primary"):
    return [{"id": "eNoLoc", "summary": "פגישת צוות", "start": "2027-02-01T10:00:00+02:00", "end": "2027-02-01T10:30:00+02:00", "location": "", "source": "Google"}]
db.google_service.list_calendar_events = _fake_event_no_location
db._check_missing_location()
loc_notices = [n for n in users.list_active_notices(UID) if n["category"] == "missing_location"]
check("an imminent timed event with no location creates exactly one notice", len(loc_notices) == 1 and "פגישת צוות" in loc_notices[0]["body"], loc_notices)

db._check_missing_location()
check("re-scanning the SAME event does not duplicate the notice",
      len([n for n in users.list_active_notices(UID) if n["category"] == "missing_location"]) == 1)
for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

def _fake_event_with_location(user_id, time_min, time_max, max_results=10, calendar_id="primary"):
    return [{"id": "eLoc", "summary": "פגישה", "start": "2027-02-01T10:00:00+02:00", "end": "2027-02-01T10:30:00+02:00", "location": "חדר ישיבות 2", "source": "Google"}]
db.google_service.list_calendar_events = _fake_event_with_location
before_has_loc = len(users.list_active_notices(UID))
db._check_missing_location()
check("an event that already has a location creates no notice", len(users.list_active_notices(UID)) == before_has_loc)

def _fake_event_with_link(user_id, time_min, time_max, max_results=10, calendar_id="primary"):
    return [{"id": "eLink", "summary": "שיחת זום https://zoom.us/j/123", "start": "2027-02-01T10:00:00+02:00", "end": "2027-02-01T10:30:00+02:00", "location": "", "source": "Google"}]
db.google_service.list_calendar_events = _fake_event_with_link
before_has_link = len(users.list_active_notices(UID))
db._check_missing_location()
check("an event whose summary already looks like a video-call link creates no notice", len(users.list_active_notices(UID)) == before_has_link)

def _fake_event_with_meet(user_id, time_min, time_max, max_results=10, calendar_id="primary"):
    return [{"id": "eMeet", "summary": "סנכרון צוות", "start": "2027-02-01T10:00:00+02:00", "end": "2027-02-01T10:30:00+02:00", "location": "", "hangout_link": "https://meet.google.com/abc-defg-hij", "source": "Google"}]
db.google_service.list_calendar_events = _fake_event_with_meet
before_has_meet = len(users.list_active_notices(UID))
db._check_missing_location()
check("an event with a Google Meet link (hangout_link, not location/summary) creates no notice -- it DOES have a place to be",
      len(users.list_active_notices(UID)) == before_has_meet)

# ---------------------------------------------------------------------------- daily_briefing.py: weather vs calendar
class _FakeResp:
    def __init__(self, data): self._data = data
    def raise_for_status(self): pass
    def json(self): return self._data
FORECAST_RAIN = {"weather": [{"date": "today"}, {"date": "tomorrow", "hourly": [{"chanceofrain": "20"}, {"chanceofrain": "70"}]}]}
FORECAST_DRY = {"weather": [{"date": "today"}, {"date": "tomorrow", "hourly": [{"chanceofrain": "5"}, {"chanceofrain": "10"}]}]}

captured_window = {}
def _fake_outdoor_event(user_id, time_min, time_max, max_results=10, calendar_id="primary"):
    captured_window["time_min"], captured_window["time_max"] = time_min, time_max
    return [{"id": "eOutdoor", "summary": "ריצת בוקר בפארק", "start": "2027-02-02T07:00:00+02:00", "end": "2027-02-02T08:00:00+02:00", "location": "", "source": "Google"}]
db.google_service.list_calendar_events = _fake_outdoor_event
db.requests.get = lambda *a, **k: _FakeResp(FORECAST_RAIN)
db._check_weather_vs_calendar()
weather_notices = [n for n in users.list_active_notices(UID) if n["category"] == "weather"]
check("rain forecast + an outdoor-sounding event creates exactly one notice", len(weather_notices) == 1 and "ריצת בוקר" in weather_notices[0]["body"], weather_notices)

# Regression: the calendar window must be "tomorrow" in the app's own
# local day (Asia/Jerusalem), not UTC -- a UTC-day window shifts by the
# local offset and can miss/mislabel events near local midnight.
import datetime as _dt
import productivity_service as _ps
_expected_tomorrow_local = _dt.datetime.now(_ps.LOCAL_TZ) + _dt.timedelta(days=1)
_expected_start_local = _expected_tomorrow_local.replace(hour=0, minute=0, second=0, microsecond=0)
_expected_time_min_utc = _expected_start_local.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
check("the weather check's calendar window starts at LOCAL (Asia/Jerusalem) midnight tomorrow, converted to UTC -- not UTC midnight",
      captured_window.get("time_min") == _expected_time_min_utc,
      (captured_window.get("time_min"), _expected_time_min_utc))

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

db.requests.get = lambda *a, **k: _FakeResp(FORECAST_DRY)
before_dry = len(users.list_active_notices(UID))
db._check_weather_vs_calendar()
check("a dry forecast creates no weather notice even with an outdoor event on the calendar", len(users.list_active_notices(UID)) == before_dry)

def _fake_indoor_event(user_id, time_min, time_max, max_results=10, calendar_id="primary"):
    return [{"id": "eIndoor", "summary": "פגישת סטטוס", "start": "2027-02-02T10:00:00+02:00", "end": "2027-02-02T11:00:00+02:00", "location": "", "source": "Google"}]
db.google_service.list_calendar_events = _fake_indoor_event
db.requests.get = lambda *a, **k: _FakeResp(FORECAST_RAIN)
before_indoor = len(users.list_active_notices(UID))
db._check_weather_vs_calendar()
check("rain forecast but no outdoor-sounding event creates no notice (no fabricated 'outdoor' guess)", len(users.list_active_notices(UID)) == before_indoor)

def _raising_forecast(*a, **k):
    raise RuntimeError("wttr.in exploded")
db.requests.get = _raising_forecast
try:
    db._check_weather_vs_calendar()
    check("a forecast-fetch exception doesn't raise out of the weather check", True)
except Exception as e:
    check("a forecast-fetch exception doesn't raise out of the weather check", False, str(e))

for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

# ---------------------------------------------------------------------------- daily_briefing.py: daily weather (unconditional)
FORECAST_SUNNY_HOT = {"weather": [{"maxtempC": "32", "mintempC": "22", "hourly": [{"weatherDesc": [{"value": "Sunny"}]}, {"weatherDesc": [{"value": "Sunny"}]}, {"weatherDesc": [{"value": "Sunny"}]}]}]}
db.requests.get = lambda *a, **k: _FakeResp(FORECAST_SUNNY_HOT)
db._check_daily_weather()
daily_notices = [n for n in users.list_active_notices(UID) if n["category"] == "daily_weather"]
check("the daily weather card fires UNCONDITIONALLY (no rain, no outdoor event needed) and names the real temps",
      len(daily_notices) == 1 and "22" in daily_notices[0]["body"] and "32" in daily_notices[0]["body"], daily_notices)
check("a recognized condition is translated to Hebrew, never left as raw English", "שמשי" in daily_notices[0]["body"] and "Sunny" not in daily_notices[0]["body"], daily_notices)

db._check_daily_weather()
check("re-running the same day's weather check does not duplicate the notice",
      len([n for n in users.list_active_notices(UID) if n["category"] == "daily_weather"]) == 1)
for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)
# The daily-weather dedupe_key is just "daily_weather:<today>" -- dismissing
# the row above still leaves it in the table (dismissed, not deleted), so a
# same-day re-run would silently no-op against it (dedup checks ANY row
# with that key, dismissed or not -- same rule as everywhere else in this
# feature). Delete it outright so this next scenario gets a clean slate
# for the SAME day, rather than a false "no notice" that's really just
# this test re-colliding with its own earlier row.
with sqlite3.connect(os.environ["JARVIS_DB_PATH"]) as _conn:
    _conn.execute("DELETE FROM system_notices WHERE category = 'daily_weather'")
    _conn.commit()

FORECAST_UNRECOGNIZED_DESC = {"weather": [{"maxtempC": "20", "mintempC": "10", "hourly": [{"weatherDesc": [{"value": "Some Unusual Condition"}]}, {"weatherDesc": [{"value": "Some Unusual Condition"}]}, {"weatherDesc": [{"value": "Some Unusual Condition"}]}]}]}
db.requests.get = lambda *a, **k: _FakeResp(FORECAST_UNRECOGNIZED_DESC)
db._check_daily_weather()
unrecognized_notices = [n for n in users.list_active_notices(UID) if n["category"] == "daily_weather"]
check("an unrecognized condition is omitted entirely (never shows raw/garbled English) -- the temps alone still show",
      len(unrecognized_notices) == 1 and "10" in unrecognized_notices[0]["body"] and "Some Unusual Condition" not in unrecognized_notices[0]["body"],
      unrecognized_notices)
for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)

def _raising_daily_forecast(*a, **k):
    raise RuntimeError("wttr.in exploded")
db.requests.get = _raising_daily_forecast
try:
    db._check_daily_weather()
    check("a daily-weather fetch exception doesn't raise out of the check", True)
except Exception as e:
    check("a daily-weather fetch exception doesn't raise out of the check", False, str(e))
check("...and creates no notice either", all(n["category"] != "daily_weather" for n in users.list_active_notices(UID)))

# ---------------------------------------------------------------------------- users.py + daily_briefing.py: stale tasks
NOW = time.time()
sid_old_task = users.add_task(UID, "לסדר את המחסן", category=None, recurring_day=None)
with sqlite3.connect(os.environ["JARVIS_DB_PATH"]) as conn:
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (NOW - 20 * 86400, sid_old_task))
    conn.commit()

sid_new_task = users.add_task(UID, "לקנות חלב", category=None, recurring_day=None)  # created just now -- NOT stale

sid_old_recurring = users.add_task(UID, "לצאת לריצה", category=None, recurring_day="Monday")
with sqlite3.connect(os.environ["JARVIS_DB_PATH"]) as conn:
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (NOW - 20 * 86400, sid_old_recurring))
    conn.commit()

stale = users.list_stale_tasks(NOW - db.STALE_TASK_DAYS * 86400)
stale_ids = {t["id"] for t in stale}
check("list_stale_tasks finds the old ONE-TIME task", sid_old_task in stale_ids, stale)
check("...but not the recently-created one-time task", sid_new_task not in stale_ids, stale)
check("...and never a RECURRING task, no matter how old (it's supposed to stay open)", sid_old_recurring not in stale_ids, stale)

db._check_stale_tasks()
stale_notices = [n for n in users.list_active_notices(UID) if n["category"] == "stale_task"]
check("the stale-task check creates exactly one notice, naming the real task text", len(stale_notices) == 1 and "לסדר את המחסן" in stale_notices[0]["body"], stale_notices)
check("the stale-task notice carries a 'go to tasks' action_label", stale_notices and stale_notices[0]["action_label"] == "פתח משימות", stale_notices)

db._check_stale_tasks()
check("re-running the check does not duplicate the notice for the same task",
      len([n for n in users.list_active_notices(UID) if n["category"] == "stale_task"]) == 1)

users.mark_task_complete(sid_old_task, UID)
for n in users.list_active_notices(UID):
    users.dismiss_notice(n["id"], UID)
db._check_stale_tasks()
check("a now-completed task is never flagged again", all(n["category"] != "stale_task" for n in users.list_active_notices(UID)))

# ---------------------------------------------------------------------------- app.py: the live routes
import app as jarvis

def _signed_in_client(uid):
    c = jarvis.app.test_client()
    with c.session_transaction() as sess:
        sess["user_id"] = uid
    return c

client = _signed_in_client(UID)

jarvis.productivity_service.get_inbox_structured = lambda user_id, max_results=8: []
jarvis.productivity_service.get_inbox_summary_spoken = lambda emails: "no new mail"
r_inbox = client.get("/api/inbox")
check("/api/inbox (Mail tab) response no longer carries a 'suggestions' field at all -- left untouched",
      "suggestions" not in r_inbox.get_json(), r_inbox.get_json())

nid2 = users.add_system_notice(UID, "market", "תנודה חדה ב-S&P 500", "מדד S&P 500 ירד 3.2% היום.", "market:S&P 500:test-route-day")
r_sugg = client.get("/api/suggestions")
data_sugg = r_sugg.get_json()
check("/api/suggestions returns both email_suggestions and notices keys", "email_suggestions" in data_sugg and "notices" in data_sugg, data_sugg)
check("/api/suggestions includes the active notice just created", any(n["id"] == nid2 for n in data_sugg["notices"]), data_sugg["notices"])

r_dismiss = client.post(f"/api/suggestions/notices/{nid2}/dismiss")
check("notice-dismiss route marks it resolved", r_dismiss.get_json().get("ok") is True, r_dismiss.get_json())
check("...and it drops out of /api/suggestions", all(n["id"] != nid2 for n in client.get("/api/suggestions").get_json()["notices"]))

other_client = _signed_in_client(other_uid)
nid3 = users.add_system_notice(UID, "holiday", "חנוכה מתקרב", "חג חנוכה שמח!", "holiday:חנוכה:test-route-day")
r_dismiss_other = other_client.post(f"/api/suggestions/notices/{nid3}/dismiss")
check("a different signed-in user can't dismiss someone else's notice",
      r_dismiss_other.get_json().get("ok") is False, r_dismiss_other.get_json())

anon = jarvis.app.test_client()
r_anon_sugg = anon.get("/api/suggestions")
check("/api/suggestions 401s when nobody is signed in", r_anon_sugg.status_code == 401, r_anon_sugg.status_code)
r_anon_dismiss = anon.post(f"/api/suggestions/notices/{nid3}/dismiss")
check("notice-dismiss route 401s when nobody is signed in", r_anon_dismiss.status_code == 401, r_anon_dismiss.status_code)

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + str(detail)[:300]))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
