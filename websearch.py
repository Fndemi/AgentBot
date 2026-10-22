"""Web search for discover.py. Tavily basic search when it is set up, DuckDuckGo otherwise.

  SEARCH_PROVIDER=tavily     try Tavily first (anything else: DuckDuckGo only)
  TAVILY_API_KEY             Tavily's key; without it DuckDuckGo answers
  TAVILY_MONTHLY_CAP         Tavily calls allowed per calendar month (UTC); after that
                             DuckDuckGo answers until the next month

Every call is counted in search_usage by month and provider. A Tavily call counts even
when it fails, so the cap is never passed. Results come back as DuckDuckGo gives them:
[{"title", "href", "body"}].
"""
from datetime import datetime, timezone

import config
import db

SCHEMA = """CREATE TABLE IF NOT EXISTS search_usage (
    month TEXT NOT NULL,
    provider TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, provider)
)"""


def this_month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def calls(provider, month=None):
    with db.conn() as c:
        c.execute(SCHEMA)
        row = c.execute("SELECT calls FROM search_usage WHERE month=? AND provider=?",
                        (month or this_month(), provider)).fetchone()
    return row[0] if row else 0


def count(provider):
    with db.conn() as c:
        c.execute(SCHEMA)
        c.execute("INSERT INTO search_usage (month, provider, calls) VALUES (?, ?, 1)"
                  " ON CONFLICT (month, provider) DO UPDATE SET calls = calls + 1",
                  (this_month(), provider))


def why_not_tavily():
    """Why Tavily won't be asked, or None if it will."""
    if config.SEARCH_PROVIDER != "tavily":
        return f"SEARCH_PROVIDER is {config.SEARCH_PROVIDER or 'not set'}"
    if not config.TAVILY_API_KEY:
        return "no TAVILY_API_KEY"
    if calls("tavily") >= config.TAVILY_MONTHLY_CAP:
        return f"this month's cap of {config.TAVILY_MONTHLY_CAP} Tavily calls is reached"
    return None


def tavily(query, max_results):
    from tavily import TavilyClient
    count("tavily")
    answer = TavilyClient(api_key=config.TAVILY_API_KEY).search(
        query, search_depth="basic", max_results=max_results)
    return [{"title": r.get("title") or "", "href": r.get("url") or "", "body": r.get("content") or ""}
            for r in answer.get("results") or []]


def duckduckgo(query, max_results):
    from ddgs import DDGS
    count("duckduckgo")
    return DDGS().text(query, max_results=max_results) or []


def search(query, max_results=10):
    """[{"title", "href", "body"}] for the query, or [] if no provider could answer."""
    reason = why_not_tavily()
    if reason is None:
        try:
            results = tavily(query, max_results)
            print(f"    (tavily answered: {len(results)} results)")
            return results
        except Exception as e:
            # the message is the error's type only: some carry the request, key included
            reason = f"Tavily failed ({type(e).__name__})"
    try:
        results = duckduckgo(query, max_results)
        print(f"    (duckduckgo answered: {len(results)} results; {reason})")
        return results
    except Exception as e:
        print(f"    search failed: {e}")
        return []
