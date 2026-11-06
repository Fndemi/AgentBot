"""Leads from an imported list (a CBK directory, a CSV): the list's address isn't trusted.
Before the first email the lender's own website is read and an address it publishes today is
used (a named person only at high confidence, otherwise the best general inbox in ROLE_ORDER).
If the site publishes none, the list's address is kept only if it is on the site's domain and
its mail server answers; otherwise the lead is "no current address" and skipped. Fake websites
and fake DNS: nothing real is read, looked up or sent."""
import pytest
from bs4 import BeautifulSoup

import config
import contacts
import db
import prepare
import sender
import verify

REAL_PREPARED = sender.prepared  # conftest makes every lead count as prepared

SITE = "https://lender.co.ke"
HOME = '<html><title>Lender Credit</title><a href="/contact">Contact</a><a href="/team">Team</a></html>'
CARD = '<div class="card"><h3>{name}</h3><p>{title}</p>{email}</div>'


def team(*cards):
    return "<html><title>Our team</title>" + "".join(
        CARD.format(name=n, title=t, email=f'<a href="mailto:{e}">{e}</a>' if e else "") for n, t, e in cards) + "</html>"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake DNS: every domain's mail server answers unless world["dns"] says otherwise
    (False: no mail server, None: DNS couldn't say)."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(contacts.time, "sleep", lambda s: None)
    dns = {}
    monkeypatch.setattr(verify, "mail_answers", lambda domain: dns.get(domain, True))
    db.init()
    return {"dns": dns}


def website(monkeypatch, pages):
    """The lender's site: {path: html}; None = the site is down."""
    def fetch(url):
        if pages is None or not url.startswith(SITE):
            return None
        html = pages.get(url.removeprefix(SITE).rstrip("/"))
        return BeautifulSoup(html, "html.parser") if html is not None else None
    monkeypatch.setattr(contacts, "fetch", fetch)


def imported(email="loans@lender.co.ke", website="lender.co.ke", status="verified", company="Lender Credit"):
    """A lead added from the CBK directory, as targeting.import_directory adds one."""
    with db.conn() as c:
        lead_id = c.execute(
            "INSERT INTO leads (company, website, industry, email, source, discovered_at, imported_at, status)"
            " VALUES (?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?) RETURNING id",
            (company, website, "digital_lender", email, "CBK DCP directory Sep 2026", status)).fetchone()[0]
    return lead_id


def lead(lead_id):
    with db.conn() as c:
        return dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


def check(lead_id):
    from collections import Counter
    outcomes = Counter()
    contacts.run([lead(lead_id)], outcomes=outcomes)
    [(outcome, n)] = outcomes.items()
    return outcome


# --- An address the site publishes today ---

def test_a_general_inbox_on_the_site_replaces_the_lists_address(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>info@lender.co.ke</p>"})
    lead_id = imported()
    assert check(lead_id) == "changed"
    l = lead(lead_id)
    assert (l["email"], l["source_email"], l["address_source"], l["address_page"], l["contact_name"]) == \
        ("info@lender.co.ke", "loans@lender.co.ke", "site", f"{SITE}/contact", None)
    msg, _ = sender.build(l)
    assert msg["To"] == "info@lender.co.ke" and sender.plain_of(msg).startswith("Hello,\n\n")


def test_inboxes_in_role_order(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>info@lender.co.ke hello@lender.co.ke tech@lender.co.ke "
                                                "enquiries@lender.co.ke</p>"})
    lead_id = imported()
    check(lead_id)
    assert lead(lead_id)["email"] == "tech@lender.co.ke"   # ROLE_ORDER: tech before hello and info
    website(monkeypatch, {"": HOME, "/contact": "<p>enquiries@lender.co.ke jkamau@lender.co.ke</p>"})
    other = imported("credit@lender2.co.ke", "lender.co.ke", company="Lender Two")
    check(other)
    assert lead(other)["email"] == "enquiries@lender.co.ke"  # an inbox, never a person's address


def test_a_named_person_only_at_high_confidence(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>info@lender.co.ke</p>",
                          "/team": team(("Jane Wanjiru", "CEO", "jane@lender.co.ke"))})
    lead_id = imported()
    check(lead_id)
    l = lead(lead_id)
    assert (l["email"], l["contact_name"]) == ("jane@lender.co.ke", "Jane Wanjiru")
    assert sender.plain_of(sender.build(l)[0]).startswith("Hello Jane,\n\n")


def test_a_medium_confidence_person_is_not_used(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>jwanjiru@lender.co.ke sales@lender.co.ke</p>",
                          "/team": team(("Jane Wanjiru", "CEO", None))})
    lead_id = imported()
    check(lead_id)
    l = lead(lead_id)
    assert (l["email"], l["contact_name"]) == ("sales@lender.co.ke", None)
    with db.conn() as c:   # she is still kept, at medium
        assert c.execute("SELECT confidence FROM lead_contacts WHERE name='Jane Wanjiru'").fetchone()[0] == "medium"


def test_the_lists_address_published_on_the_site_is_kept(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>loans@lender.co.ke</p>"})
    lead_id = imported()
    assert check(lead_id) == "kept"
    assert (lead(lead_id)["email"], lead(lead_id)["address_source"], lead(lead_id)["address_page"]) == \
        ("loans@lender.co.ke", "site", f"{SITE}/contact")


def test_an_address_whose_mail_server_doesnt_answer_is_passed_over(world, monkeypatch):
    world["dns"]["mail.lender.co.ke"] = False
    website(monkeypatch, {"": HOME, "/contact": "<p>sales@mail.lender.co.ke info@lender.co.ke</p>"})
    lead_id = imported()
    check(lead_id)
    assert lead(lead_id)["email"] == "info@lender.co.ke"


def test_another_leads_address_is_passed_over(world, monkeypatch):
    db.add_lead("Lender Credit (other)", "lender.co.ke", "digital_lender", "info@lender.co.ke", "search")
    website(monkeypatch, {"": HOME, "/contact": "<p>info@lender.co.ke contact@lender.co.ke</p>"})
    lead_id = imported()
    check(lead_id)
    assert lead(lead_id)["email"] == "contact@lender.co.ke"


# --- The site publishes none ---

def test_list_address_kept_on_the_sites_domain_with_a_mail_server(world, monkeypatch):
    website(monkeypatch, {"": HOME})
    lead_id = imported()
    assert check(lead_id) == "kept"
    l = lead(lead_id)
    assert (l["email"], l["address_source"], l["status"]) == ("loans@lender.co.ke", "list", "verified")


def test_list_address_on_another_domain_means_no_current_address(world, monkeypatch):
    website(monkeypatch, {"": HOME})
    lead_id = imported("lendercredit@gmail.com", "lender.co.ke")
    assert check(lead_id) == "skipped"
    l = lead(lead_id)
    assert l["status"] == "excluded" and l["excluded_reason"].startswith("no current address: ")
    assert lead_id not in {w["id"] for w in sender.approved_waiting(quiet=True)}


def test_list_address_whose_mail_server_doesnt_answer_means_no_current_address(world, monkeypatch):
    world["dns"]["lender.co.ke"] = False
    website(monkeypatch, {"": HOME})
    lead_id = imported()
    assert check(lead_id) == "skipped"
    assert "mail server doesn't answer" in lead(lead_id)["excluded_reason"]


def test_no_website_means_no_current_address(world):
    lead_id = imported("lendercredit@gmail.com", None)
    assert check(lead_id) == "skipped"
    assert lead(lead_id)["status"] == "excluded"


# --- Not sure yet: tried again ---

def test_a_site_that_cant_be_read_is_tried_again_then_the_list_rule(world, monkeypatch):
    website(monkeypatch, None)
    lead_id = imported()
    assert [check(lead_id), check(lead_id)] == ["waiting", "waiting"]
    assert lead(lead_id)["address_source"] is None and lead(lead_id)["status"] == "verified"
    assert check(lead_id) == "kept"                 # third try: the list's address on its own
    assert lead(lead_id)["address_source"] == "list"


def test_dns_that_cant_say_waits(world, monkeypatch):
    world["dns"]["lender.co.ke"] = None
    website(monkeypatch, {"": HOME})
    lead_id = imported()
    assert check(lead_id) == "waiting"
    assert lead(lead_id)["status"] == "verified" and lead(lead_id)["address_source"] is None


# --- Which leads, and the first email waits for it ---

def test_the_first_email_waits_until_the_address_is_settled(world, monkeypatch):
    lead_id = imported()
    with db.conn() as c:
        c.execute("UPDATE leads SET score=80, scored_at=CURRENT_TIMESTAMP, score_version=?,"
                  " contact_checked_at=CURRENT_TIMESTAMP WHERE id=?", (__import__("targeting").SCORE_VERSION, lead_id))
    monkeypatch.setattr(contacts, "fetch", lambda url: None)
    assert not REAL_PREPARED(lead(lead_id))
    website(monkeypatch, {"": HOME, "/contact": "<p>info@lender.co.ke</p>"})
    monkeypatch.setattr(sender, "prepared", REAL_PREPARED)
    prepare.step()                                   # the prepare job settles it, read before or not
    assert lead(lead_id)["email"] == "info@lender.co.ke"
    assert REAL_PREPARED(lead(lead_id))
    search = db.add_lead("Found", "found.co.ke", "fintech", "info@found.co.ke", "search")
    assert not contacts.needs_list_check(db.find_lead_by_email("info@found.co.ke"))


def test_which_leads_are_checked(world, monkeypatch):
    waiting, invalid, emailed = imported(), imported("a@b2.co.ke", "b2.co.ke", "invalid", "B2"), \
        imported("x@c3.co.ke", "c3.co.ke", company="C3")
    db.record_sent(emailed, "<1@x>", "t")
    db.set_status(emailed, "sent", "send")
    db.add_lead("Found", "found.co.ke", "fintech", "info@found.co.ke", "search")
    csv_lead = db.add_lead("Listed", "listed.co.ke", "sacco", "sacco@listed.co.ke", "csv") and \
        db.find_lead_by_email("sacco@listed.co.ke")["id"]
    assert {l["id"] for l in contacts.listed_to_check()} == {waiting, invalid, csv_lead}


def test_an_invalid_list_address_revived_by_the_site(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>info@lender.co.ke</p>"})
    lead_id = imported("old@lender-old.co.ke", "lender.co.ke", "invalid")
    assert check(lead_id) == "changed"
    assert (lead(lead_id)["email"], lead(lead_id)["status"]) == ("info@lender.co.ke", "verified")


def test_a_kept_list_address_is_not_rechecked_after_60_days(world, monkeypatch):
    website(monkeypatch, {"": HOME})
    lead_id = imported()
    with db.conn() as c:
        c.execute("UPDATE leads SET source_email='loans@lender.co.ke', discovered_at='2026-01-01 10:00:00' WHERE id=?",
                  (lead_id,))
    check(lead_id)
    assert lead(lead_id)["address_source"] == "list"
    assert not contacts.needs_address_check(lead(lead_id))


def test_the_command_counts_changed_kept_skipped_and_waiting(world, monkeypatch, capsys):
    import main
    pages = {"https://a.co.ke": "<p>info@a.co.ke</p>", "https://b.co.ke": "<p>Welcome</p>",
             "https://c.co.ke": "<p>Welcome</p>"}
    monkeypatch.setattr(contacts, "fetch", lambda url: BeautifulSoup(pages[url.rstrip("/")], "html.parser")
                        if url.rstrip("/") in pages else None)
    imported("loans@a.co.ke", "a.co.ke", company="A")         # changed: info@ on its site
    imported("loans@b.co.ke", "b.co.ke", company="B")         # kept: nothing published, list rule
    imported("b@gmail.com", "c.co.ke", company="C")           # skipped: list address on gmail
    imported("loans@d.co.ke", "d.co.ke", company="D")         # waiting: site down
    assert contacts.check_listed() == {"changed": 1, "kept": 1, "skipped": 1, "waiting": 1}
    out = capsys.readouterr().out
    assert "4 leads from imported lists not emailed yet and not checked" in out
    assert "Changed 1, kept 1, skipped 1 (no current address), waiting 1" in out
    monkeypatch.setattr("sys.argv", ["main.py", "list-addresses", "--limit", "1"])
    main.main()
    assert "1 leads from imported lists not emailed yet and not checked" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["main.py", "list-addresses", "now"])
    with pytest.raises(SystemExit, match="Usage"):
        main.main()
