"""Company names (names.py). A name from an official list or a list you gave is never replaced.
A web lead's name is picked from what its site calls itself (schema.org, og:site_name, footer,
logo; a title or headline only when short and matching the address) and the domain brand:
the candidate most like the domain brand, the AI choosing only among them when the site's
disagree. With nothing usable and no clear domain brand there is no name: "Hello," and no
name in the subject. Greetings and subjects use the short name, without legal endings.
Websites and the AI are fakes: nothing is fetched or asked, and nothing is sent."""
import json

import pytest
from bs4 import BeautifulSoup

import classifier
import config
import db
import names
import sender


def soup(html):
    return BeautifulSoup(html, "html.parser")


def pick(html, domain, **kw):
    return names.pick(soup(html) if html is not None else None, domain, **kw)


@pytest.fixture
def ai(monkeypatch):
    """The AI's answers, in order; every prompt it was asked."""
    answers, asked = [], []

    def ask(prompt):
        asked.append(prompt)
        return answers.pop(0)

    monkeypatch.setattr(classifier, "ask_with_retry", ask)
    return answers, asked


# --- Where names come from ---

def test_schema_org_name_comes_first():
    page = ('<script type="application/ld+json">{"@context": "https://schema.org", "@graph": ['
            '{"@type": "WebPage", "name": "Home"}, {"@type": "FinancialService", "name": "Zidi Capital Ltd"}]}'
            '</script><meta property="og:site_name" content="Zidi">')
    assert pick(page, "zidicapital.com") == ("Zidi Capital Ltd", "Zidi Capital", "schema.org")


@pytest.mark.parametrize("html, domain, expected", [
    ('<meta property="og:site_name" content="Noble Sacco Society Ltd">', "noblesacco.co.ke",
     ("Noble Sacco Society Ltd", "Noble Sacco", "og:site_name")),
    ("<footer>© 2025 Zidi Capital Limited. All rights reserved.</footer>", "zidicapital.com",
     ("Zidi Capital Limited", "Zidi Capital", "footer")),
    ('<header><a class="logo"><img src="/l.png" title="Pay Hero Kenya Limited"></a></header>', "payhero.co.ke",
     ("Pay Hero Kenya Limited", "Pay Hero", "logo")),
    ("<title>Mwanzo Credit</title>", "mwanzocredit.co.ke", ("Mwanzo Credit", "Mwanzo Credit", "title")),
    ("<h1>Elimika Sasa</h1>", "elimikasasa.co.ke", ("Elimika Sasa", "Elimika Sasa", "headline")),
])
def test_each_source(html, domain, expected):
    assert pick(html, domain) == expected


def test_a_title_or_headline_is_used_only_when_short_and_this_sites():
    # 5 words: too long for a title, so the domain brand
    assert pick("<title>Mwanzo Credit Loans In Minutes</title>", "mwanzocredit.co.ke").source == "domain"
    # short, but not this site's name
    assert pick("<h1>Quick Loans Today</h1>", "mwanzocredit.co.ke").source == "domain"


def test_taglines_long_names_and_others_names_are_rejected():
    page = ("<title>Credit Reference Bureau Services in Kenya | Home</title>"
            '<meta property="og:site_name" content="Your Trusted Partner for Credit Reports">'
            '<div class="clients-logos"><img src="/s.png" alt="Safaricom"></div>'
            "<footer>© 2026 Credit Reference Agency Group Holdings East Africa Limited</footer>")
    assert pick(page, "cragroup.co.ke") == ("CRA Group", "CRA Group", "domain")
    assert names.problem("Sacco Societies Regulatory Authority (SASRA)", "kenversitysacco.co.ke", "footer")
    assert names.problem("maseno-uni", "maseno.ac.ke", "og:site_name") == "written like an address"
    assert names.problem("Elimikasasa School Management System", "elimikasasa.co.ke", "og:site_name")


def test_the_domain_brand_is_spelled_as_the_site_writes_it():
    assert pick("<p>SeamlessIT builds payroll systems.</p>", "seamlessit.co.ke") == \
        ("SeamlessIT", "SeamlessIT", "domain")
    assert pick("<footer>© C3 Nairobi 2026</footer>", "c3nairobi.com").full == "C3 Nairobi"   # no year
    # the site's own spelling of the same name: no separate domain candidate
    assert [c.source for c in names.candidates(soup('<meta property="og:site_name" content="ElimuPoa">'),
                                               "elimupoa.com")] == ["og:site_name"]


def test_the_sites_own_name_beats_the_domain_brand():
    page = "<footer>© 2026 Sovereign Grace Church Nairobi</footer>"
    assert pick(page, "sgcnairobi.org") == ("Sovereign Grace Church Nairobi", "Sovereign Grace Church Nairobi", "footer")


def test_the_candidate_most_like_the_domain_brand_wins():
    page = ('<meta property="og:site_name" content="Zidi">'
            "<footer>© 2026 Zidi Capital Ltd</footer>")       # they agree: no AI needed
    assert pick(page, "zidicapital.com", use_ai=False) == ("Zidi Capital Ltd", "Zidi Capital", "footer")


# --- When the site's names disagree, the AI chooses one of them ---

DISAGREE = ('<meta property="og:site_name" content="Tenakata">'
            "<footer>© 2026 Tenakata Enterprises Ltd</footer>"
            '<header class="logo"><img src="/l.png" alt="Tena Pay"></header>'
            "<p>Tenakata Enterprises builds Tena Pay, a merchant app.</p>")


def test_the_ai_chooses_only_from_the_candidates(ai):
    answers, asked = ai
    answers.append(json.dumps({"choice": 2}))
    assert pick(DISAGREE, "tenakata.co.ke") == ("Tenakata Enterprises Ltd", "Tenakata Enterprises",
                                                "footer, chosen by AI")
    assert "1. Tenakata\n2. Tenakata Enterprises Ltd\n3. Tena Pay" in asked[0]
    assert "a merchant app" in asked[0]


@pytest.mark.parametrize("answer", ['{"choice": "Tenakata Group"}', '{"choice": 9}', "not json"])
def test_an_ai_answer_outside_the_list_is_ignored(ai, answer):
    ai[0].append(answer)
    assert pick(DISAGREE, "tenakata.co.ke").source in ("og:site_name", "footer", "logo", "domain")
    assert pick(DISAGREE, "tenakata.co.ke", use_ai=False).full in ("Tenakata", "Tenakata Enterprises Ltd", "Tena Pay")


# --- No confident name ---

def test_no_clear_name_means_hello_and_no_name_in_the_subject(tmp_path, monkeypatch):
    assert pick("<title>Best clinic in Nakuru for families</title>", "algadhirclinicnakuruhospital.com") == \
        (None, None, "none")
    assert pick(None, "cragroup.co.ke") == ("CRA Group", "CRA Group", "domain")

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    db.add_lead("algadhirclinicnakuruhospital.com", "algadhirclinicnakuruhospital.com", "healthtech",
                "info@algadhirclinicnakuruhospital.com", "search")
    lead_id = db.find_lead_by_email("info@algadhirclinicnakuruhospital.com")["id"]
    db.save_name(lead_id, None, None, "none")
    lead = db.find_lead_by_email("info@algadhirclinicnakuruhospital.com")
    assert sender.greeting(lead) == "Hello,"
    assert [sender.fill_subject(s, lead) for s in (
        "Fewer missed appointments at {company}", "Free SMS credits for {company}'s next project",
        "Reaching every {company} member", "Reaching every member of {company}", "SMS cost check for {company}")] == [
        "Fewer missed appointments", "Free SMS credits for your next project", "Reaching every member",
        "Reaching every member", "SMS cost check"]


# --- Short names ---

@pytest.mark.parametrize("full, short", [
    ("Novatok Credit Limited", "Novatok Credit"), ("Spectrum Credit Ltd", "Spectrum Credit"),
    ("Noble Sacco Society Ltd", "Noble Sacco"), ("Acme Kenya Limited", "Acme"), ("Avenews Ke Ltd", "Avenews"),
    ("Equity Group Holdings PLC", "Equity Group Holdings"), ("Zidi", "Zidi"),
    ("NAIROBI CONSUMERS SACCO SOCIETY LIMITED", "Nairobi Consumers Sacco"),
    # trading names, as the CBK directory writes them
    ("Inventure Mobile Limited (Trading as Tala)", "Tala"), ("Little Limited (trading as SpotIt)", "SpotIt"),
    ("Ambush Capital Limited trading under FlashPesa", "FlashPesa"),
    ("Nawiri African Sprouts Ltd trading as Fast Credit", "Fast Credit"),
])
def test_short_names_leave_out_legal_endings(full, short):
    assert names.display(full) == short


def test_greetings_and_subjects_use_the_short_name(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    db.add_lead("Spectrum Credit Ltd", None, "digital_lender", "loans@spectrum.co.ke", "CBK DCP directory Sep 2026")
    lead = db.find_lead_by_email("loans@spectrum.co.ke")
    assert sender.greeting(lead) == "Hello Spectrum Credit team,"
    assert sender.fill_subject("Loan reminders at {company}", lead) == "Loan reminders at Spectrum Credit"


# --- Existing leads ---

def add(email, company, source="search", website=None, status="verified"):
    db.add_lead(company, website if website is not None else email.split("@")[1], "fintech", email, source)
    lead_id = db.find_lead_by_email(email)["id"]
    db.set_status(lead_id, status, "test")
    return lead_id


def test_names_run_saves_waiting_leads_and_only_reports_sent_ones(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    sites = {"https://zidicapital.com": "<footer>© 2026 Zidi Capital Ltd</footer>",
             "https://cragroup.co.ke": "<title>Credit Reports | Best Credit Bureau in Kenya</title>",
             "https://flowcash.co.ke": '<meta property="og:site_name" content="FlowCash">'}
    fetched = []

    def fetch(url):
        fetched.append(url)
        return soup(sites[url]) if url in sites else None

    monkeypatch.setattr(names.scraper, "fetch", fetch)
    web = add("info@zidicapital.com", "Fast loans in minutes | Zidi")
    unread = add("info@seamlessit.co.ke", "SeamlessIT")      # site can't be read: domain brand, its spelling
    tagline = add("info@cragroup.co.ke", "Credit Reports | Best Credit Bureau in Kenya")
    cbk = add("loans@novatok.co.ke", "Novatok Credit Limited", source="CBK DCP directory Sep 2026")
    with db.conn() as c:
        c.execute("UPDATE leads SET imported_at=CURRENT_TIMESTAMP WHERE id=?", (cbk,))
    mine = add("hello@mine.co.ke", "My Own Name Ltd", source="csv")
    sent = add("info@flowcash.co.ke", "Flow Cash | Instant loans", status="sent")
    db.record_sent(sent, "<s@talksasa.co.ke>", "fintech")
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on='2026-11-02'")

    waiting, sent_rows = names.run(apply=True, day="2026-11-02")
    out = capsys.readouterr().out
    assert "Sent on 2026-11-02 (report only: never changed)" in out and "FlowCash" in out
    assert "https://novatok.co.ke" not in fetched and "https://mine.co.ke" not in fetched   # list names: not read
    with db.conn() as c:
        rows = {r["id"]: dict(r) for r in c.execute("SELECT * FROM leads")}
    assert (rows[web]["company"], rows[web]["display_name"], rows[web]["name_source"]) == \
        ("Zidi Capital Ltd", "Zidi Capital", "footer")
    assert (rows[tagline]["company"], rows[tagline]["name_source"]) == ("CRA Group", "domain")
    assert (rows[unread]["company"], rows[unread]["name_source"]) == ("SeamlessIT", "domain")
    assert (rows[cbk]["company"], rows[cbk]["display_name"]) == ("Novatok Credit Limited", "Novatok Credit")
    assert rows[cbk]["name_source"] == "CBK DCP directory Sep 2026"
    assert (rows[mine]["company"], rows[mine]["display_name"]) == ("My Own Name Ltd", "My Own Name")
    assert (rows[sent]["company"], rows[sent]["display_name"], rows[sent]["name_source"]) == \
        ("Flow Cash | Instant loans", None, None)                       # sent: report only
    assert [n for l, n in sent_rows] == [("FlowCash", "FlowCash", "og:site_name")]
