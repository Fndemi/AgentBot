"""websearch.search: Tavily first, DuckDuckGo when the key is missing, Tavily fails or the
month's cap is used; every call counted by month and provider. The month's cap is shared out
by day: when today's share is used, search stops for the day instead of asking DuckDuckGo."""
import sys
import types
from datetime import datetime, timezone

import pytest

import config
import db
import websearch

DDG = [{"title": "Sacco A", "href": "https://a.co.ke", "body": "from DuckDuckGo"}]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr(config, "TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(config, "TAVILY_MONTHLY_CAP", 3)
    # the last day of the month: all that is left of the month's cap may be used today
    monkeypatch.setattr(websearch, "now", lambda: datetime(2026, 11, 30, 8, tzinfo=timezone.utc))
    asked = []

    class FakeTavily:
        def __init__(self, api_key):
            assert api_key == "tvly-test"

        def search(self, query, search_depth, max_results):
            asked.append((query, search_depth, max_results))
            if query == "boom":
                raise RuntimeError("Tavily is down")
            return {"results": [{"title": "Sacco T", "url": "https://t.co.ke", "content": "from Tavily"}]}

    class FakeDDGS:
        def text(self, query, max_results):
            asked.append(("duckduckgo", query))
            return DDG

    monkeypatch.setitem(sys.modules, "tavily", types.SimpleNamespace(TavilyClient=FakeTavily))
    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    return asked


def test_tavily_basic_search_answers_in_duckduckgo_shape(world, capsys):
    assert websearch.search("SACCO Nairobi", 5) == [
        {"title": "Sacco T", "href": "https://t.co.ke", "body": "from Tavily"}]
    assert world == [("SACCO Nairobi", "basic", 5)]
    assert websearch.calls("tavily") == 1 and "tavily answered" in capsys.readouterr().out


def test_no_key_uses_duckduckgo(world, monkeypatch, capsys):
    monkeypatch.setattr(config, "TAVILY_API_KEY", "")
    assert websearch.search("SACCO Nairobi") == DDG
    assert world == [("duckduckgo", "SACCO Nairobi")] and websearch.calls("duckduckgo") == 1
    assert "duckduckgo answered: 1 results; no TAVILY_API_KEY" in capsys.readouterr().out


def test_tavily_error_falls_back_and_still_counts(world, capsys):
    assert websearch.search("boom") == DDG
    assert websearch.calls("tavily") == 1 and websearch.calls("duckduckgo") == 1
    out = capsys.readouterr().out
    assert "Tavily failed (RuntimeError)" in out and "tvly-test" not in out


def test_monthly_cap_switches_to_duckduckgo(world):
    for _ in range(3):
        websearch.search("SACCO Nairobi")
    assert websearch.search("SACCO Nairobi") == DDG
    assert len(world) == 4 and world[-1][0] == "duckduckgo" and websearch.calls("tavily") == 3
    assert websearch.calls("tavily", month="1999-01") == 0  # counted by month


def test_duckduckgo_provider_never_asks_tavily(world, monkeypatch):
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "duckduckgo")
    assert websearch.search("SACCO Nairobi") == DDG and world == [("duckduckgo", "SACCO Nairobi")]


def on(monkeypatch, day):
    monkeypatch.setattr(websearch, "now", lambda: datetime(2026, 11, day, 8, tzinfo=timezone.utc))


def test_daily_share_stops_searching_without_duckduckgo(world, monkeypatch):
    monkeypatch.setattr(config, "TAVILY_MONTHLY_CAP", 90)
    on(monkeypatch, 1)                       # 90 calls over November's 30 days: 3 today
    assert websearch.daily_allowance() == 3
    for _ in range(3):
        websearch.search("SACCO Nairobi")
    with pytest.raises(websearch.DailyAllowanceUsed, match="today's 3 Tavily calls are used"):
        websearch.search("SACCO Nairobi")
    assert len(world) == 3 and websearch.calls("duckduckgo") == 0 and websearch.calls_today("tavily") == 3
    on(monkeypatch, 2)                       # a new day: 87 left over 29 days
    assert websearch.calls_today("tavily") == 0 and websearch.daily_allowance() == 3
    assert websearch.search("SACCO Nairobi")[0]["href"] == "https://t.co.ke"


def test_unused_days_are_shared_over_the_rest_of_the_month(world, monkeypatch):
    monkeypatch.setattr(config, "TAVILY_MONTHLY_CAP", 90)
    on(monkeypatch, 16)                      # nothing used yet: 90 over the last 15 days
    assert websearch.daily_allowance() == 6
