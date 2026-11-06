"""Named contacts (contacts.py), from free sources only: the lead's own pages, sitemap, JSON-LD and
PDFs on its own domain, at most 15 per site. Every person with a title in TITLES is kept in
lead_contacts with a confidence (high: address on their card; medium: plainly theirs, found
elsewhere on the site, in a PDF or JSON-LD; low: no address, never emailed). The first email goes
to the best-ranked person with high or medium confidence, else the best general inbox. No
address is ever guessed or built from a name. Fake websites: nothing real is read or sent."""
import pytest
from bs4 import BeautifulSoup

import config
import contacts
import crm
import db
import sender

SITE = "https://acme.co.ke"
HOME = '<html><title>Acme Pay</title><a href="/team">Our team</a><p>Payments for shops.</p></html>'


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(contacts.time, "sleep", lambda s: None)
    db.init()


def website(monkeypatch, pages, files=None, pdfs=None):
    """A fake site: pages {path: html}; files {path: bytes} (a sitemap); pdfs {path: text}.
    Returns every address asked for, in order."""
    asked = []

    def path_of(url):
        return url.removeprefix(SITE).rstrip("/") if url.startswith(SITE) else url

    def fetch(url):
        asked.append(url)
        html = pages.get(path_of(url))
        return BeautifulSoup(html, "html.parser") if html is not None else None

    def fetch_raw(url):
        asked.append(url)
        path = path_of(url)
        if path in (pdfs or {}):
            return b"%PDF-1.4 " + path.encode()
        return (files or {}).get(path)

    monkeypatch.setattr(contacts, "fetch", fetch)
    monkeypatch.setattr(contacts, "fetch_raw", fetch_raw)
    monkeypatch.setattr(contacts, "pdf_text", lambda data: (pdfs or {})[data.decode().removeprefix("%PDF-1.4 ")])
    return asked


CARD = '<div class="card"><h3>{name}</h3><p>{title}</p>{email}</div>'


def team(*cards):
    return "<html><title>Our team | Acme</title><h1>Leadership</h1>" + "".join(
        CARD.format(name=n, title=t, email=f'<a href="mailto:{e}">{e}</a>' if e else "") for n, t, e in cards) + "</html>"


def people(found):
    return [(p["name"], p["title_rank"], p["email"], p["confidence"]) for p in found["people"]]


# --- Who is found, and how sure ---

def test_every_person_is_kept_with_a_confidence(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>Write to potieno@acme.co.ke or info@acme.co.ke</p>",
                          "/team": team(("Jane Wanjiru", "Chief Executive Officer", "jane@acme.co.ke"),
                                        ("Peter Otieno", "Chief Technology Officer", None),
                                        ("Ann Muthoni", "Head of Product", None),
                                        ("Mark Kamau", "Head of Marketing", "mark@acme.co.ke"))})
    found = contacts.find(SITE, "Acme")
    assert people(found) == [("Jane Wanjiru", 1, "jane@acme.co.ke", "high"),
                             ("Peter Otieno", 2, "potieno@acme.co.ke", "medium"),
                             ("Ann Muthoni", 3, None, "low")]           # marketing isn't in TITLES
    assert found["contact"] == {"name": "Jane Wanjiru", "title": "Chief Executive Officer",
                                "email": "jane@acme.co.ke", "url": f"{SITE}/team"}
    assert found["people"][0]["email_type"] == "named"


def test_rank_comes_before_confidence(world, monkeypatch):
    """CTO with an address found elsewhere (medium) beats a product manager with one on their card."""
    website(monkeypatch, {"": HOME, "/contact": "<p>david.kamau@acme.co.ke</p>",
                          "/team": team(("Grace Njeri", "Product Manager", "grace@acme.co.ke"),
                                        ("Paul Mwangi", "Operations Lead", "paul@acme.co.ke"),
                                        ("David Kamau", "Head of Technology", None))})
    found = contacts.find(SITE, "Acme")
    assert [p["name"] for p in found["people"]] == ["David Kamau", "Grace Njeri", "Paul Mwangi"]
    assert found["contact"]["name"] == "David Kamau"


def test_founder_ceo_and_md_share_a_rank_and_high_wins(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>jwanjiru@acme.co.ke</p>",
                          "/team": team(("Jane Wanjiru", "Co-founder", None),
                                        ("Tom Ouma", "Managing Director", "tom@acme.co.ke"))})
    found = contacts.find(SITE, "Acme")
    assert [(p["name"], p["title_rank"], p["confidence"]) for p in found["people"]] == \
        [("Tom Ouma", 1, "high"), ("Jane Wanjiru", 1, "medium")]
    assert found["contact"]["email"] == "tom@acme.co.ke"


def test_a_posts_own_inbox_on_their_card_is_high_but_a_general_one_is_not_theirs(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/team": team(("Jane Wanjiru", "CEO", "ceo@acme.co.ke"),
                                                  ("Peter Otieno", "CTO", "info@acme.co.ke"))})
    found = contacts.find(SITE, "Acme")
    assert people(found) == [("Jane Wanjiru", 1, "ceo@acme.co.ke", "high"), ("Peter Otieno", 2, None, "low")]
    assert found["people"][0]["email_type"] == "role"


def test_an_address_is_never_guessed_from_a_name(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>info@acme.co.ke jane@acme.co.ke</p>",
                          "/team": team(("Jane Wanjiru", "Founder", None))})
    found = contacts.find(SITE, "Acme")
    # "jane@" away from her card could be another Jane: not plainly hers, so low
    assert people(found) == [("Jane Wanjiru", 1, None, "low")]
    assert found["contact"] is None and found["role"] == "info@acme.co.ke"
    assert sorted(found["emails"]) == ["info@acme.co.ke", "jane@acme.co.ke"]  # only what the site shows


def test_general_inboxes_in_order(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>info@acme.co.ke contact@acme.co.ke hello@acme.co.ke "
                                                "tech@acme.co.ke partnerships@acme.co.ke sales@gmail.com</p>"})
    found = contacts.find(SITE, "Acme")
    assert found["role"] == "partnerships@acme.co.ke"
    assert [e for e, _ in found["inboxes"]] == ["partnerships@acme.co.ke", "hello@acme.co.ke",
                                                "contact@acme.co.ke", "info@acme.co.ke"]
    assert all(page == f"{SITE}/contact" for _, page in found["inboxes"])


# --- More places to look ---

def test_json_ld_people(world, monkeypatch):
    ld = ('<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Organization",'
          ' "founder": {"@type": "Person", "name": "Jane Wanjiru", "email": "mailto:jane.wanjiru@acme.co.ke"},'
          ' "employee": [{"@type": "Person", "name": "Peter Otieno", "jobTitle": "CTO", "email": "boss@acme.co.ke"},'
          ' {"@type": "Person", "name": "Lucy Achieng", "jobTitle": "Accountant"}]}</script>')
    website(monkeypatch, {"": HOME.replace("</html>", ld + "</html>")})
    found = contacts.find(SITE, "Acme")
    assert people(found) == [("Jane Wanjiru", 1, "jane.wanjiru@acme.co.ke", "medium"),
                             ("Peter Otieno", 2, None, "low")]   # boss@ isn't plainly Peter's


def test_a_company_profile_pdf_on_its_own_domain(world, monkeypatch):
    home = HOME.replace("</html>", '<a href="/files/Acme-Company-Profile.pdf">Profile</a>'
                                   '<a href="https://partner.co.ke/deck.pdf">Partner deck</a></html>')
    asked = website(monkeypatch, {"": home},
                    pdfs={"/files/Acme-Company-Profile.pdf": "Our leadership\nPeter Otieno\nChief Technology Officer\n"
                                                             "Email: potieno@acme.co.ke\nGrace Njeri\nHead of Product"})
    found = contacts.find(SITE, "Acme")
    assert people(found) == [("Peter Otieno", 2, "potieno@acme.co.ke", "medium"), ("Grace Njeri", 3, None, "low")]
    assert found["people"][0]["url"] == f"{SITE}/files/Acme-Company-Profile.pdf"
    assert "https://partner.co.ke/deck.pdf" not in asked     # never another domain's PDF


def test_pages_only_the_sitemap_knows(world, monkeypatch):
    sitemap = (b'<?xml version="1.0"?><urlset><url><loc>https://acme.co.ke/about/leadership-team</loc></url>'
               b'<url><loc>https://acme.co.ke/blog/post-1</loc></url>'
               b'<url><loc>https://other.co.ke/team</loc></url></urlset>')
    asked = website(monkeypatch, {"": '<html><title>Acme</title></html>',
                                  "/about/leadership-team": team(("Jane Wanjiru", "CEO", "jane@acme.co.ke"))},
                    files={"/sitemap.xml": sitemap})
    assert contacts.find(SITE, "Acme")["contact"]["email"] == "jane@acme.co.ke"
    assert f"{SITE}/blog/post-1" not in asked and "https://other.co.ke/team" not in asked


def test_posts_and_repeated_pages_are_not_read(world, monkeypatch):
    home = ('<html><title>Acme</title><a href="/blog/property-management-tips">Management tips</a>'
            '<a href="/news/our-team-grows">Our team grows</a><a href="/contact">Contact</a>'
            '<a href="/contact?intent=demo">Book a demo</a><a href="/about/leadership">Leadership</a></html>')
    asked = website(monkeypatch, {"": home, "/contact": "<p>info@acme.co.ke</p>",
                                  "/about/leadership": team(("Jane Wanjiru", "CEO", "jane@acme.co.ke"))})
    pages = [u for u, _ in contacts.crawl(SITE)["pages"]]
    assert pages[:3] == [SITE, f"{SITE}/contact", f"{SITE}/about/leadership"]
    assert not any("blog" in u or "news" in u or "?" in u for u in asked)


def test_a_page_the_site_repeats_at_every_address_counts_once(world, monkeypatch):
    same = "<html><title>Acme</title><p>Everything on one page. info@acme.co.ke</p></html>"
    website(monkeypatch, {path: same for path in ("",) + tuple(contacts.PATHS[1:])})
    assert [u for u, _ in contacts.crawl(SITE)["pages"]] == [SITE]


def test_at_most_15_pages_a_site(world, monkeypatch):
    links = "".join(f'<a href="/team/member-{i}">Team member {i}</a>' for i in range(40))
    pdf_links = "".join(f'<a href="/profile-{i}.pdf">Profile {i}</a>' for i in range(10))
    pages = {"": f"<html><title>Acme</title>{links}{pdf_links}</html>"}
    pages.update({f"/team/member-{i}": f"<p>Person {i}</p>" for i in range(40)})
    website(monkeypatch, pages, pdfs={f"/profile-{i}.pdf": "Nothing here" for i in range(10)})
    site = contacts.crawl(SITE)
    assert len(site["pages"]) + len(site["pdfs"]) == contacts.MAX_PAGES == 15
    assert len(site["pages"]) == contacts.MAX_HTML and len(site["pdfs"]) == 3


def test_an_unreadable_homepage_reads_nothing_else(world, monkeypatch):
    asked = website(monkeypatch, {"/team": team(("Jane Wanjiru", "CEO", "jane@acme.co.ke"))})
    assert contacts.crawl(SITE) == {"pages": [], "pdfs": []}
    assert asked == [SITE]


# --- Saved on the lead, and the email ---

def add(email="info@acme.co.ke"):
    db.add_lead("Acme", "acme.co.ke", "fintech", email, "search")
    lead_id = db.find_lead_by_email(email)["id"]
    db.set_status(lead_id, "verified", "verify")
    return lead_id


def lead(lead_id):
    with db.conn() as c:
        return c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def saved(lead_id):
    with db.conn() as c:
        return [tuple(r) for r in c.execute(
            "SELECT name, title_rank, email, email_type, confidence, source_url FROM lead_contacts"
            " WHERE lead_id=? ORDER BY title_rank, id", (lead_id,))]


def test_run_keeps_everyone_and_writes_to_the_best(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>sales@acme.co.ke info@acme.co.ke</p>",
                          "/team": team(("Jane Wanjiru", "CEO", "jane@acme.co.ke"), ("Ann Muthoni", "Head of Product", None))})
    lead_id = add()
    contacts.run([lead(lead_id)])
    assert saved(lead_id) == [("Jane Wanjiru", 1, "jane@acme.co.ke", "named", "high", f"{SITE}/team"),
                              ("Ann Muthoni", 3, None, None, "low", f"{SITE}/team"),
                              (None, 5, "sales@acme.co.ke", "general", "high", f"{SITE}/contact"),
                              (None, 10, "info@acme.co.ke", "general", "high", f"{SITE}/contact")]
    l = lead(lead_id)
    assert (l["email"], l["contact_name"], l["source_email"]) == ("jane@acme.co.ke", "Jane Wanjiru", "info@acme.co.ke")
    msg, _ = sender.build(l)
    assert msg["To"] == "jane@acme.co.ke" and sender.plain_of(msg).startswith("Hello Jane,\n\n")


def test_low_confidence_people_are_never_written_to(world, monkeypatch):
    website(monkeypatch, {"": HOME, "/contact": "<p>hello@acme.co.ke</p>",
                          "/team": team(("Jane Wanjiru", "CEO", None))})
    lead_id = add("support@acme.co.ke")
    contacts.run([lead(lead_id)])
    l = lead(lead_id)
    assert (l["email"], l["contact_name"]) == ("hello@acme.co.ke", None)     # the best general inbox
    assert ("Jane Wanjiru", 1, None, None, "low", f"{SITE}/team") in saved(lead_id)
    msg, _ = sender.build(l)
    assert sender.plain_of(msg).startswith("Hello,\n\n")


def test_nothing_on_the_site_keeps_the_address_it_came_with(world, monkeypatch):
    website(monkeypatch, {"": HOME})
    lead_id = add("accounts@acme.co.ke")
    contacts.run([lead(lead_id)])
    assert lead(lead_id)["email"] == "accounts@acme.co.ke" and saved(lead_id) == []


def test_reading_again_replaces_what_was_kept(world, monkeypatch):
    lead_id = add()
    with db.conn() as c:
        contacts.save_contacts(c, lead_id, {"people": [{"name": "Old Name", "title": "CEO", "title_rank": 1,
                                                        "email": None, "email_type": None, "url": SITE,
                                                        "confidence": "low"}], "inboxes": []})
        contacts.save_contacts(c, lead_id, {"people": [], "inboxes": [("info@acme.co.ke", SITE)]})
    assert saved(lead_id) == [(None, 10, "info@acme.co.ke", "general", "high", SITE)]


def test_the_lead_page_lists_every_contact(world, monkeypatch):
    crm.ensure_schema()
    website(monkeypatch, {"": HOME, "/contact": "<p>info@acme.co.ke</p>",
                          "/team": team(("Jane Wanjiru", "CEO", "jane@acme.co.ke"), ("Ann Muthoni", "Head of Product", None))})
    lead_id = add()
    contacts.run([lead(lead_id)])
    for role in ("admin", "sales"):
        found = crm.lead_detail(lead_id, {"id": 1, "role": role})["lead"]["personal"]["contacts"]
        assert [(f["name"], f["title"], f["email"], f["confidence"], f["source_url"], f["to"]) for f in found] == [
            ("Jane Wanjiru", "CEO", "jane@acme.co.ke", "high", f"{SITE}/team", True),
            ("Ann Muthoni", "Head of Product", None, "low", f"{SITE}/team", False),
            (None, None, "info@acme.co.ke", "high", f"{SITE}/contact", False)]


def test_the_crm_page_shows_the_contacts_list():
    from pathlib import Path
    page = (Path(__file__).parent.parent / "static" / "crm.html").read_text(encoding="utf-8")
    assert "Contacts found" in page and "no published address" in page and "f.source_url" in page


# --- python main.py contacts ---

def found_on(lead_id, *rows, read_at="2026-11-01 10:00:00"):
    with db.conn() as c:
        c.execute("UPDATE leads SET contact_checked_at=? WHERE id=?", (read_at, lead_id))
        for name, rank, conf in rows:
            c.execute("INSERT INTO lead_contacts (lead_id, name, title_rank, confidence) VALUES (?,?,?,?)",
                      (lead_id, name, rank, conf))


def test_summary_by_title_and_confidence(world, capsys, monkeypatch):
    a, b, c_, d = (add(f"info@lead{i}.co.ke") for i in range(4))
    with db.conn() as c:
        c.execute("UPDATE leads SET contact_name='Jane', contact_email=email WHERE id=?", (a,))
    found_on(a, ("Jane", 1, "high"), ("Ann", 3, "low"), read_at="2026-11-04 10:00:00")
    found_on(b, ("Peter", 2, "medium"), ("Tom", 1, "low"), read_at="2026-11-03 10:00:00")
    found_on(c_, ("Lucy", 4, "low"), read_at="2026-11-02 10:00:00")
    found_on(d, read_at="2026-11-01 10:00:00")
    lines = contacts.summary()
    assert lines[:4] == ["Leads whose website was read for contacts: 4",
                         "With a named contact to write to (high or medium confidence): 2 (50%)",
                         "Named on the site but no address of theirs published (low only): 1 (25%)",
                         "First email goes to: a named person 1, a general inbox 3, the address it came with 0"]
    table = "\n".join(lines)
    assert "  Founder/CEO/Managing Director       2     1       0     1" in table
    assert "  CTO/Head of Technology              1     0       1     0" in table
    assert "  Operations/Commercial Lead          1     0       0     1" in table
    recent = contacts.summary(2)
    assert recent[0] == "Leads whose website was read for contacts: 2 (the 2 read most recently)"
    assert recent[1] == "With a named contact to write to (high or medium confidence): 2 (100%)"
    import main
    monkeypatch.setattr("sys.argv", ["main.py", "contacts", "--recent", "1"])
    main.main()
    assert "Leads whose website was read for contacts: 1 (the 1 read most recently)" in capsys.readouterr().out


def test_summary_with_nothing_read(world):
    assert contacts.summary() == ["Leads whose website was read for contacts: 0"]
