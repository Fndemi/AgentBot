"""Web search for discover.py. Tavily basic search when it is set up, DuckDuckGo otherwise.

  SEARCH_PROVIDER=tavily     try Tavily first (anything else: DuckDuckGo only)
  TAVILY_API_KEY             Tavily's key; without it DuckDuckGo answers
  TAVILY_MONTHLY_CAP         Tavily calls allowed per calendar month (UTC); after that
                             DuckDuckGo answers until the next month

The month's cap is shared out by day (daily_allowance). When today's share of Tavily calls
is used, search() raises DailyAllowanceUsed instead of asking DuckDuckGo, so discovery
stops for the day and the rest of the month's calls are kept for the days after.

Every call is counted in search_usage by month and provider, and in search_days by day
and provider. A Tavily call counts even when it fails, so the cap is never passed.
Results come back as DuckDuckGo gives them: [{"title", "href", "body"}].
"""
from calendar import monthrange
from datetime import datetime, timezone

import config
import db

SCHEMA = """CREATE TABLE IF NOT EXISTS search_usage (
    month TEXT NOT NULL,
    provider TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, provider)
)"""
DAY_SCHEMA = """CREATE TABLE IF NOT EXISTS search_days (
    day TEXT NOT NULL,
    provider TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider)
)"""


class DailyAllowanceUsed(Exception):
    """Today's share of the month's Tavily calls is used: no more searches today."""


def now():
    return datetime.now(timezone.utc)


def this_month():
    return now().strftime("%Y-%m")


def today():
    return now().date().isoformat()


def calls(provider, month=None):
    with db.conn() as c:
        c.execute(SCHEMA)
        row = c.execute("SELECT calls FROM search_usage WHERE month=? AND provider=?",
                        (month or this_month(), provider)).fetchone()
    return row[0] if row else 0


def calls_today(provider):
    with db.conn() as c:
        c.execute(DAY_SCHEMA)
        row = c.execute("SELECT calls FROM search_days WHERE day=? AND provider=?",
                        (today(), provider)).fetchone()
    return row[0] if row else 0


def count(provider):
    with db.conn() as c:
        c.execute(SCHEMA)
        c.execute(DAY_SCHEMA)
        c.execute("INSERT INTO search_usage (month, provider, calls) VALUES (?, ?, 1)"
                  " ON CONFLICT (month, provider) DO UPDATE SET calls = search_usage.calls + 1",
                  (this_month(), provider))
        c.execute("INSERT INTO search_days (day, provider, calls) VALUES (?, ?, 1)"
                  " ON CONFLICT (day, provider) DO UPDATE SET calls = search_days.calls + 1",
                  (today(), provider))


def daily_allowance():
    """Tavily calls allowed today: what was left of the month's cap this morning, shared
    evenly over the days left in the month, today included. Calls a day doesn't use (a
    weekend, a quiet day) are shared out over the days after it."""
    t = now()
    days_left = monthrange(t.year, t.month)[1] - t.day + 1
    left = config.TAVILY_MONTHLY_CAP - (calls("tavily") - calls_today("tavily"))
    return max(0, left) // days_left


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
    """[{"title", "href", "body"}] for the query, or [] if no provider could answer.
    Raises DailyAllowanceUsed when Tavily would answer but today's share of it is used."""
    reason = why_not_tavily()
    if reason is None:
        allowance = daily_allowance()
        if calls_today("tavily") >= allowance:
            raise DailyAllowanceUsed(f"today's {allowance} Tavily calls are used "
                                     f"(TAVILY_MONTHLY_CAP {config.TAVILY_MONTHLY_CAP} shared by day)")
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
