"""
stocks_service.py — zero-LLM stock quote lookup, backing the HUD's "מניות"
button.

Same "deterministic bypass" philosophy as /api/agenda/week and
daily_briefing.py: this never goes through Groq/Gemini at all — just a
plain HTTP call to Yahoo Finance's public (unofficial, no API key or
signup needed) endpoints, so a search can't be broken by a bad LLM
response and doesn't cost anything against either free-tier quota.

Two calls per lookup: a symbol search (so a user can type "apple" instead
of needing to know "AAPL"), then a chart fetch for that ticker to compute
today's and this week's change. Unofficial API — no SLA, no key, can
change or start blocking a host's IP without notice; every call is wrapped
so a failure degrades to a clear error dict instead of a stack trace.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import cert_bootstrap  # noqa: F401 — must run before any HTTPS-making import below
import requests

logger = logging.getLogger("jarvis.stocks")

_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# A bare requests User-Agent gets a 403 from this endpoint; any real
# browser-looking one satisfies it.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _resolve_symbol(query: str):
    res = requests.get(
        _SEARCH_URL,
        params={"q": query, "quotesCount": 5, "newsCount": 0},
        headers=_HEADERS, timeout=6,
    )
    res.raise_for_status()
    quotes = [
        q for q in res.json().get("quotes", [])
        if q.get("symbol") and q.get("quoteType") == "EQUITY"
    ]
    return quotes[0] if quotes else None  # Yahoo already ranks these by relevance


def get_quote(query: str) -> dict:
    """Returns a data dict on success:
      {symbol, name, exchange, currency, price, change_today, change_today_pct,
       change_week, change_week_pct, day_high, day_low, week_52_high, week_52_low,
       closes, dates, volumes}
    or {"error": "..."} on failure — never raises, since this is called
    directly from a Flask route with no LLM in between to smooth over an
    unexpected shape."""
    query = (query or "").strip()
    if not query:
        return {"error": "What company or ticker should I look up, sir?"}
    try:
        match = _resolve_symbol(query)
        if not match:
            return {"error": f"Couldn't find a ticker matching '{query}', sir."}
        symbol = match["symbol"]

        # 1mo/1d (~22 trading days) instead of the previous 5d — the HUD's
        # chart was legible but visibly sparse at 5 points; a month gives
        # it an actual shape to trace without needing a paid/richer data
        # source. Still one HTTP call, same free unofficial endpoint.
        res = requests.get(
            _CHART_URL.format(symbol=symbol),
            params={"range": "1mo", "interval": "1d"},
            headers=_HEADERS, timeout=6,
        )
        res.raise_for_status()
        result = res.json()["chart"]["result"][0]
        meta = result["meta"]
        raw_closes = result["indicators"]["quote"][0]["close"]
        raw_volumes = result["indicators"]["quote"][0].get("volume") or [0] * len(raw_closes)
        raw_timestamps = result.get("timestamp") or []
        # Some days in range (holidays, or today before/during market
        # hours) come back with a timestamp but a null close — keep only
        # the pairs where both are real so dates and closes stay aligned
        # (zipping first, then filtering, rather than filtering closes
        # alone as before — that would silently desync the two arrays).
        paired = [
            (t, c, v) for t, c, v in zip(raw_timestamps, raw_closes, raw_volumes)
            if c is not None
        ]
        if not paired:
            return {"error": f"No price data available for {symbol} right now, sir."}
        closes = [c for _, c, _ in paired]
        volumes = [v or 0 for _, _, v in paired]
        dates = [
            datetime.fromtimestamp(t, tz=timezone.utc).strftime("%d %b")
            for t, _, _ in paired
        ]

        price = meta.get("regularMarketPrice", closes[-1])
        # meta's chartPreviousClose is NOT "yesterday's close" — it's the
        # close right before whatever range was requested, so its value
        # (and meaning) silently shifts with the range param: confirmed by
        # querying the same symbol with range=5d vs range=1mo back to back
        # and getting two different numbers back for it. previousClose
        # (the field that would actually mean "yesterday") comes back
        # None from this unofficial endpoint far more often than not. The
        # one number guaranteed to mean "the trading day before today" is
        # our own fetched daily series' second-to-last close.
        prev_close = closes[-2] if len(closes) >= 2 else (meta.get("previousClose") or price)
        week_open = closes[max(0, len(closes) - 5)]  # ~5 trading days back, not the full month's start

        change_today = price - prev_close
        change_week = price - week_open

        return {
            "symbol": symbol,
            "name": match.get("shortname") or match.get("longname") or symbol,
            "exchange": meta.get("fullExchangeName", match.get("exchange", "")),
            "currency": meta.get("currency", ""),
            "price": round(price, 2),
            "change_today": round(change_today, 2),
            "change_today_pct": round(change_today / prev_close * 100, 2) if prev_close else 0,
            "change_week": round(change_week, 2),
            "change_week_pct": round(change_week / week_open * 100, 2) if week_open else 0,
            "day_high": round(meta.get("regularMarketDayHigh", price), 2),
            "day_low": round(meta.get("regularMarketDayLow", price), 2),
            "week_52_high": round(meta.get("fiftyTwoWeekHigh", 0) or 0, 2),
            "week_52_low": round(meta.get("fiftyTwoWeekLow", 0) or 0, 2),
            # ~1 month of daily closes, oldest first — enough for the HUD
            # to draw an actual trend chart instead of a 5-point sparkline.
            "closes": [round(c, 2) for c in closes],
            # Parallel to closes — short "DD Mon" labels for the chart's
            # own axis, computed here (not guessed client-side) since only
            # the backend has the real trading-day timestamps.
            "dates": dates,
            # Parallel to closes — daily trading volume for the volume-bar
            # subplot under the price chart.
            "volumes": volumes,
        }
    except requests.RequestException as e:
        logger.warning(f"Stock quote fetch failed for '{query}': {e}")
        return {"error": "Couldn't reach the stock data service right now, sir."}
    except Exception as e:
        logger.error(f"Stock quote parsing failed for '{query}': {e}")
        return {"error": "Something went wrong reading that stock's data, sir."}


# Major US indices — enough to answer "how's the market doing" / "did
# stocks drop this week" without any AI search at all. This exists because
# get_current_info (Gemini) turned out to need Google Cloud billing set up
# even for its free tier, which the user didn't want to do — this answers
# the one question that actually motivated adding Gemini in the first
# place, with a service that's been zero-key and zero-cost the whole time.
_MARKET_INDICES = [("^GSPC", "S&P 500"), ("^DJI", "Dow Jones"), ("^IXIC", "Nasdaq")]


def _fetch_index_change(symbol: str):
    """Returns (today_pct, week_pct) for one index, or None on failure.
    Shared by get_market_summary() (spoken sentence) and
    get_market_snapshot() (structured, for the stock panel's "vs the
    market" card) so the previousClose fix below only has to be right
    once."""
    try:
        res = requests.get(
            _CHART_URL.format(symbol=requests.utils.quote(symbol, safe="")),
            params={"range": "5d", "interval": "1d"},
            headers=_HEADERS, timeout=6,
        )
        res.raise_for_status()
        result = res.json()["chart"]["result"][0]
        meta = result["meta"]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if not closes:
            return None
        price = meta.get("regularMarketPrice", closes[-1])
        # meta's chartPreviousClose means "close before this request's
        # range", not "yesterday" — confirmed wrong here too (was showing
        # S&P 500 -1.14% today against Yahoo's own -0.45%). closes[-2] is
        # the actual prior trading day from our own fetched series.
        prev_close = closes[-2] if len(closes) >= 2 else (meta.get("previousClose") or price)
        week_open = closes[0]
        today_pct = (price - prev_close) / prev_close * 100 if prev_close else 0
        week_pct = (price - week_open) / week_open * 100 if week_open else 0
        return today_pct, week_pct
    except Exception as e:
        logger.warning(f"Index fetch failed for {symbol}: {e}")
        return None


def _fetch_all_indices() -> list:
    """Runs _fetch_index_change for all three indices concurrently — these
    are three independent HTTPS calls to the same slow unofficial API, and
    running them one after another (the original shape of this function)
    meant every caller paid the sum of all three latencies (measured at
    6.6s sequential vs ~2s in parallel) for what should cost only the
    slowest single one. Returns [(label, (today_pct, week_pct) | None)]
    in the same order as _MARKET_INDICES."""
    with ThreadPoolExecutor(max_workers=len(_MARKET_INDICES)) as pool:
        changes = list(pool.map(lambda pair: _fetch_index_change(pair[0]), _MARKET_INDICES))
    return [(label, change) for (_, label), change in zip(_MARKET_INDICES, changes)]


def get_market_summary() -> str:
    lines = []
    for label, change in _fetch_all_indices():
        if change is None:
            continue
        today_pct, week_pct = change
        today_dir = "up" if today_pct >= 0 else "down"
        week_dir = "up" if week_pct >= 0 else "down"
        lines.append(f"{label} is {today_dir} {abs(today_pct):.1f}% today and {week_dir} {abs(week_pct):.1f}% this week")
    if not lines:
        return "I couldn't reach the market data service right now, sir."
    return "Here's the market, sir: " + "; ".join(lines) + "."


def get_market_snapshot() -> list:
    """Structured version of get_market_summary() for the stock panel's
    "today vs the market" card — same three indices, same numbers, shaped
    for the UI to render as rows instead of a spoken sentence."""
    out = []
    for label, change in _fetch_all_indices():
        if change is None:
            continue
        today_pct, week_pct = change
        out.append({"label": label, "today_pct": round(today_pct, 2), "week_pct": round(week_pct, 2)})
    return out
