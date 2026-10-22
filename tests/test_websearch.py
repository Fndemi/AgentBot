"""websearch.search: Tavily first, DuckDuckGo when the key is missing, Tavily fails or the
month's cap is used; every call counted by month and provider."""
import sys
import types

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
    assert world == [] and websearch.calls("duckduckgo") == 1
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
    assert len(world) == 3 and websearch.calls("tavily") == 3
    assert websearch.calls("tavily", month="1999-01") == 0  # counted by month


def test_duckduckgo_provider_never_asks_tavily(world, monkeypatch):
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "duckduckgo")
    assert websearch.search("SACCO Nairobi") == DDG and world == []
