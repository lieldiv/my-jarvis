import os, sys
os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))   # the repo root
import productivity_service as ps

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

g, m = ps.google_service, ps.microsoft_service
ps.any_mail_configured = lambda: True
g.CONFIGURED, m.CONFIGURED = True, False

def with_google(ret):
    g.list_recent_emails = lambda user_id, max_results=8, query="is:unread": ret

with_google(None)
check("Google configured but the user's mailbox did not answer -> None (NOT an empty inbox)", ps._collect_emails("u", True, 8) is None)
check("...so the HUD route reports 'not connected'", ps.get_inbox_structured("u") is None)
check("...and the spoken text says no mailbox is connected", "No mailbox is connected" in ps.get_emails_text("u"))
with_google([])
check("an answered-but-empty mailbox is an empty list, not None", ps._collect_emails("u", True, 8) == [])
check("...and reads as a clear inbox", ps.get_emails_text("u") == "Inbox is clear, sir.")
with_google([{"subject": "s", "sender": "Dana Cohen <dana@example.com>", "snippet": "hi", "date_ms": 1790000000000, "source": "Gmail"}])
out = ps.get_inbox_structured("u")
check("structured inbox carries Gmail's own message time (ms)", out and out[0]["date_ms"] == 1790000000000, str(out))
check("...the sender is split into name and address", out[0]["sender_name"] == "Dana Cohen" and out[0]["sender_email"] == "dana@example.com")
with_google([{"subject": "s", "sender": "x@y.com", "snippet": "", "source": "Gmail"}])
check("a message without a time (other providers) gets 0, which the HUD renders as no time label", ps.get_inbox_structured("u")[0]["date_ms"] == 0)

# two providers: one answers, one does not
g.CONFIGURED, m.CONFIGURED = True, True
with_google(None)
m.list_recent_emails = lambda max_results=8, unread_only=True: [{"subject": "o", "sender": "Outlook Person", "snippet": "", "source": "Outlook"}]
check("one provider down, the other answering -> its mail is shown", len(ps._collect_emails("u", True, 8)) == 1)
m.list_recent_emails = lambda max_results=8, unread_only=True: None
check("both providers down -> None", ps._collect_emails("u", True, 8) is None)
# the daily agenda uses `or []`, so a down mailbox must not crash it
check("daily-agenda path tolerates None (uses 'or []')", (ps._collect_emails("u", unread_only=True, max_results=10) or []) == [])

# ---- the calendar had the same conflation
ps.any_calendar_configured = lambda: True
g.CONFIGURED, m.CONFIGURED = True, False
g.list_calendar_events = lambda user_id, tmin, tmax, max_results=15: None
check("calendar: configured server but the user's calendar did not answer -> None (NOT 'nothing scheduled')", ps._collect_events("u", 7, 15) is None)
check("...the HUD route therefore says not connected", ps.get_upcoming_events_structured("u") is None)
check("...the LLM tool says no calendar is connected", "No calendar is connected" in ps.get_calendar_events_text("u"))
g.list_calendar_events = lambda user_id, tmin, tmax, max_results=15: []
check("calendar: an answered-but-empty calendar is [], and reads as nothing scheduled", ps._collect_events("u", 7, 15) == [] and "Nothing on the calendar" in ps.get_calendar_events_text("u"))
g.list_calendar_events = lambda user_id, tmin, tmax, max_results=15: None
g.list_recent_emails = lambda user_id, max_results=8, query="is:unread": None
ps.users.get_user_tasks = lambda uid, include_completed=False: []
brief = ps.get_daily_agenda_text("u")
check("morning briefing: a calendar/mailbox that did not answer is NOT reported as empty/clear", "couldn't reach your calendar" in brief and "couldn't reach your mailbox" in brief and "Nothing on the calendar" not in brief and "Inbox is clear" not in brief, brief)
check("weekly email: same, it does not claim an empty week", "couldn't reach your calendar" in ps.get_weekly_summary_text("u") and "Nothing on your calendar" not in ps.get_weekly_summary_text("u"))
g.list_calendar_events = lambda user_id, tmin, tmax, max_results=15: []
g.list_recent_emails = lambda user_id, max_results=8, query="is:unread": []
brief = ps.get_daily_agenda_text("u")
check("morning briefing: genuinely empty still says so", "Nothing on the calendar" in brief and "Inbox is clear" in brief, brief)

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "\n      " + detail[:300]))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
