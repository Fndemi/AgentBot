"""Lead quality (names: test_names.py). Directories, marketplaces, events, associations,
communities, accelerators, media and government sites are never leads: discovery skips them
(and visits the companies they list), and existing ones are excluded with no more emails.
Before a first email the AI says which approved niche the lead's site belongs to: one that fits
none is never emailed, one that fits another moves there. Websites, the AI and the mail
server are fakes: nothing is fetched, asked or sent."""
import json

import pytest
from bs4 import BeautifulSoup

import classifier
import config
import db
import discover
import names
import nichefit
import personalise
import quality
import ranking
import sender
import targeting

REAL_SITE_NICHE = nichefit.site_niche


def soup(html):
    return BeautifulSoup(html, "html.parser")


# --- What is never a lead ---

@pytest.mark.parametrize("domain, text, why", [
    ("kra.go.ke", "Kenya Revenue Authority", "government body"),
    ("kbd.co.ke", "Kenya Business Directory | Find companies", "directory or marketplace"),
    ("aff.co.ke", "Africa Fintech Summit 2026", "event"),
    ("kba.co.ke", "Kenya Bankers Association", "association or chamber"),
    ("nbo.co.ke", "Nairobi Chamber of Commerce", "association or chamber"),
    ("dev.co.ke", "Nairobi Developer Community", "community"),
    ("lab.co.ke", "Startup accelerator for East Africa", "accelerator or investor"),
    ("tm.co.ke", "Tech Magazine Kenya | Latest tech news", "media or news"),
    ("ok.co.ke", "Acme | School management software for Kenyan schools", None),
    ("ok2.co.ke", "Join a community of 10,000 merchants paid by M-Pesa", None),
    ("hivisasa.com", "Hivisasa | Find the latest local news from all of Kenya", "media or news"),
    # real companies, found while previewing the existing leads
    ("imarishasacco.co.ke", "Imarisha Sacco | a branch network of 14 units", None),
    ("realpeople.co.ke", "Real People | Stay up to date with latest news and insights about finance", None),
    ("c3churchnairobi.com", "C3 NAIROBI | Join Our Faith Community", None),
])
def test_excluded_kinds(domain, text, why):
    assert quality.excluded_reason(domain, text) == why


@pytest.fixture
def web(tmp_path, monkeypatch):
    """Fake websites: {domain: Page}. A listing's links are LINKS[domain]."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    pages, links = {}, {}
    monkeypatch.setattr(discover, "look_at", lambda url: pages.get(discover.domain_of(url)))
    monkeypatch.setattr(discover.scraper, "scrape_site", lambda home: {f"info@{discover.domain_of(home)}"})
    monkeypatch.setattr(discover, "listed_sites", lambda page: links.get(discover.domain_of(page), []))
    monkeypatch.setattr(discover, "portfolio_pages", lambda home: [])
    monkeypatch.setattr(discover, "save_csv", lambda row: None)
    return pages, links


def test_discovery_skips_an_event_but_adds_the_companies_it_lists(web):
    pages, links = web
    pages["fintechsummit.co.ke"] = discover.Page("Africa Fintech Summit", True, "", None,
                                                 "Africa Fintech Summit | Nairobi, May 2026")
    pages["zidicapital.com"] = discover.Page("Fast loans in minutes", True, "",
                                             names.Name("Zidi Capital Ltd", "Zidi Capital", "footer"), "Fast loans")
    pages["kra.go.ke"] = discover.Page("KRA", True, "", None, "Kenya Revenue Authority")
    links["fintechsummit.co.ke"] = ["https://zidicapital.com/", "https://kra.go.ke/"]
    tally = discover.Tally()
    added = discover.visit("https://fintechsummit.co.ke/", "fintech", "fintech Kenya", set(), tally, 10)
    assert [(r["company"], r["website"]) for r in added] == [("Zidi Capital Ltd", "zidicapital.com")]
    lead = db.find_lead_by_email("info@zidicapital.com")
    assert (lead["display_name"], lead["name_source"]) == ("Zidi Capital", "footer")
    assert tally.dropped == {"event": 1, "government body": 1}
    assert db.find_lead_by_email("info@fintechsummit.co.ke") is None


def test_a_discovered_lead_without_a_good_name_gets_the_domain_brand(web):
    pages, _ = web
    pages["cragroup.co.ke"] = discover.Page("Credit Reports | Best in Kenya", True, "", None, "Credit Reports")
    row = discover.consider("https://cragroup.co.ke/", "fintech", "q", set())
    assert row["company"] == "CRA Group"
    assert db.find_lead_by_email("info@cragroup.co.ke")["name_source"] == "domain"


# --- Existing leads ---

def add(email, company, industry="fintech", status="verified", source="search", website=None):
    db.add_lead(company, email.split("@")[1] if website is None else website or None, industry, email, source)
    lead_id = db.find_lead_by_email(email)["id"]
    db.set_status(lead_id, status, "test")
    return lead_id


def test_clean_leads_shows_then_excludes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    sites = {"https://cragroup.co.ke": "<title>Credit Reports | Best in Kenya</title>",
             "https://fintechweek.co.ke": "<title>Nairobi Fintech Week 2026 | Conference and Expo</title>",
             "https://zidi.co.ke": '<meta property="og:site_name" content="Zidi">'}
    monkeypatch.setattr(quality.scraper, "fetch", lambda url: soup(sites[url]) if url in sites else None)
    tagline = add("info@cragroup.co.ke", "Credit Reports | Best Credit Bureau in Kenya")
    event = add("info@fintechweek.co.ke", "Nairobi Fintech Week 2026", status="sent")
    good = add("info@zidi.co.ke", "Zidi")
    gov = add("info@ict.go.ke", "Ministry of ICT", source="csv")
    lender = add("loans@gmail.com", "Quick Cash Credit Ltd", "digital_lender", source="CBK DCP directory", website="")
    db.record_sent(event, "<e@talksasa.co.ke>", "fintech")

    excluded = quality.run()                              # shows only
    assert {l["id"]: why for l, why in excluded} == {event: "event", gov: "government body"}
    assert "Nothing was changed" in capsys.readouterr().out
    assert db.find_lead_by_email("info@fintechweek.co.ke")["status"] == "sent"

    quality.run(apply=True)
    with db.conn() as c:
        leads = {r["id"]: dict(r) for r in c.execute("SELECT * FROM leads")}
    assert leads[tagline]["status"] == "verified"           # names are names.py's job
    assert (leads[event]["status"], leads[event]["excluded_reason"]) == ("excluded", "event")
    assert leads[gov]["status"] == "excluded" and leads[good]["status"] == "verified"
    assert leads[lender]["status"] == "verified"
    # no first email and no follow-ups for an excluded lead
    assert event not in [l["id"] for l in db.awaiting_followup(1)]
    assert gov not in [l["id"] for l in ranking.waiting()]
    with pytest.raises(RuntimeError, match="is excluded"):
        sender.deliver(None, leads[gov], None, "fintech")


# --- Niche check before the first email ---

@pytest.fixture
def sending(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(nichefit, "site_niche", REAL_SITE_NICHE)
    db.init()
    texts, asked = {}, []
    monkeypatch.setattr(targeting, "site_text", lambda website: texts.get(website))

    def ask(prompt):
        asked.append(prompt)
        site = prompt.split("Website: ")[1].split("\n")[0]
        if site == "down.co.ke":
            raise RuntimeError("Groq is down")
        return json.dumps(answers[site])

    answers = {}
    monkeypatch.setattr(classifier, "ask_with_retry", ask)
    return texts, answers, asked


def planned():
    return {l["email"]: l["industry"] for l, followup in sender.plan() if not followup}


def test_a_tour_company_gets_no_tech_startup_email(sending):
    texts, answers, asked = sending
    tour = add("info@safaritours.co.ke", "Safari Tours", "tech_startup")
    add("info@realapp.co.ke", "Real App", "tech_startup")
    texts.update({"safaritours.co.ke": "Masai Mara safaris and beach holidays",
                  "realapp.co.ke": "Our app lets shops take orders by M-Pesa"})
    answers.update({"safaritours.co.ke": {"niche": "none", "reason": "a tour operator"},
                    "realapp.co.ke": {"niche": "tech_startup", "reason": "builds its own app"}})
    assert planned() == {"info@realapp.co.ke": "tech_startup"}
    assert "- tech_startup:" in asked[0] and "- college" not in asked[0]       # approved niches only
    with db.conn() as c:
        row = c.execute("SELECT niche_fit, niche_reason FROM leads WHERE id=?", (tour,)).fetchone()
    assert (row["niche_fit"], row["niche_reason"]) == ("none", "a tour operator")
    asked.clear()
    assert planned() == {"info@realapp.co.ke": "tech_startup"} and asked == []   # checked once
    with pytest.raises(RuntimeError, match="fits no approved niche"):
        sender.deliver(None, db.find_lead_by_email("info@safaritours.co.ke"), None, "tech_startup")


def test_a_lead_that_fits_another_approved_niche_moves_there(sending):
    texts, answers, _ = sending
    lead_id = add("info@schoolsys.co.ke", "School Sys", "tech_startup")
    texts["schoolsys.co.ke"] = "School management system used by 200 schools"
    answers["schoolsys.co.ke"] = {"niche": "software_vendor", "reason": "sells school software"}
    assert planned() == {"info@schoolsys.co.ke": "software_vendor"}
    with db.conn() as c:
        event = c.execute("SELECT detail FROM lead_events WHERE lead_id=? AND event_type='industry_changed'",
                          (lead_id,)).fetchone()
    assert json.loads(event["detail"])["old"] == "tech_startup"


def test_unreadable_site_or_ai_failure_waits_for_the_next_send(sending):
    texts, answers, _ = sending
    add("info@down.co.ke", "Down", "fintech")
    add("info@dark.co.ke", "Dark", "fintech")                  # its site can't be read
    texts["down.co.ke"] = "Payments"
    assert planned() == {}
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM leads WHERE niche_checked_at IS NOT NULL").fetchone()[0] == 0


def test_directory_leads_are_not_checked(sending):
    _, _, asked = sending
    lead_id = add("loans@gmail.com", "Quick Cash", "digital_lender", source="CBK DCP directory", website="")
    with db.conn() as c:
        c.execute("UPDATE leads SET imported_at=CURRENT_TIMESTAMP WHERE id=?", (lead_id,))
    assert planned() == {"loans@gmail.com": "digital_lender"} and asked == []
    assert "digital_lender" in nichefit.approved_niches() and "school" in personalise.USES
