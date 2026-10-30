"""Automatic personalisation: who the first email goes to (contacts.py: a decision-maker
named on the lead's own site with their own published address, else the best role address),
and the AI opener, which must name the exact sentence on the site it comes from and pass a
check in code and a second AI check. Fake websites and a fake AI: nothing real is read or sent."""
import json

import pytest
from bs4 import BeautifulSoup

import config
import contacts
import crm
import db
import personalise
import ranking
import sender

SITE = "https://umojacredit.co.ke"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    monkeypatch.setattr(sender.notify, "send", lambda text: None)
    monkeypatch.setattr(contacts.time, "sleep", lambda s: None)
    db.init()


def website(monkeypatch, pages):
    """A fake website: {path: html}. Any other address can't be read."""
    def fetch(url):
        path = url.removeprefix(SITE).rstrip("/") or ""
        html = pages.get(path)
        return BeautifulSoup(html, "html.parser") if html is not None else None
    monkeypatch.setattr(contacts, "fetch", fetch)


HOME = '<html><title>Umoja Credit</title><a href="/team">Our team</a><p>Loans for traders.</p></html>'
CARD = '<div class="card"><h3>{name}</h3><p>{title}</p>{email}</div>'


def team(*cards):
    return "<html><title>Our team | Umoja Credit</title><h1>Meet the team</h1>" + "".join(
        CARD.format(name=n, title=t, email=f'<a href="mailto:{e}">{e}</a>' if e else "") for n, t, e in cards) + "</html>"


# --- Which address ---

def test_role_addresses_in_order_on_the_same_domain(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>info@umojacredit.co.ke contact@umojacredit.co.ke "
                                               "hello@umojacredit.co.ke ops@umojacredit.co.ke "
                                               "sales@gmail.com business@umojacredit.co.ke</p>"})
    found = contacts.find(SITE, "Umoja Credit")
    assert found["role"] == "business@umojacredit.co.ke" and found["contact"] is None
    assert "sales@gmail.com" not in found["emails"]  # not their own domain


def test_sales_comes_first(world, monkeypatch):
    website(monkeypatch, {"": HOME + "<p>info@umojacredit.co.ke</p>", "/contact": "<p>sales@umojacredit.co.ke</p>"})
    assert contacts.find(SITE)["role"] == "sales@umojacredit.co.ke"


def test_a_named_decision_maker_with_their_own_address(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>sales@umojacredit.co.ke</p>",
                          "/team": team(("Jane Wanjiru", "Chief Executive Officer", "jane@umojacredit.co.ke"),
                                        ("Peter Otieno", "Customer Support Officer", "peter@umojacredit.co.ke"))})
    found = contacts.find(SITE, "Umoja Credit")
    assert found["contact"] == {"name": "Jane Wanjiru", "title": "Chief Executive Officer",
                                "email": "jane@umojacredit.co.ke", "url": f"{SITE}/team"}


def test_the_highest_title_wins(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/team": team(("Ann Muthoni", "Head of Marketing", "ann@umojacredit.co.ke"),
                                                  ("David Kamau", "Co-founder & CTO", "david.kamau@umojacredit.co.ke"))})
    assert contacts.find(SITE)["contact"]["name"] == "David Kamau"


def test_address_elsewhere_on_the_site_counts_only_if_plainly_theirs(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/team": team(("Jane Wanjiru", "CEO", None)),
                          "/contact": "<p>jwanjiru@umojacredit.co.ke</p>"})
    assert contacts.find(SITE)["contact"]["email"] == "jwanjiru@umojacredit.co.ke"
    website(monkeypatch, {"": HOME, "/team": team(("Jane Wanjiru", "CEO", None)),
                          "/contact": "<p>jane@umojacredit.co.ke info@umojacredit.co.ke</p>"})
    found = contacts.find(SITE)  # "jane@" away from her card could be another Jane
    assert found["contact"] is None and found["role"] == "info@umojacredit.co.ke"


def test_an_address_is_never_made_up(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/team": team(("Jane Wanjiru", "Founder", None)),
                          "/contact": "<p>info@umojacredit.co.ke</p>"})
    found = contacts.find(SITE)
    assert found["contact"] is None
    assert found["emails"] == ["info@umojacredit.co.ke"]  # only what the site shows


def test_a_role_address_on_a_card_is_not_a_person(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/team": team(("Jane Wanjiru", "CEO", "info@umojacredit.co.ke"))})
    assert contacts.find(SITE)["contact"] is None


def test_names_count_only_on_a_team_about_or_leadership_page(world, monkeypatch):
    website(monkeypatch, {"": "<html><title>Umoja</title>" + CARD.format(
        name="Jane Wanjiru", title="CEO", email='<a href="mailto:jane@umojacredit.co.ke">x</a>') + "</html>"})
    assert contacts.find(SITE)["contact"] is None


def test_cloudflare_protected_addresses_are_read(world, monkeypatch):
    hidden = "".join(f"{ord(ch) ^ 0x2a:02x}" for ch in "sales@umojacredit.co.ke")
    website(monkeypatch, {"": HOME + f'<a data-cfemail="2a{hidden}">[email protected]</a>'})
    assert contacts.find(SITE)["role"] == "sales@umojacredit.co.ke"


def test_addresses_are_tidied_and_listed_once(world, monkeypatch):
    website(monkeypatch, {"": HOME + '<a href="mailto: support@umojacredit.co.ke">x</a>'
                              '<a href="mailto:%20Support@umojacredit.co.ke?subject=hi">y</a>'
                              "<p>support@umojacredit.co.ke.</p>"})
    assert contacts.find(SITE)["emails"] == ["support@umojacredit.co.ke"]


# --- Saving it on the lead, and the email ---

def add(email="info@umojacredit.co.ke", industry="digital_lender"):
    db.add_lead("Umoja Credit", "umojacredit.co.ke", industry, email, "search")
    lead_id = db.find_lead_by_email(email)["id"]
    db.set_status(lead_id, "verified", "verify")
    return lead_id


def row(lead_id):
    with db.conn() as c:
        return c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def test_run_saves_the_contact_and_switches_the_address(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/team": team(("Jane Wanjiru", "CEO", "jane@umojacredit.co.ke"))})
    lead_id = add()
    assert contacts.run([row(lead_id)]) == 1
    lead = row(lead_id)
    assert (lead["email"], lead["source_email"], lead["contact_name"], lead["contact_title"]) == \
        ("jane@umojacredit.co.ke", "info@umojacredit.co.ke", "Jane Wanjiru", "CEO")
    assert lead["contact_url"] == f"{SITE}/team" and lead["contact_checked_at"]
    assert contacts.run([row(lead_id)]) == 0  # read once only
    msg, _ = sender.build(row(lead_id))
    assert msg["To"] == "jane@umojacredit.co.ke" and sender.plain_of(msg).startswith("Hello Jane,\n\n")
    assert sender.plain_of(sender.build_followup(dict(row(lead_id), template_variant="A"), 1)[0]).startswith("Hello Jane,")


def test_a_lead_already_emailed_keeps_its_address(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>sales@umojacredit.co.ke</p>"})
    lead_id = add()
    db.record_sent(lead_id, "<1@x>", "t")
    contacts.run([row(lead_id)])
    assert row(lead_id)["email"] == "info@umojacredit.co.ke" and row(lead_id)["contact_checked_at"] is None


def test_an_opted_out_address_is_not_switched_to(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>sales@umojacredit.co.ke</p>"})
    db.unsubscribe("sales@umojacredit.co.ke")
    lead_id = add()
    contacts.run([row(lead_id)])
    assert row(lead_id)["email"] == "info@umojacredit.co.ke"


def test_an_unreadable_site_leaves_the_lead_as_it_is(world):
    lead_id = add()
    contacts.run([row(lead_id)])
    lead = row(lead_id)
    assert lead["email"] == "info@umojacredit.co.ke" and lead["contact_name"] is None


def test_greeting_without_a_contact_is_the_team(world):
    msg, _ = sender.build(row(add()))
    assert sender.plain_of(msg).startswith("Hello Umoja Credit team,\n\n")


# --- The opener ---

PAGE = ("Umoja Credit offers mobile loans to small traders in Kisumu and Nakuru. "
        "Apply in minutes from your phone and get an answer the same day.")
SOURCE = "Umoja Credit offers mobile loans to small traders in Kisumu and Nakuru."
GOOD = "I read that you serve traders with mobile credit in Kisumu and Nakuru."


@pytest.fixture
def ai(monkeypatch):
    """The fake AI: what the writer (WRITER_MODEL) and the checker answer, and every prompt."""
    replies = {"opener": GOOD, "source": SOURCE, "url": f"{SITE}/about", "supported": True, "prompts": []}
    monkeypatch.setattr(config, "USE_AI_OPENER", True)
    monkeypatch.setattr(personalise.scraper, "site_sources", lambda w: [(SITE, "Umoja Credit. Loans. " * 20),
                                                                        (f"{SITE}/about", PAGE * 2)])
    monkeypatch.setattr(personalise.scraper, "site_taglines", lambda w: [])

    def ask(prompt, groq_model=None):
        replies["prompts"].append((groq_model, prompt))
        if groq_model == config.WRITER_MODEL:
            return json.dumps({"opener": replies["opener"], "source_sentence": replies["source"],
                               "source_url": replies["url"], "uses": ["OTPs", "loan repayment reminders"]})
        return json.dumps({"supported": replies["supported"], "problem": "" if replies["supported"] else "made up"})

    monkeypatch.setattr(personalise.classifier, "ask", ask)
    return replies


def lead_for_opener():
    return {"id": 1, "company": "Umoja Credit", "industry": "digital_lender", "website": SITE,
            "email": "info@umojacredit.co.ke"}


def test_opener_with_its_source_after_both_checks(world, ai):
    got = personalise.generate(lead_for_opener())
    assert (got["opener"], got["source"], got["url"], got["note"]) == (GOOD, SOURCE, f"{SITE}/about", None)
    writer, checker = ai["prompts"]
    assert writer[0] == config.WRITER_MODEL and "25 words" in writer[1] and f"=== PAGE: {SITE}/about ===" in writer[1]
    assert "an opener that only says where they are is not allowed" in writer[1]
    assert checker[0] is None and GOOD in checker[1] and SOURCE in checker[1]  # GROQ_MODEL, a second opinion


@pytest.mark.parametrize("change, note", [
    ({"source": "Umoja Credit gives loans to everyone in Kenya."}, "isn't on that page"),
    ({"url": "https://elsewhere.example/page", "source": "This sentence is on no page of theirs at all."},
     "isn't one we read"),
    ({"opener": "I read that you serve 5,000 traders with mobile credit in Kisumu."}, 'number that isn\'t on the page ("5,000")'),
    ({"opener": "I read that you serve traders with mobile credit in Kisumu, Nakuru and Eldoret."}, '("Eldoret")'),
    ({"supported": False}, "AI check failed: made up"),
    ({"opener": ""}, "no clear, specific fact"),
])
def test_any_failed_check_means_no_opener(world, ai, change, note):
    ai.update(change)
    got = personalise.generate(lead_for_opener())
    assert got["opener"] is None and got["source"] is None and note in got["note"]


def test_a_sentence_on_another_page_we_read_gets_that_page(world, ai):
    ai["url"] = SITE  # the AI named the homepage; the sentence is on the about page
    got = personalise.generate(lead_for_opener())
    assert got["opener"] == GOOD and got["url"] == f"{SITE}/about"


def test_a_failed_check_sends_the_normal_template(world, ai, monkeypatch):
    ai["supported"] = False
    lead_id = add()
    with db.conn() as c:
        c.execute("UPDATE leads SET website=? WHERE id=?", (SITE, lead_id))
    personalise.run([row(lead_id)])
    lead = row(lead_id)
    assert lead["opener"] is None and lead["confidence"] == 0 and "AI check failed" in lead["opener_note"]
    text = sender.plain_of(sender.build(row(lead_id))[0])
    assert text.startswith("Hello Umoja Credit team,\n\n") and GOOD not in text


def test_opener_is_the_first_line_after_the_greeting_and_only_in_the_first_email(world, ai):
    lead_id = add()
    with db.conn() as c:
        c.execute("UPDATE leads SET website=? WHERE id=?", (SITE, lead_id))
    personalise.run([row(lead_id)])
    lead = row(lead_id)
    assert (lead["opener"], lead["opener_source"], lead["opener_url"]) == (GOOD, SOURCE, f"{SITE}/about")
    for v in "ABC":
        text = sender.plain_of(sender.build(dict(lead, template_variant=v))[0])
        assert text.startswith(f"Hello Umoja Credit team,\n\n{GOOD}\n\n")
        for n in (1, 2):
            assert GOOD not in sender.plain_of(sender.build_followup(dict(lead, template_variant=v), n)[0])


def test_an_opener_without_a_source_is_never_used(world, monkeypatch):
    monkeypatch.setattr(config, "USE_AI_OPENER", True)
    lead = {"opener": "Old opener.", "confidence": 1, "opener_source": None}
    assert personalise.opener(lead) == ""
    assert personalise.opener(dict(lead, opener_source="x")) == "Old opener."


def test_every_first_email_has_greeting_then_opener():
    for niche in ("sacco", "school", "church", "fintech", "digital_lender", "hr_payroll", "saas", "tech_startup"):
        for v in "ABC":
            body = sender.load_template(niche, v)[2]
            assert body.startswith("{greeting}\n\n{opener}\n\n")
            assert sender.load_template(niche, v)[2].count("{opener}") == 1


# --- What went out, the report and the lead page ---

def test_each_email_records_opener_and_contact(world, monkeypatch, smtp, ai):
    monkeypatch.setattr(personalise, "run", lambda leads: 0)
    website(monkeypatch, {"": HOME, "/team": team(("Jane Wanjiru", "CEO", "jane@umojacredit.co.ke"))})
    lead_id = add()
    with db.conn() as c:
        c.execute("UPDATE leads SET website=?, opener=?, confidence=1, opener_source=?, opener_url=? WHERE id=?",
                  (SITE, GOOD, SOURCE, f"{SITE}/about", lead_id))
    sender.run()
    assert smtp["delivered"] == ["jane@umojacredit.co.ke"]
    with db.conn() as c:
        assert tuple(c.execute("SELECT with_opener, to_contact FROM sent").fetchone()) == (1, 1)


def test_report_compares_opener_and_contact(world):
    crm.ensure_schema()
    for i, (opener, contact, status) in enumerate([(1, 1, "replied"), (0, 0, "sent"), (1, 0, "meeting"), (None, None, "sent")]):
        lead_id = add(f"info{i}@x{i}.co.ke")
        db.record_sent(lead_id, f"<{i}@x>", "t", with_opener=opener, to_contact=contact)
        db.set_status(lead_id, status, "test")
        if status == "replied":
            db.add_reply(lead_id, f"info{i}@x{i}.co.ke", "Re: hi", "Tell me more", "question", "", f"uid{i}")
    r = crm.report(0, {"id": 1, "role": "admin"})
    opener = {g["name"]: g for g in r["groups"]["opener"]}
    contact = {g["name"]: g for g in r["groups"]["contact"]}
    assert list(opener) == ["With an opener", "Without an opener"] and list(contact) == ["Named contact", "Role address"]
    assert (opener["With an opener"]["emailed"], opener["With an opener"]["replied"],
            opener["With an opener"]["meetings"]) == (2, 1, 1)
    assert contact["Named contact"]["emailed"] == 1 and contact["Role address"]["emailed"] == 2
    assert r["total"]["emailed"] == 4  # the one from before is only left out of these two


def test_lead_page_shows_contact_opener_and_source(world):
    crm.ensure_schema()
    lead_id = add("jane@umojacredit.co.ke")
    with db.conn() as c:
        c.execute("UPDATE leads SET contact_name='Jane Wanjiru', contact_title='CEO', contact_email=?,"
                  " contact_url=?, contact_checked_at=CURRENT_TIMESTAMP, opener=?, confidence=1,"
                  " opener_source=?, opener_url=?, source_email='info@umojacredit.co.ke' WHERE id=?",
                  ("jane@umojacredit.co.ke", f"{SITE}/team", GOOD, SOURCE, f"{SITE}/about", lead_id))
    db.record_sent(lead_id, "<1@x>", "t", with_opener=1, to_contact=1)
    for role in ("admin", "sales"):
        p = crm.lead_detail(lead_id, {"id": 1, "role": role})["lead"]["personal"]
        assert p["contact"] == {"name": "Jane Wanjiru", "title": "CEO", "email": "jane@umojacredit.co.ke",
                                "url": f"{SITE}/team"} and p["to_contact"]
        assert (p["opener"], p["opener_source"], p["opener_url"]) == (GOOD, SOURCE, f"{SITE}/about")
        assert p["previous_email"] == "info@umojacredit.co.ke"
        assert p["first_email"] == {"with_opener": 1, "to_contact": 1}
