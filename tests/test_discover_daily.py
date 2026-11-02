"""Daily discovery: DISCOVER_PER_DAY shared evenly across the niches approved and not paused in
uses.yaml, which take turns one search each, list searches first; a niche that finds nothing
leaves its share to the others. Searching stops for the day when today's share of the month's
Tavily calls is used, without DuckDuckGo. Each niche's day is logged: searches, results, leads
added and why the rest were dropped. Searches and websites are fakes: nothing is fetched."""
import sys
import types
from datetime import date, datetime, timezone

import pytest

import config
import db
import discover
import personalise
import websearch


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Every query finds 3 sites, named after the query. A site whose name starts with
    "noemail" gives no email; any other is a Kenyan company with info@ its own address.
    A list page links to 3 company sites. Returns the searches made, in order."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    made = []

    def search(query):
        made.append(query)
        slug = query.replace(" ", "")
        return [{"href": f"https://{slug}{i}.co.ke/"} for i in range(3)]

    monkeypatch.setattr(discover, "search", search)
    monkeypatch.setattr(discover, "look_at", lambda url: (discover.domain_of(url), True, ""))
    monkeypatch.setattr(discover.scraper, "scrape_site", lambda home: (
        set() if discover.domain_of(home).startswith("noemail") else {f"info@{discover.domain_of(home)}"}))
    monkeypatch.setattr(discover, "listed_sites", lambda page: [
        f"https://{discover.domain_of(page)}co{i}.co.ke/" for i in range(3)])
    monkeypatch.setattr(discover, "portfolio_pages", lambda home: [])
    monkeypatch.setattr(discover, "save_csv", lambda row: None)
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)
    monkeypatch.setattr(discover, "TOWNS", ["Nakuru"])
    monkeypatch.setattr(discover, "QUERIES", {
        "sacco": ["sacco one {town}", "sacco two {town}"],
        "fintech": ["fintech one {town}", "fintech two {town}"],
        "tech_startup": ["startup one {town}"],
        "noemail": ["noemail one {town}", "noemail two {town}"]})
    monkeypatch.setattr(discover, "LIST_QUERIES", {"tech_startup": ["accelerator portfolio"]})
    monkeypatch.setattr(websearch, "now", lambda: datetime(2026, 11, 2, 8, tzinfo=timezone.utc))
    return made


def by_niche():
    return {r["niche"]: r for r in discover.log_rows()}


def test_only_approved_unpaused_niches_are_searched(monkeypatch):
    monkeypatch.setitem(personalise.USES, "school", dict(personalise.USES["school"], paused_until="2027-01-05"))
    found = discover.niches(date(2026, 11, 2))
    assert found and all(personalise.approved({"industry": n}) for n in found)
    assert found == [n for n in discover.PRIORITY if n in found]           # PRIORITY order
    assert "college" not in found                                           # not approved
    assert "school" not in found                                            # paused until 2027-01-05
    assert "school" in discover.niches(date(2027, 1, 5))


def test_the_day_is_shared_evenly_and_niches_take_turns(world, monkeypatch):
    monkeypatch.setattr(discover, "niches", lambda: ["sacco", "fintech", "tech_startup"])
    monkeypatch.setattr(config, "DISCOVER_PER_DAY", 6)
    added = discover.run()
    # one search each, in turn; tech_startup's list search before its ordinary one
    assert world == ["sacco one Nakuru", "fintech one Nakuru", "accelerator portfolio"]
    assert [sum(r["industry"] == n for r in added) for n in ("sacco", "fintech", "tech_startup")] == [2, 2, 2]
    log = by_niche()
    assert {n: (r["searches"], r["results"], r["added"]) for n, r in log.items()} == {
        "sacco": (1, 3, 2), "fintech": (1, 3, 2), "tech_startup": (1, 3, 2)}
    assert all(r["day"] == "2026-11-02" for r in log.values())


def test_a_niche_that_finds_nothing_leaves_its_share_to_the_others(world, monkeypatch):
    monkeypatch.setattr(discover, "niches", lambda: ["noemail", "fintech"])
    added = discover.run(max_new=4)
    assert world == ["noemail one Nakuru", "fintech one Nakuru", "noemail two Nakuru", "fintech two Nakuru"]
    assert len(added) == 4 and all(r["industry"] == "fintech" for r in added)
    log = by_niche()
    assert (log["noemail"]["searches"], log["noemail"]["added"]) == (2, 0)
    assert log["noemail"]["dropped"] == {"no email found": 6}
    assert log["fintech"]["added"] == 4


def test_drop_reasons_and_runs_on_the_same_day_add_up(world, monkeypatch):
    monkeypatch.setattr(discover, "niches", lambda: ["sacco"])
    db.add_lead("Known", "elsewhere.co.ke", "sacco", "info@saccoonenakuru0.co.ke", "csv", "", "")
    discover.run(max_new=1)       # site 0's email is already a lead's; site 1 is added
    discover.run(max_new=1)       # the same search again: site 0 as before, site 1 known, site 2 added
    r = by_niche()["sacco"]
    assert (r["searches"], r["results"], r["added"]) == (2, 6, 2)
    assert r["dropped"] == {"email already a lead": 2, "already known": 1}


def test_searching_stops_for_the_day_when_tavilys_share_is_used(world, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(discover, "search", lambda q: websearch.search(q, 10))
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr(config, "TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(config, "TAVILY_MONTHLY_CAP", 58)    # 58 over November's 29 days left: 2 today
    asked = []

    class FakeTavily:
        def __init__(self, api_key):
            pass

        def search(self, query, search_depth, max_results):
            asked.append(query)
            return {"results": [{"title": "", "url": f"https://t{len(asked)}.co.ke", "content": ""}]}

    class FakeDDGS:
        def text(self, query, max_results):
            pytest.fail("DuckDuckGo was asked after today's Tavily share was used")

    monkeypatch.setitem(sys.modules, "tavily", types.SimpleNamespace(TavilyClient=FakeTavily))
    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    monkeypatch.setattr(discover, "niches", lambda: ["sacco", "fintech"])
    added = discover.run(max_new=50)
    assert asked == ["sacco one Nakuru", "fintech one Nakuru"] and len(added) == 2
    assert "Stopped for today: today's 2 Tavily calls are used" in capsys.readouterr().out
    assert {n: r["searches"] for n, r in by_niche().items()} == {"sacco": 1, "fintech": 1}
