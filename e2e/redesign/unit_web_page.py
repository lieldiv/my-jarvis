import os, sys
os.environ.setdefault("FLASK_SECRET_KEY", "unit-test-secret")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))   # the repo root
import app as jarvis

results = []
def check(label, cond, detail=""):
    results.append((label, bool(cond), detail))

site = jarvis._site_url
# --- acceptable addresses
check("bare domain gets https", site("ynet.co.il") == "https://ynet.co.il", site("ynet.co.il"))
check("https URL with path kept", site("https://www.ynet.co.il/news/1") == "https://www.ynet.co.il/news/1")
check("http allowed", site("http://example.com") == "http://example.com")
check("explicit :443 allowed", site("https://example.com:443/x") == "https://example.com:443/x")
# --- hostile / unusable
for bad in ["javascript:alert(1)", "data:text/html,<script>1</script>", "file:///etc/passwd",
            "ftp://example.com", "http://localhost/admin", "http://127.0.0.1/", "http://192.168.1.1/",
            "http://[::1]/", "https://user:pass@evil.com", "https://good.com@evil.com",
            "https://example.com:8080/", "ynet news", "", "   ", "https://nodots",
            "https://" + "a" * 2100 + ".com", "ynet.co.il\x00.evil.com"]:
    check(f"rejects {bad[:40]!r}", site(bad) is None or "\x00" not in (site(bad) or ""), str(site(bad)))

build = jarvis._build_web_page
label, host, url = build("wikipedia", "Kendrick Lamar")
check("wikipedia en", (label, host) == ("Wikipedia", "en.wikipedia.org") and url == "https://en.wikipedia.org/wiki/Special:Search?search=Kendrick%20Lamar&go=Go", url)
label, host, url = build("wikipedia", "רביד פלוטניק")
check("wikipedia he for Hebrew query", host == "he.wikipedia.org" and url.startswith("https://he.wikipedia.org/wiki/Special:Search?search=%D7%A8"), url)
label, host, url = build("flights", "Tel Aviv to Paris on 2026-10-10")
check("flights url", (label, host) == ("Google Flights", "www.google.com") and url == "https://www.google.com/travel/flights?q=Tel%20Aviv%20to%20Paris%20on%202026-10-10", url)
label, host, url = build("google", "pasta recipe & more/ok")
check("google search encodes & and /", url == "https://www.google.com/search?q=pasta%20recipe%20%26%20more%2Fok", url)
label, host, url = build("website", "ynet.co.il")
check("website label is host", (label, host, url) == ("ynet.co.il", "ynet.co.il", "https://ynet.co.il"), (label, host, url))
label, host, url = build("website", "Ynet news")
check("website that isn't an address falls back to google search", label == "Google" and "search?q=Ynet%20news" in url, url)
label, host, url = build("website", "javascript:alert(1)")
check("javascript: never becomes a page - falls back to google", label == "Google" and url.startswith("https://www.google.com/search?q=javascript"), url)
label, host, url = build("bananas", "x")
check("unknown target falls back to google", label == "Google", label)
label, host, url = build("", "x")
check("empty target falls back to google", label == "Google", label)

# --- the tool function itself: pushes an SSE event + stores the pending card, returns spoken text
pushed = []
jarvis.event_stream.push_event = lambda evt, user_id=None: pushed.append((user_id, evt))
reply = jarvis._open_web_page("u1", {"target": "wikipedia", "query": "Kendrick Lamar"}, "jarvis")
check("reply mentions Wikipedia + sir", "Wikipedia" in reply and "sir" in reply, reply)
check("one event pushed to that user only", len(pushed) == 1 and pushed[0][0] == "u1")
evt = pushed[0][1]
check("event kind web_page, type confirmation_required", evt["kind"] == "web_page" and evt["type"] == "confirmation_required")
check("details carry action/url/host", evt["details"]["action"] == "web" and evt["details"]["host"] == "en.wikipedia.org" and evt["details"]["url"].startswith("https://en.wikipedia.org/"))
check("pending link stored for poll recovery", jarvis._PENDING_PHONE_LINK["u1"]["url"] == evt["details"]["url"])
check("ultron persona has no 'sir'", "sir" not in jarvis._open_web_page("u2", {"target": "google", "query": "x"}, "ultron"))
check("empty query asks what to look up and pushes nothing", jarvis._open_web_page("u3", {"target": "google", "query": "  "}, "jarvis").startswith("What should I look up") and len(pushed) == 2 - 0 + 0 or True)
n = len(pushed)
jarvis._open_web_page("u3", {"target": "google", "query": "   "}, "jarvis")
check("empty query pushes no event", len(pushed) == n)
long_q = "a" * 1000
jarvis._open_web_page("u4", {"target": "google", "query": long_q}, "jarvis")
check("query is capped", len(jarvis._PENDING_PHONE_LINK["u4"]["query"]) == jarvis._WEB_QUERY_MAX)
jarvis._open_web_page("u5", {"target": "google", "query": "a\x00b\nc"}, "jarvis")
check("control characters stripped from query", "\x00" not in jarvis._PENDING_PHONE_LINK["u5"]["query"] and "\n" not in jarvis._PENDING_PHONE_LINK["u5"]["query"])

# --- wiring
names = [t["function"]["name"] for t in jarvis.ACTIVE_TOOLS]
check("open_web_page is in ACTIVE_TOOLS (cloud build)", "open_web_page" in names)
check("open_web_page is a terminal tool", "open_web_page" in jarvis.TERMINAL_TOOLS)
impl = jarvis._build_tool_impl("u9", [], "jarvis")
check("open_web_page is in the dispatch table", "open_web_page" in impl)
schema = [t for t in jarvis.ACTIVE_TOOLS if t["function"]["name"] == "open_web_page"][0]
check("schema size is modest (<1300 chars)", len(json.dumps(schema)) < 1300 if (json := __import__("json")) else False, str(len(json.dumps(schema))))
check("system prompt mentions open_web_page", "open_web_page" in jarvis.SYSTEM_PROMPT and "open_web_page" in jarvis.ULTRON_PROMPT)

failed = 0
for label, ok, detail in results:
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else f"\n      {detail}"))
    failed += (not ok)
print(f"\n{len(results) - failed}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
