"""Regulators' lists (imports.py): only an organisation's name and listed website are taken;
known ones and do-not-contact domains are skipped; a missing website costs one Tavily search
while TAVILY_DISCOVERY_RESERVE calls stay for discovery, and waits as "website pending"
beyond that (the prepare job looks up WEBSITES_PER_DAY a day); a lead is added only at an
address its own website publishes today, never the list's. Fake lists, sites, DNS and
Tavily: nothing real is read, searched or sent."""
from datetime import datetime, timezone

import pytest
from bs4 import BeautifulSoup

import bounces
import config
import contacts
import db
import imports
import personalise
import prepare
import verify
import websearch


# --- Fakes -----------------------------------------------------------------------------------

def make_pdf(path, items, width=765, height=935):
    """A one-page PDF with each (x, y from the top, text) written in Helvetica 8pt."""
    esc = lambda t: t.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 8 Tf " + " ".join(f"1 0 0 1 {x} {height - y} Tm ({esc(t)}) Tj" for x, y, t in items) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Contents 4 0 R"
            " /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)
    return str(path)


TABLE = """<html><head><meta property="article:modified_time" content="2026-08-12T07:51:09+00:00"></head>
<body><table><tr><th>No.</th><th>UTILITY NAME</th><th>Website</th><th>Email</th></tr>{rows}</table></body></html>"""


def table_file(tmp_path, rows):
    """A saved web page whose table lists (name, website, email) rows."""
    html = TABLE.format(rows="".join(f"<tr><td>{i}</td><td>{n}</td><td>{w or ''}</td><td>{e or ''}</td></tr>"
                                     for i, (n, w, e) in enumerate(rows, 1)))
    path = tmp_path / "register.html"
    path.write_text(html, encoding="utf-8")
    return str(path)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Fake websites ({domain: {path: html}}), DNS (every mail server answers unless
    world["dns"] says False) and Tavily (world["search"]: {name: [result urls]})."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(contacts.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "SEARCH_PROVIDER", "tavily")
    monkeypatch.setattr(config, "TAVILY_API_KEY", "key")
    monkeypatch.setattr(config, "TAVILY_MONTHLY_CAP", 900)
    monkeypatch.setattr(config, "TAVILY_DISCOVERY_RESERVE", 300)
    monkeypatch.setattr(websearch, "now", lambda: datetime(2026, 11, 11, 8, tzinfo=timezone.utc))
    state = {"sites": {}, "dns": {}, "search": {}, "queries": [], "read": []}

    def fetch(url):
        domain = url.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")
        pages = state["sites"].get(domain)
        state["read"].append(domain)
        if pages is None:
            return None
        html = pages.get("/" + url.split("//", 1)[-1].split("/", 1)[-1].rstrip("/") if "/" in url.split("//", 1)[-1] else "")
        html = pages.get(url.split(domain, 1)[1].rstrip("/"), html)
        return BeautifulSoup(html, "html.parser") if html is not None else None

    def tavily(query, max_results):
        websearch.count("tavily")
        state["queries"].append(query)
        name = query.split('"')[1]
        return [{"title": name, "href": u, "body": ""} for u in state["search"].get(name, [])]

    monkeypatch.setattr(contacts, "fetch", fetch)
    monkeypatch.setattr(websearch, "tavily", tavily)
    monkeypatch.setattr(verify, "mail_answers", lambda domain: state["dns"].get(domain, True))
    db.init()
    return state


def site(world, domain, contact_html="<p>info@{d}</p>"):
    world["sites"][domain] = {"": f'<html><title>{domain}</title><a href="/contact">Contact</a></html>',
                              "/contact": contact_html.format(d=domain)}


def add_lead(company, email, website=None, status="verified"):
    with db.conn() as c:
        return c.execute("INSERT INTO leads (company, email, website, status, source) VALUES (?,?,?,?,'search')"
                         " RETURNING id", (company, email, website, status)).fetchone()[0]


def leads():
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM leads ORDER BY id")]


def entries():
    with db.conn() as c:
        return {r["name"]: dict(r) for r in c.execute("SELECT * FROM list_entries")}


def used(n):
    """This month's Tavily calls so far: n."""
    with db.conn() as c:
        c.execute(websearch.SCHEMA)
        c.execute("INSERT INTO search_usage (month, provider, calls) VALUES ('2026-11', 'tavily', ?)", (n,))


# --- Reading lists ---------------------------------------------------------------------------

def test_a_web_table_gives_names_and_listed_websites_and_the_pages_date(tmp_path):
    doc = imports.fetch(table_file(tmp_path, [("Nairobi City Water and Sewerage Company (NCWSC)",
                                               "https://www.nairobiwater.co.ke/", "info@x.com"),
                                              ("Wote Water", None, None)]))
    doc.modified = imports.modified_date(doc.data)
    assert imports.parse_any(doc) == [
        {"name": "Nairobi City Water and Sewerage Company (NCWSC)", "website": "nairobiwater.co.ke"},
        {"name": "Wote Water", "website": None}]
    assert imports.list_date(doc) == "2026-08-12"


def test_sasra_takes_schedules_one_to_three_across_both_columns(tmp_path):
    head = lambda x, s: [(x, 20, f"SCHEDULE {s}: LIST OF SACCO SOCIETIES"),
                         (x + 20, 40, "Names of the"), (x + 110, 40, "Postal Address"), (x + 200, 40, "County")]
    items = (head(10, "I") + [(10, 60, "1"), (30, 60, "2NK Sacco Society Ltd"), (120, 60, "P.O Box 12196 Nyeri"),
                              (10, 75, "2"), (30, 75, "Afya Sacco"), (120, 75, "P.O Box 1 Nairobi"),
                              (30, 85, "Society Ltd"), (120, 85, "Nairobi")]
             # the next column: the heading again, and the numbering goes on
             + head(395, "I") + [(395, 60, "3"), (415, 60, "Yetu Sacco Society Ltd"), (505, 60, "P.O Box 3 Meru")]
             + [(395, 120, "SCHEDULE II: LIST OF SACCO SOCIETIES"), (415, 140, "Names of the"), (560, 140, "Postal Address"),
                                      (395, 160, "1"), (415, 160, "Banki Kuu Regulated Non-WDT-"),
                                      (560, 160, "P.O Box 60000 Nairobi"), (415, 170, "Sacco")]
             + [(395, 200, "SCHEDULE IV: LIST OF SACCO SOCIETY WHICH CEASED"), (415, 220, "Names of the"),
                (505, 220, "Postal Address"), (395, 240, "1"), (415, 240, "Merged Sacco Society Ltd"),
                (505, 240, "P.O Box 9 Nairobi")]
             + [(395, 300, "Dated at Nairobi this 3rd day of February 2026")])
    doc = imports.fetch(make_pdf(tmp_path / "sasra.pdf", items))
    assert [e["name"] for e in imports.parse_sasra(doc)] == [
        "2NK Sacco Society Ltd", "Afya Sacco Society Ltd", "Yetu Sacco Society Ltd", "Banki Kuu Sacco"]
    assert imports.list_date(doc) == "2026-02-03"


def test_earb_takes_the_listed_companies_notice_and_its_own_date(tmp_path):
    items = [(30, 20, "Dated the 24th March, 2026."),   # another notice in the same gazette
             (30, 40, "LISTED ESTATE AGENTS COMPANIES"),
             (30, 60, "Company name Address"),
             (30, 75, "Advent Valuers Limited P.O. Box 50823-00100 Nairobi"),
             (30, 90, "Evero Realtors P.O. Box 2714-60200 Meru"),
             (30, 105, "Dated the 25th March, 2026."),
             (30, 120, "Someone Else P.O. Box 1 Nairobi")]
    doc = imports.fetch(make_pdf(tmp_path / "earb.pdf", items))
    assert [e["name"] for e in imports.parse_earb(doc)] == ["Advent Valuers Limited", "Evero Realtors"]
    assert imports.list_date(doc) == "2026-03-25"


@pytest.mark.parametrize("text, url, expected", [
    ("DatedatNairobithis3rddayofFebruary2026", "x.pdf", "2026-02-03"),
    ("listed as at 27th March, 2026.", "x.pdf", "2026-03-27"),
    ("", "Directory-of-Licenced-Money-Remittance-Providers-as-at-September-30-2026.pdf", "2026-09-30"),
    ("", "Directory-of-Licenced-Microfinance-Banks-Feb-2026.pdf", "2026-02"),
    ("", "list.pdf", None)])
def test_the_list_date(text, url, expected):
    assert imports.list_date(imports.Doc(b"%PDF", url), text) == expected


def test_a_bot_check_page_is_unavailable_not_a_list(monkeypatch):
    class Page:
        status_code, headers = 200, {}
        content = b"<html><head><title>Bot Verification</title><script>grecaptcha</script>"
    monkeypatch.setattr(imports.requests, "get", lambda *a, **k: Page())
    with pytest.raises(imports.Unavailable, match="bot check"):
        imports.fetch("https://www.ira.go.ke/x.pdf")


def test_new_niches_are_in_uses_yaml_and_not_approved():
    for niche in ("water_utility", "insurance", "proptech"):
        assert personalise.USES[niche]["approved"] is False
    for niche in {lst.niche for s in imports.SOURCES.values() for lst in s.lists}:
        assert niche in personalise.USES


# --- Importing -------------------------------------------------------------------------------

def test_only_an_address_the_site_publishes_is_used_never_the_lists(world, tmp_path):
    site(world, "kikuyuwater.co.ke")
    path = table_file(tmp_path, [("Kikuyu Water Company Limited", "kikuyuwater.co.ke", "md@gmail.com")])
    n = imports.import_list("wasreb", path, "water_utility")
    [lead] = leads()
    assert lead["email"] == "info@kikuyuwater.co.ke" and lead["source_email"] is None
    assert (lead["status"], lead["industry"], lead["source"]) == ("verified", "water_utility", "wasreb")
    assert lead["imported_at"] and lead["address_source"] == "site"
    assert not contacts.needs_list_check(lead)
    assert {k: n[k] for k in ("found", "known", "website_found", "website_pending", "email_found", "added")} == {
        "found": 1, "known": 0, "website_found": 1, "website_pending": 0, "email_found": 1, "added": 1}
    assert n["list_date"] == "2026-08-12" and world["queries"] == []   # listed website: no search


def test_no_address_on_the_site_means_no_lead(world, tmp_path):
    site(world, "quiet.co.ke", "<p>Call us</p>")
    n = imports.import_list("wasreb", table_file(tmp_path, [("Quiet Water", "quiet.co.ke", "a@quiet.co.ke")]),
                            "water_utility")
    assert leads() == [] and entries()["Quiet Water"]["state"] == "no_email"
    assert (n["email_found"], n["added"], n["no_email"]) == (0, 0, 1)


def test_known_names_domains_and_do_not_contact_are_skipped(world, tmp_path):
    add_lead("Tala Kenya Limited", "hello@tala.co.ke", "tala.co.ke")
    add_lead("Somebody", "x@lender.co.ke", "https://www.lender.co.ke")
    db.add_do_not_contact("campaign.co.ke", "Group A")
    site(world, "new.co.ke")
    path = table_file(tmp_path, [("Tala Kenya Ltd", None, None), ("Different Name", "lender.co.ke", None),
                                 ("Campaign Co", "campaign.co.ke", None), ("New Water", "new.co.ke", None),
                                 ("New Water Limited", None, None)])   # the same organisation twice in a list
    n = imports.import_list("wasreb", path, "water_utility")
    assert (n["found"], n["known"], n["do_not_contact"], n["added"]) == (5, 3, 1, 1)
    assert world["read"] and "campaign.co.ke" not in world["read"] and world["queries"] == []
    # a second import of the same list finds them all known (or still on do-not-contact)
    again = imports.import_list("wasreb", path, "water_utility")
    assert (again["known"], again["do_not_contact"], again["added"]) == (4, 1, 0)


def test_a_missing_website_costs_one_search(world, tmp_path):
    site(world, "2nksacco.co.ke")
    world["search"]["2NK Sacco Society Ltd"] = ["https://www.facebook.com/2nk", "https://kenyans.co.ke/x",
                                                "https://www.2nksacco.co.ke/about"]
    n = imports.import_list("sasra", table_file(tmp_path, [("2NK Sacco Society Ltd", None, None)]), "sacco")
    assert world["queries"] == ['"2NK Sacco Society Ltd" Kenya official website']
    assert websearch.calls("tavily") == 1
    assert (n["website_found"], n["added"]) == (1, 1)
    assert entries()["2NK Sacco Society Ltd"]["website_from"] == "search"


def test_a_search_finding_no_site_of_its_own(world, tmp_path):
    world["search"]["Afya Sacco"] = ["https://www.linkedin.com/afya", "https://www.otherthing.co.ke/"]
    n = imports.import_list("sasra", table_file(tmp_path, [("Afya Sacco", None, None)]), "sacco")
    assert n["no_website"] == 1 and entries()["Afya Sacco"]["state"] == "no_website" and leads() == []


def test_searches_stop_where_discoverys_reserve_starts(world, tmp_path):
    used(598)   # 900 cap - 300 kept for discovery: 2 left
    path = table_file(tmp_path, [(f"Org{i} Water", None, None) for i in range(5)])
    n = imports.import_list("wasreb", path, "water_utility")
    assert len(world["queries"]) == 2 and websearch.calls("tavily") == 600
    assert n["website_pending"] == 3
    assert sum(e["state"] == "pending" for e in entries().values()) == 3
    assert imports.search_budget() == 0


def test_no_tavily_means_every_missing_website_waits(world, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TAVILY_API_KEY", "")
    n = imports.import_list("wasreb", table_file(tmp_path, [("Org Water", None, None)]), "water_utility")
    assert n["website_pending"] == 1 and world["queries"] == []


def test_the_prepare_job_looks_up_twenty_a_day_and_carries_into_next_month(world, tmp_path, monkeypatch):
    used(600)   # nothing left this month
    imports.import_list("wasreb", table_file(tmp_path, [(f"Org{i} Water", None, None) for i in range(45)]),
                        "water_utility")
    assert world["queries"] == [] and imports.daily()["searched"] == 0
    for day, expected in ((1, 20), (1, 0), (2, 20), (3, 5)):
        monkeypatch.setattr(websearch, "now", lambda d=day: datetime(2026, 12, d, 18, tzinfo=timezone.utc))
        assert imports.daily()["searched"] == expected
    assert len(world["queries"]) == 45 and not any(e["state"] == "pending" for e in entries().values())


def test_a_site_found_later_is_read_and_its_lead_added_by_the_daily_step(world, tmp_path):
    used(600)
    imports.import_list("wasreb", table_file(tmp_path, [("Later Water", None, None)]), "water_utility")
    with db.conn() as c:
        c.execute(websearch.SCHEMA)
        c.execute("UPDATE search_usage SET calls=0")
    world["search"]["Later Water"] = ["https://laterwater.co.ke"]
    site(world, "laterwater.co.ke")
    assert imports.daily() == {"searched": 1, "retried": 0, "added": 1}
    assert leads()[0]["email"] == "info@laterwater.co.ke"


def test_an_unreadable_site_is_tried_again_another_day(world, tmp_path, monkeypatch):
    n = imports.import_list("wasreb", table_file(tmp_path, [("Down Water", "down.co.ke", None)]), "water_utility")
    assert n["unreadable"] == 1 and entries()["Down Water"]["state"] == "unreadable"
    assert imports.daily()["retried"] == 0          # not again the same day
    monkeypatch.setattr(websearch, "now", lambda: datetime(2026, 11, 12, 8, tzinfo=timezone.utc))
    site(world, "down.co.ke")
    assert imports.daily() == {"searched": 0, "retried": 1, "added": 1}


def test_an_address_opted_out_or_without_a_mail_server_is_not_used(world, tmp_path):
    db.suppress("sales@shared.co.ke", "unsubscribed")
    site(world, "shared.co.ke", "<p>sales@{d}</p>")
    n = imports.import_list("wasreb", table_file(tmp_path, [("Shared Water", "shared.co.ke", None)]), "water_utility")
    assert (n["email_found"], n["added"]) == (1, 0) and leads() == []
    world["dns"]["dead.co.ke"] = False
    site(world, "dead.co.ke")
    n = imports.import_list("wasreb", table_file(tmp_path, [("Dead Water", "dead.co.ke", None)]), "water_utility")
    assert (n["email_found"], n["added"], n["no_email"]) == (1, 0, 1) and leads() == []


def test_a_dry_run_saves_searches_and_reads_nothing(world, tmp_path):
    add_lead("Known Water", "a@known.co.ke")
    path = table_file(tmp_path, [("Known Water", None, None), ("Listed Water", "listed.co.ke", None),
                                 ("Unlisted Water", None, None)])
    n = imports.import_list("wasreb", path, "water_utility", dry_run=True)
    assert (n["found"], n["known"], n["website_found"], n["website_pending"]) == (3, 1, 1, 1)
    assert world["queries"] == [] and world["read"] == [] and len(leads()) == 1
    with db.conn() as c:
        c.executescript(imports.SCHEMA)
        assert c.execute("SELECT COUNT(*) FROM list_entries").fetchone()[0] == 0


def test_unknown_source_or_niche_is_refused(world, tmp_path):
    path = table_file(tmp_path, [("A Water", None, None)])
    with pytest.raises(SystemExit, match="Unknown source"):
        imports.import_list("nope", path, "water_utility")
    with pytest.raises(SystemExit, match="Unknown niche"):
        imports.import_list("wasreb", path, "plumbing")


def test_import_all_goes_on_past_a_list_it_cannot_read(world, tmp_path, monkeypatch, capsys):
    path = table_file(tmp_path, [("A Water", None, None)])

    def unavailable():
        raise imports.Unavailable("the site asks for a bot check")
    monkeypatch.setattr(imports, "SOURCES", {
        "ira": imports.Source("ira", "IRA", [imports.List("IRA list", "insurance", unavailable)]),
        "wasreb": imports.Source("wasreb", "WASREB", [imports.List("WASREB", "water_utility", path)])})
    results = imports.import_all()
    assert results["ira"][0]["error"] == "the site asks for a bot check"
    assert results["wasreb"][0]["found"] == 1
    table = "\n".join(imports.table(results))
    assert "unavailable" in table and "2026-08-12" in table
    for header in ("LIST DATE", "FOUND", "ALREADY KNOWN", "WEBSITE FOUND", "WEBSITE PENDING", "EMAIL FOUND", "ADDED"):
        assert header in table


# --- Results by source -----------------------------------------------------------------------

def test_bounces_and_stats_report_by_source(world, tmp_path):
    site(world, "a.co.ke")
    imports.import_list("wasreb", table_file(tmp_path, [("A Water", "a.co.ke", None)]), "water_utility")
    [lead] = leads()
    assert bounces.lead_source({"lead_id": lead["id"], "campaign": None, "source": "wasreb"}) == "wasreb"
    assert bounces.lead_source({"lead_id": 1, "campaign": None, "source": "CBK DCP directory Sep 2026"}) == "CBK"
    assert db.list_source_stats() == {"wasreb": {"leads": 1, "emailed": 0, "replied": 0, "bounced": 0}}


def test_the_prepare_job_runs_the_daily_step_and_survives_its_errors(world, monkeypatch):
    monkeypatch.setattr(prepare, "step", lambda skip: 0)
    monkeypatch.setattr(imports, "daily", lambda: {"searched": 3, "retried": 1, "added": 2})
    assert "list websites: searched 3, read again 1, added 2" in prepare.run()

    def boom():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(imports, "daily", boom)
    assert "list websites: FAILED (database is locked)" in prepare.run()
