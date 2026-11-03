"""Company names (names.py). A name from an official list or a list you gave is never replaced.
A web lead's name comes from what its site calls itself (og:site_name, logo, copyright line,
footer, schema.org), never its page title; a name that reads like a search phrase ("Ecommerce
Agency in Nairobi Kenya"), has more than 5 words or shares no word with the address is dropped,
and with nothing left the name is built from the domain ("garaconsultancy.co.ke" -> "Gara
Consultancy"). Names are checked at discovery, by the bot's prepare step in batches, and just
before every email; a follow-up keeps its thread's subject but uses the corrected name.
Websites, the AI and the mail server are fakes: nothing is fetched, asked or sent."""
import json
from datetime import datetime

import pytest
from bs4 import BeautifulSoup

import auto
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


class Sites(dict):
    """Fake websites: {"https://domain": html}. Every address read is in .fetched."""
    def __init__(self):
        super().__init__()
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        return soup(self[url]) if url in self else None


@pytest.fixture
def sites(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    web = Sites()
    monkeypatch.setattr(names.scraper, "fetch", web.fetch)
    return web


# --- The reported case: an SEO title is never a name ---

SEO_PAGE = ("<title>Ecommerce Agency in Nairobi Kenya</title><h1>Ecommerce Agency in Nairobi Kenya</h1>"
            '<meta property="og:site_name" content="Ecommerce Agency in Nairobi Kenya">')


def test_an_seo_title_is_never_the_name():
    assert pick(SEO_PAGE + "<footer>© 2025 Gara Consultancy</footer>", "garaconsultancy.co.ke") == \
        ("Gara Consultancy", "Gara Consultancy", "copyright")
    # nothing usable on the site: built from the domain, its joined words split
    assert pick(SEO_PAGE, "garaconsultancy.co.ke") == ("Gara Consultancy", "Gara Consultancy", "domain")


@pytest.mark.parametrize("name", [
    "Ecommerce Agency in Nairobi Kenya", "Gara Consultancy Kenya", "Gara Consultancy Nairobi",
    "Best Gara Consultancy", "Top Gara Consultancy", "Gara Consultancy Services", "Gara agency in town",
    "Gara company in town", "Gara Consultancy near me", "Gara | Consultancy", "Gara - Consultancy",
    "Gara Consultancy and Digital Marketing Agency Group",       # more than 5 words
    "Digital Growth Partners",                                    # no word shared with the address
])
def test_search_phrases_long_names_and_others_names_are_rejected(name):
    assert names.problem(name, "garaconsultancy.co.ke", "og:site_name")


def test_legal_endings_are_not_place_words():
    assert names.problem("Gara Consultancy Kenya Limited", "garaconsultancy.co.ke", "copyright") is None
    assert names.display("Gara Consultancy Kenya Limited") == "Gara Consultancy"


# --- Where names come from ---

@pytest.mark.parametrize("html, domain, expected", [
    ('<meta property="og:site_name" content="Noble Sacco Society Ltd">', "noblesacco.co.ke",
     ("Noble Sacco Society Ltd", "Noble Sacco", "og:site_name")),
    ('<header><a class="logo"><img src="/l.png" title="Pay Hero Kenya Limited"></a></header>', "payhero.co.ke",
     ("Pay Hero Kenya Limited", "Pay Hero", "logo")),
    ("<footer>© 2025 Zidi Capital Limited. All rights reserved.</footer>", "zidicapital.com",
     ("Zidi Capital Limited", "Zidi Capital", "copyright")),
    ("<footer><h4>Zidi Capital</h4><p>Loans in minutes</p></footer>", "zidicapital.com",
     ("Zidi Capital", "Zidi Capital", "footer")),
    ('<script type="application/ld+json">{"@type": "FinancialService", "name": "Zidi Capital Ltd"}</script>',
     "zidicapital.com", ("Zidi Capital Ltd", "Zidi Capital", "schema.org")),
])
def test_each_source(html, domain, expected):
    assert pick(html, domain) == expected


def test_titles_and_headings_are_never_used():
    assert pick("<title>Mwanzo Credit</title><h1>Mwanzo Credit</h1>", "mwanzocredit.co.ke").source == "domain"


def test_the_sites_own_name_beats_the_domain_brand():
    page = "<footer>© 2026 Sovereign Grace Church</footer>"
    assert pick(page, "sovereigngrace.org") == ("Sovereign Grace Church", "Sovereign Grace Church", "copyright")


def test_the_candidate_most_like_the_domain_brand_wins():
    page = ('<meta property="og:site_name" content="Zidi">'
            "<footer>© 2026 Zidi Capital Ltd</footer>")       # they agree: no AI needed
    assert pick(page, "zidicapital.com", use_ai=False) == ("Zidi Capital Ltd", "Zidi Capital", "copyright")


@pytest.mark.parametrize("domain, brand", [
    ("garaconsultancy.co.ke", "Gara Consultancy"), ("cragroup.co.ke", "CRA Group"),
    ("noblesaccosociety.co.ke", "Noble Sacco Society"), ("sendy.co.ke", "Sendy"),
    ("2nksacco.co.ke", "2NK Sacco"), ("pay-hero.co.ke", "Pay Hero"), ("powerup.works", "Powerup"),
])
def test_domain_brands(domain, brand):
    assert names.domain_brand(domain) == brand


def test_the_domain_brand_is_spelled_as_the_site_writes_it():
    assert pick("<p>SeamlessIT builds payroll systems.</p>", "seamlessit.co.ke") == \
        ("SeamlessIT", "SeamlessIT", "domain")


# --- When the site's names disagree, the AI chooses one of them ---

DISAGREE = ('<meta property="og:site_name" content="Tenakata">'
            "<footer>© 2026 Tenakata Enterprises Ltd</footer>"
            '<header class="logo"><img src="/l.png" alt="Tena Pay"></header>'
            "<p>Tenakata Enterprises builds Tena Pay, a merchant app.</p>")


def test_the_ai_chooses_only_from_the_candidates(ai):
    answers, asked = ai
    answers.append(json.dumps({"choice": 3}))
    assert pick(DISAGREE, "tenakata.co.ke") == ("Tenakata Enterprises Ltd", "Tenakata Enterprises",
                                                "copyright, chosen by AI")
    assert "1. Tenakata\n2. Tena Pay\n3. Tenakata Enterprises Ltd" in asked[0]


@pytest.mark.parametrize("answer", ['{"choice": "Tenakata Group"}', '{"choice": 9}', "not json"])
def test_an_ai_answer_outside_the_list_is_ignored(ai, answer):
    ai[0].append(answer)
    assert pick(DISAGREE, "tenakata.co.ke").full in ("Tenakata", "Tenakata Enterprises Ltd", "Tena Pay")


# --- No confident name ---

def test_no_clear_name_means_hello_and_no_name_in_the_subject(sites):
    assert pick(SEO_PAGE, "qwrtzxvbnmplkjhg.com") == (None, None, "none")
    lead_id = add("info@qwrtzxvbnmplkjhg.com", "qwrtzxvbnmplkjhg.com")
    db.save_name(lead_id, None, None, "none")
    lead = db.find_lead_by_email("info@qwrtzxvbnmplkjhg.com")
    assert sender.greeting(lead) == "Hello,"
    assert [sender.fill_subject(s, lead) for s in (
        "Fewer missed appointments at {company}", "Free SMS credits for {company}'s next project",
        "Reaching every {company} member", "SMS cost check for {company}")] == [
        "Fewer missed appointments", "Free SMS credits for your next project", "Reaching every member",
        "SMS cost check"]


# --- Short names ---

@pytest.mark.parametrize("full, short", [
    ("Novatok Credit Limited", "Novatok Credit"), ("Noble Sacco Society Ltd", "Noble Sacco"),
    ("Acme Kenya Limited", "Acme"), ("Avenews Ke Ltd", "Avenews"),
    ("Inventure Mobile Limited (Trading as Tala)", "Tala"),
    ("Ambush Capital Limited trading under FlashPesa", "FlashPesa"),
])
def test_short_names_leave_out_legal_endings(full, short):
    assert names.display(full) == short


# --- Leads: just before every email, and the prepare step ---

def add(email, company, source="search", website=None, status="verified", industry="fintech"):
    db.add_lead(company, website if website is not None else email.split("@")[1], industry, email, source)
    lead_id = db.find_lead_by_email(email)["id"]
    db.set_status(lead_id, status, "test")
    return lead_id


def row(lead_id):
    with db.conn() as c:
        return dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


class FakeSMTP:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def sending(sites, monkeypatch):
    monkeypatch.setattr(names, "ensure", names.REAL_ENSURE)
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    return sites


def test_the_name_is_corrected_just_before_a_first_email(sending):
    sending["https://garaconsultancy.co.ke"] = SEO_PAGE + "<footer>© 2025 Gara Consultancy</footer>"
    lead_id = add("info@garaconsultancy.co.ke", "Ecommerce Agency in Nairobi Kenya")
    smtp = FakeSMTP()
    assert sender.send_one(smtp, db.find_lead_by_email("info@garaconsultancy.co.ke")) == "sent"
    body = sender.plain_of(smtp.sent[0])
    assert "Ecommerce Agency" not in body + str(smtp.sent[0]["Subject"])
    assert "Hello Gara Consultancy team," in body
    assert (row(lead_id)["company"], row(lead_id)["name_source"]) == ("Gara Consultancy", "copyright")


def test_a_follow_up_keeps_the_thread_subject_but_uses_the_corrected_name(sending):
    lead_id = add("info@garaconsultancy.co.ke", "Ecommerce Agency in Nairobi Kenya", status="sent")
    with db.conn() as c:
        c.execute("UPDATE leads SET template_variant='A' WHERE id=?", (lead_id,))
    db.record_sent(lead_id, "<first@talksasa.co.ke>", "fintech_A", "When your SMS provider goes down",
                   "Hello Ecommerce Agency in Nairobi Kenya team, ...", "template", "A")
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on = date('now', '-20 days')")
    # the site can't be read now: the bad name gives way to the domain's
    smtp = FakeSMTP()
    assert sender.send_one(smtp, db.awaiting_followup(1)[0], followup=1) == "sent"
    msg = smtp.sent[0]
    assert str(msg["Subject"]) == "Re: When your SMS provider goes down"
    assert msg["In-Reply-To"] == "<first@talksasa.co.ke>"
    assert sender.plain_of(msg).startswith("Hello Gara Consultancy team,")
    assert row(lead_id)["name_source"] == "domain"


def test_a_good_checked_name_is_not_read_again(sending):
    lead_id = add("info@zidicapital.com", "Zidi Capital Ltd")
    db.save_name(lead_id, "Zidi Capital Ltd", "Zidi Capital", "copyright", version=names.NAME_VERSION)
    assert names.ensure(row(lead_id))["display_name"] == "Zidi Capital"
    assert sending.fetched == []


def test_list_names_are_never_replaced(sending):
    cbk = add("loans@gmail.com", "Quick Cash Kenya Limited", source="CBK DCP directory Sep 2026", website="")
    mine = add("hello@mine.co.ke", "Best Services in Nairobi", source="csv")
    assert names.ensure(row(cbk))["company"] == "Quick Cash Kenya Limited"
    assert names.ensure(row(mine))["company"] == "Best Services in Nairobi"
    assert (row(cbk)["display_name"], row(mine)["name_source"]) == ("Quick Cash", "csv")
    assert sending.fetched == []


def test_prepare_checks_waiting_and_emailed_leads_in_batches(sites):
    sites["https://garaconsultancy.co.ke"] = "<footer>© 2025 Gara Consultancy</footer>"
    bad = add("info@garaconsultancy.co.ke", "Ecommerce Agency in Nairobi Kenya", status="sent")
    good = add("info@paywaveexpress.co.ke", "Paywave Express")                 # site unreadable: kept
    seo = add("info@cragroup.co.ke", "Credit Reports | Best Credit Bureau in Kenya")
    cbk = add("loans@novatok.co.ke", "Novatok Credit Limited", source="CBK DCP directory Sep 2026")
    bounced = add("info@gone.co.ke", "Gone | Home", status="bounced")         # never emailed again: left
    assert names.prepare(batch=2) == (3, 1)          # the list lead, then 2 web leads
    assert names.prepare(batch=2) == (1, 0)
    assert names.prepare(batch=2) == (0, 0)
    assert row(bad)["company"] == "Gara Consultancy"
    assert (row(good)["company"], row(good)["name_source"]) == ("Paywave Express", "current name, checked")
    assert (row(seo)["company"], row(seo)["name_source"]) == ("CRA Group", "domain")
    assert (row(cbk)["company"], row(cbk)["display_name"]) == ("Novatok Credit Limited", "Novatok Credit")
    assert row(bounced)["name_version"] is None


def test_the_bot_prepares_names_before_sending(monkeypatch):
    calls, steps, left = [], [], [45]

    def prepare(batch=names.BATCH):
        calls.append(batch)
        left[0] = max(0, left[0] - batch)
        return batch, left[0]

    monkeypatch.setattr(names, "prepare", prepare)
    assert auto._prepare() == "checked the names of 60 leads, 0 still to check"
    assert len(calls) == 3
    monkeypatch.setattr(auto, "MARKER", type("Marker", (), {"write_text": lambda self, text: None})())
    monkeypatch.setattr(auto, "step", lambda name, fn: steps.append(name))
    auto.daily_run(datetime(2026, 11, 3, 9))
    assert steps == ["discover", "verify", "personalise", "prepare", "send"]


def test_report_of_the_most_recently_emailed(sites, capsys):
    first = add("info@garaconsultancy.co.ke", "Ecommerce Agency in Nairobi Kenya", status="sent")
    second = add("info@zidicapital.com", "Zidi Capital Ltd", status="sent")
    db.record_sent(first, "<a@talksasa.co.ke>", "fintech")
    db.record_sent(second, "<b@talksasa.co.ke>", "fintech")
    assert [l["id"] for l in names.recently_emailed(30)] == [second, first]
    names.run(recent=30)
    out = capsys.readouterr().out
    assert "The 30 leads emailed last" in out and "Gara Consultancy" in out and "Nothing was changed" in out
    assert row(first)["company"] == "Ecommerce Agency in Nairobi Kenya"     # a report changes nothing
