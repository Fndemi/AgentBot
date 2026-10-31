"""Discovery never adds another bulk SMS company as a lead: not by its page title alone, but by
its description and headings, its address, and the address of the email its site gives. Law
firms are left out too, unless the search was for them (the professional category).
Websites are fakes: nothing is fetched."""
import pytest

import config
import db
import discover


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Fake websites: {domain: (title, headline)}; each site's email is EMAILS[domain]."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    sites, emails, looked = {}, {}, []

    def look_at(url):
        domain = discover.domain_of(url)
        looked.append(domain)
        if domain not in sites:
            return None
        title, headline = sites[domain]
        return title, True, f"{title} | {headline}"

    monkeypatch.setattr(discover, "look_at", look_at)
    monkeypatch.setattr(discover.scraper, "scrape_site", lambda home: {emails[discover.domain_of(home)]})
    monkeypatch.setattr(discover, "save_csv", lambda row: None)
    return sites, emails, looked


def add(world, domain, title, headline, email):
    sites, emails, _ = world
    sites[domain], emails[domain] = (title, headline), email
    return discover.consider(f"https://{domain}/", "software_vendor", "school management system Kenya", set())


def test_a_real_vendor_is_added(world):
    row = add(world, "ebingwa.com", "eBingwa | School management", "Fees, exams and parent SMS in one system",
              "support@ebingwa.com")
    assert row and row["email"] == "support@ebingwa.com"


def test_rival_known_by_its_headings_not_its_title(world, capsys):
    assert add(world, "textme.co.ke", "Textme | Home", "Cheap bulk SMS for every business", "info@textme.co.ke") is None
    assert 'another bulk SMS company ("bulk SMS" on textme.co.ke)' in capsys.readouterr().out


@pytest.mark.parametrize("domain", ["advantasms.com", "bongasms.co.ke", "africastalking.com", "talksasa.com"])
def test_rival_known_by_its_address(world, domain):
    assert add(world, domain, "Home", "Welcome", f"info@{domain}") is None


def test_product_site_whose_email_is_a_rivals_is_skipped(world, capsys):
    """jibuerp.com sells school software; its address, jibuerp@advantasms.com, is Advanta SMS's."""
    sites, _, looked = world
    sites["advantasms.com"] = ("Bulk SMS Kenya | Cheap Bulk SMS Service Provider", "")
    assert add(world, "jibuerp.com", "School Management Software", "Jibu ERP for schools",
               "jibuerp@advantasms.com") is None
    assert "jibuerp@advantasms.com: another bulk SMS company (advantasms.com)" in capsys.readouterr().out
    assert db.find_lead_by_email("jibuerp@advantasms.com") is None


def test_email_on_another_domain_is_checked_by_its_site(world, capsys):
    sites, _, looked = world
    sites["textco.co.ke"] = ("Textco", "SMS gateway and API for developers")
    assert add(world, "schoolapp.co.ke", "School App", "Fees made simple", "hello@textco.co.ke") is None
    assert "textco.co.ke" in looked
    sites["parentgroup.co.ke"] = ("Parent Group", "Schools across Kenya")
    assert add(world, "schoolapp2.co.ke", "School App 2", "Fees made simple", "hello@parentgroup.co.ke")


def test_free_mail_and_own_domain_need_no_second_look(world):
    _, _, looked = world
    assert add(world, "duka.co.ke", "Duka POS", "POS for shops", "duka@gmail.com")
    assert add(world, "pos.co.ke", "POS", "POS for shops", "sales@mail.pos.co.ke")
    assert looked == ["duka.co.ke", "pos.co.ke"]


@pytest.mark.parametrize("domain", ["tourisms.co.ke", "smsacco.co.ke"])
def test_names_that_only_contain_sms_are_not_rivals(domain):
    assert discover.rival_reason(domain, "Welcome") is None


def add_for(world, category, domain, title, email):
    sites, emails, _ = world
    sites[domain], emails[domain] = (title, "Welcome"), email
    return discover.consider(f"https://{domain}/", category, "payment service provider Kenya", set())


@pytest.mark.parametrize("domain, title", [
    ("kdsadvocates.com", "KDS Advocates and Legal Advisory"),
    ("mwangi.co.ke", "Mwangi & Co | Law Firm in Nairobi"),
    ("ab-llp.co.ke", "A&B | Home"),
    ("otieno.co.ke", "Otieno Attorneys"),
])
def test_law_firms_are_skipped_outside_professional(world, capsys, domain, title):
    assert add_for(world, "payment_provider", domain, title, f"info@{domain}") is None
    assert "a law firm" in capsys.readouterr().out


def test_law_firms_are_kept_when_the_search_was_for_them(world):
    assert add_for(world, "professional", "kdsadvocates.com", "KDS Advocates and Legal Advisory",
                   "info@kdsadvocates.com")


@pytest.mark.parametrize("title", ["Pesapal | Payments for businesses", "Legal notice | Paykit",
                                   "LegalTech Africa | Software"])
def test_other_titles_are_not_law_firms(world, title):
    assert discover.law_firm_reason("pesa.co.ke", title, "payment_provider") is None
