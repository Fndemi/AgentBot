"""Fewer bounces from the addresses discovery picks (scraper.py), and old leads' addresses
checked again before their first email (contacts.recheck_addresses).

- Only addresses on the site's own domain or its subdomains: never another domain, so a site
  that gives none of its own adds no lead.
- No placeholders (yourdomain, company.com, name@, john@, test@, example) and no web designer's
  credit ("designed by", "website by", "developed by", "powered by").
- pick_best: PREFERRED, then ROLE_ORDER, not the alphabet.
- A lead found more than 60 days ago whose address came from its website has the site read again;
  an address no longer there means no email. Websites are fakes: nothing is fetched."""
from datetime import date, timedelta

import pytest
from bs4 import BeautifulSoup

import config
import contacts
import db
import discover
import nichefit
import prepare
import scraper
import sender
import targeting

REAL_PREPARED = sender.prepared  # conftest makes every lead count as prepared; these tests need the rule


def soup(html):
    return BeautifulSoup(html, "html.parser")


# --- Own domain only ---

def test_only_the_sites_own_domain_and_subdomains_are_kept():
    found = {"info@acme.co.ke", "Sales@Mail.Acme.co.ke", "acme@gmail.com", "hello@partner.com",
             "info@notacme.co.ke", "ops@acme.co.ke."}
    assert scraper.clean(found, "acme.co.ke") == {"info@acme.co.ke", "sales@mail.acme.co.ke", "ops@acme.co.ke"}
    assert scraper.clean(found, "www.acme.co.ke") == scraper.clean(found, "acme.co.ke")


def test_no_fallback_to_another_domain():
    assert scraper.clean({"acme@gmail.com", "info@webagency.co.ke"}, "acme.co.ke") == set()


def test_a_site_with_no_address_of_its_own_adds_no_lead(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    pages = {"https://acme.co.ke": "<p>Write to acmeltd@gmail.com</p>",
             "https://acme.co.ke/contact": "<a href='mailto:info@webagency.co.ke'>Our agency</a>"}
    monkeypatch.setattr(scraper, "fetch", lambda url: soup(pages[url.rstrip("/")]) if url.rstrip("/") in pages else None)
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    monkeypatch.setattr(discover, "look_at", lambda url: discover.Page("Acme", True, "Acme | Loans", None, "Acme"))
    monkeypatch.setattr(discover, "save_csv", lambda row: None)
    assert discover.consider("https://acme.co.ke/", "fintech", "fintech Kenya", set()) is None
    assert db.find_lead_by_email("acmeltd@gmail.com") is None


# --- Placeholders and credits ---

@pytest.mark.parametrize("address", ["info@yourdomain.co.ke", "hello@company.com", "name@acme.co.ke",
                                     "john@acme.co.ke", "test@acme.co.ke", "info@example.co.ke",
                                     "example@acme.co.ke", "you@yourdomain.com"])
def test_placeholders_are_dropped(address):
    assert scraper.clean({address, "info@acme.co.ke"}, address.split("@")[1]) - {"info@acme.co.ke"} == set()


@pytest.mark.parametrize("address", ["johnson@acme.co.ke", "testimonials@acme.co.ke", "names@acme.co.ke"])
def test_real_addresses_that_only_start_like_a_placeholder_are_kept(address):
    assert scraper.clean({address}, "acme.co.ke") == {address}


@pytest.mark.parametrize("phrase", ["Designed by", "Website by", "Developed by", "Powered by", "website BY"])
def test_an_address_next_to_a_designers_credit_is_dropped(phrase):
    page = soup(f"<footer><p>Contact: info@acme.co.ke</p><p>{phrase} Pixel Studio - studio@acme.co.ke</p></footer>")
    assert scraper.page_emails(page) == {"info@acme.co.ke"}


def test_a_mailto_credit_link_is_dropped_however_it_is_written():
    page = soup("<p>Site <b>developed by</b> <a href='mailto:dev@acme.co.ke'>Code House</a></p>"
                "<p><a href='mailto:sales@acme.co.ke'>Email sales</a></p>")
    assert scraper.page_emails(page) == {"sales@acme.co.ke"}


def test_a_credit_far_from_the_address_drops_nothing():
    filler = "We serve SACCOs, schools and hospitals across Kenya with loans and savings. " * 2
    page = soup(f"<div>Payments powered by M-Pesa. {filler} Write to info@acme.co.ke</div>")
    assert scraper.page_emails(page) == {"info@acme.co.ke"}


def test_an_address_in_a_credit_is_dropped_even_where_it_appears_again():
    page = soup("<p>Website by Acme Web, web@acme.co.ke</p><p>Questions? web@acme.co.ke</p>")
    assert scraper.page_emails(page) == set()


# --- Picking one ---

def test_preferred_inboxes_come_first():
    assert scraper.pick_best({"ops@acme.co.ke", "sales@acme.co.ke", "info@acme.co.ke"}) == "info@acme.co.ke"


def test_then_role_order_not_the_alphabet():
    assert scraper.pick_best({"tech@acme.co.ke", "partnerships@acme.co.ke", "business@acme.co.ke"}) == \
        "partnerships@acme.co.ke"
    assert scraper.pick_best({"ops@acme.co.ke", "business@acme.co.ke", "aaron@acme.co.ke"}) == "business@acme.co.ke"
    assert scraper.ROLE_ORDER is contacts.ROLE_ORDER


def test_then_any_other_inbox_before_a_persons_address():
    assert scraper.pick_best({"alice@acme.co.ke", "support@acme.co.ke"}) == "support@acme.co.ke"
    assert scraper.pick_best({"wanjiru@acme.co.ke"}) == "wanjiru@acme.co.ke"
    assert scraper.pick_best(set()) is None


# --- An old lead's address, checked again ---

@pytest.fixture
def old_lead(tmp_path, monkeypatch):
    """A waiting fintech lead found by discovery 61 days ago, scored, contact-checked and given
    its first email's variant (the prepare job's sender.choose_variant)."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    db.init()
    db.add_lead("Acme", "acme.co.ke", "fintech", "info@acme.co.ke", "search")
    lead_id = db.find_lead_by_email("info@acme.co.ke")["id"]
    db.set_status(lead_id, "verified", "verify")
    found = (date.today() - timedelta(days=61)).isoformat() + " 08:00:00"
    with db.conn() as c:
        c.execute("UPDATE leads SET discovered_at=?, created_at=?, score=80, scored_at=CURRENT_TIMESTAMP,"
                  " score_version=?, contact_checked_at=CURRENT_TIMESTAMP, niche_fit='fintech',"
                  " niche_checked_at=CURRENT_TIMESTAMP, template_variant='A' WHERE id=?",
                  (found, found, targeting.SCORE_VERSION, lead_id))
    return lead_id


def site(monkeypatch, pages):
    """The lead's website: {url: html}; None = it can't be read at all."""
    read = []

    def read_site(website):
        read.append(website)
        return [] if pages is None else [(url, soup(html)) for url, html in pages.items()]

    monkeypatch.setattr(contacts, "read_site", read_site)
    return read


def lead(lead_id):
    with db.conn() as c:
        return dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


def test_which_leads_are_checked_again(old_lead):
    l = lead(old_lead)
    today = date.today()
    assert contacts.needs_address_check(l, today)
    assert not contacts.needs_address_check(l, today - timedelta(days=1))           # 60 days: not yet
    assert not contacts.needs_address_check(dict(l, source="CBK DCP directory Sep 2026"), today)
    assert contacts.needs_address_check(dict(l, source="CBK DCP directory Sep 2026", source_email="x@cbk"), today)
    assert not contacts.needs_address_check(dict(l, website=None), today)
    checked = (today - timedelta(days=6)).isoformat()
    assert not contacts.needs_address_check(dict(l, address_checked_at=checked), today)
    assert contacts.needs_address_check(dict(l, address_checked_at=(today - timedelta(days=7)).isoformat()), today)


def test_an_old_lead_is_not_ready_until_its_address_is_seen_again(old_lead, monkeypatch):
    monkeypatch.setattr(nichefit, "needs_check", lambda lead: False)
    assert not REAL_PREPARED(lead(old_lead))
    site(monkeypatch, {"https://acme.co.ke": "<p>Contact: info@acme.co.ke</p>"})
    assert contacts.recheck_addresses([lead(old_lead)]) == {"published": 1, "gone": 0, "unreadable": 0}
    l = lead(old_lead)
    assert l["address_page"] == "https://acme.co.ke" and l["address_checked_at"]
    assert REAL_PREPARED(l)


def test_an_address_no_longer_on_the_site_is_never_emailed(old_lead, monkeypatch):
    site(monkeypatch, {"https://acme.co.ke": "<p>Contact: hello@acme.co.ke</p>"})
    assert contacts.recheck_addresses([lead(old_lead)]) == {"published": 0, "gone": 1, "unreadable": 0}
    l = lead(old_lead)
    assert l["status"] == "excluded" and "no longer published" in l["excluded_reason"]
    assert old_lead not in {w["id"] for w in sender.approved_waiting(quiet=True)}


def test_a_site_that_cannot_be_read_leaves_the_lead_waiting(old_lead, monkeypatch):
    monkeypatch.setattr(nichefit, "needs_check", lambda lead: False)
    site(monkeypatch, None)
    assert contacts.recheck_addresses([lead(old_lead)]) == {"published": 0, "gone": 0, "unreadable": 1}
    l = lead(old_lead)
    assert l["status"] == "verified" and not l["address_checked_at"]
    assert not REAL_PREPARED(l)


def test_the_named_contacts_own_page_is_read_too(old_lead, monkeypatch):
    with db.conn() as c:
        c.execute("UPDATE leads SET contact_url='https://acme.co.ke/team' WHERE id=?", (old_lead,))
    site(monkeypatch, {"https://acme.co.ke": "<p>Welcome</p>"})
    monkeypatch.setattr(contacts, "fetch", lambda url: soup("<p>Jane, CEO: info@acme.co.ke</p>")
                        if url == "https://acme.co.ke/team" else None)
    assert contacts.recheck_addresses([lead(old_lead)])["published"] == 1
    assert lead(old_lead)["address_page"] == "https://acme.co.ke/team"


def test_a_recent_lead_is_not_read_again(old_lead, monkeypatch):
    with db.conn() as c:
        c.execute("UPDATE leads SET discovered_at=CURRENT_TIMESTAMP WHERE id=?", (old_lead,))
    read = site(monkeypatch, {"https://acme.co.ke": "<p>nothing</p>"})
    assert contacts.recheck_addresses([lead(old_lead)]) == {"published": 0, "gone": 0, "unreadable": 0}
    assert read == [] and lead(old_lead)["status"] == "verified"


def test_the_prepare_job_checks_the_address_before_the_send(old_lead, monkeypatch):
    monkeypatch.setattr(sender, "prepared", REAL_PREPARED)
    read = site(monkeypatch, {"https://acme.co.ke/contact": "<p>info@acme.co.ke</p>"})
    assert prepare.step() == 1
    assert read == ["acme.co.ke"]
    assert [l["id"] for l in prepare.ready()] == [old_lead]
