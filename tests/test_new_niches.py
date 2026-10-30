"""Four new niches (payment_provider, payg_energy, software_agency, software_vendor): their
A/B/C templates, uses, approval, discovery searches and need-score rules, and the importer for
CBK's directories of payment service providers and money remittance providers. The directories'
table cells and CBK's pages are given here, so nothing is downloaded and nothing is sent."""
import re

import pytest

import config
import crm
import db
import discover
import personalise
import ranking
import sender
import targeting
import verify

NICHES = ("payment_provider", "payg_energy", "software_agency", "software_vendor")
SIGNATURE = "Kind regards,\nFlorence\nTalkSasa Communications Ltd\n+254 762 000 008\ntalksasa.com\n\n--\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    monkeypatch.setattr(config, "DAILY_LIMIT", 50)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    monkeypatch.setattr(sender.notify, "send", lambda text: None)
    db.init()


# --- Templates, uses, approval, discovery ---

def test_uses_and_approval():
    assert personalise.USES["payment_provider"]["uses"] == ["OTPs", "payment confirmations", "transfer alerts"]
    assert personalise.USES["payg_energy"]["uses"] == ["unlock codes", "payment reminders", "low-credit alerts"]
    assert personalise.USES["software_agency"]["uses"] == personalise.USES["software_vendor"]["uses"] == \
        ["OTPs", "reminders", "alerts"]
    assert all(personalise.USES[n]["approved"] is True for n in NICHES)


@pytest.mark.parametrize("niche", NICHES)
def test_three_variants_with_follow_ups_signature_and_footer(world, niche):
    lead = {"id": 4, "company": "Pesa", "industry": niche, "email": "info@pesa.co.ke"}
    assert sender.variants(lead) == ["A", "B", "C"]
    for v in "ABC":
        lead["template_variant"] = v
        first, tname = sender.build(lead)
        f1, f1name = sender.build_followup(lead, 1)
        f2, f2name = sender.build_followup(lead, 2)
        assert (tname, f1name, f2name) == (f"{niche}_{v}", f"followup/{niche}_{v}", f"followup2/{niche}")
        assert list(first.iter_attachments()) == [] and f1["Subject"] == "Re: " + first["Subject"]
        for msg in (first, f1, f2):
            text = sender.plain_of(msg)
            assert text.startswith("Hello Pesa team,\n\n") and SIGNATURE in text
            assert 'just reply "unsubscribe"' in text and "{" not in text
            assert len(re.findall(r"(?:Mon|Tues|Wednes|Thurs|Fri)day \d+ \w{3} at \d", text)) == 2


def test_new_leads_are_sent_their_variants(world, smtp):
    for i, niche in enumerate(NICHES):
        db.add_lead(f"Co {i}", f"co{i}.co.ke", niche, f"info@co{i}.co.ke", "search")
        db.set_status(db.find_lead_by_email(f"info@co{i}.co.ke")["id"], "verified", "verify")
    sender.run()
    assert len(smtp["delivered"]) == 4


def test_discovery_searches():
    assert discover.QUERIES["payment_provider"] == ["payment gateway Kenya", "payment service provider Kenya",
                                                    "money remittance company Kenya"]
    assert discover.QUERIES["payg_energy"] == ["pay as you go solar Kenya", "solar home system company Kenya",
                                               "pay as you go clean cooking Kenya"]
    assert discover.QUERIES["software_agency"] == ["software development company Nairobi",
                                                   "web and mobile app developers Kenya", "system integrator Kenya"]
    assert discover.QUERIES["software_vendor"] == [
        "school management system Kenya", "hospital management system Kenya", "SACCO management software Kenya",
        "POS system Kenya", "ERP software Kenya", "property management software Kenya"]
    assert all(n in discover.PRIORITY for n in NICHES)


def test_crm_lists_niches_that_only_have_variants(world):
    crm.ensure_schema()
    listed = {t["name"]: t for t in crm.list_templates({"id": 1, "role": "admin"})["templates"]}
    for niche in NICHES:
        assert [v["variant"] for v in listed[niche]["variants"]] == ["A", "B", "C"]
        assert listed[niche]["kinds"] == ["followup2"]  # no legacy first email or follow-up 1
    assert "payment_provider_A" not in listed


# --- Need score ---

def score(industry="payment_provider", email="info@pesa.co.ke", website="pesa.co.ke", status="verified", text=""):
    return targeting.need_score({"industry": industry, "email": email, "website": website, "status": status}, text)


def test_starts_at_50_plus_15_for_its_own_domain():
    assert score(text="We move money.") == (65, ["+50 start (payment_provider)", "+15 own website domain (pesa.co.ke)"])


@pytest.mark.parametrize("text, points", [
    ("Read our API docs", 15), ("Built for developers", 15),
    ("Pay with M-Pesa", 10), ("Paybill 4000", 10), ("Accept payments", 10),
    ("Get our mobile app on Google Play", 10),
])
def test_each_site_signal(text, points):
    total, reasons = score(text=text)
    assert total == 65 + points, reasons


@pytest.mark.parametrize("niche", ["software_agency", "software_vendor"])
@pytest.mark.parametrize("text", ["Our clients include Equity and KCB", "Trusted by 300 schools",
                                  "Hospital software built for hospitals and clinics", "Used by 40 SACCOs",
                                  "ERP for growing businesses"])
def test_software_niches_score_their_clients(niche, text):
    assert score(industry=niche, text=text)[0] == 80


def test_client_rule_is_only_for_software_niches():
    assert score(industry="payg_energy", text="Our clients include 3,000 homes")[0] == 65


def test_free_email_and_invalid_email():
    total, reasons = score(email="pesa@gmail.com", website=None, status="invalid", text=None)
    assert total == 0 and "-20 email is free mail (gmail.com)" in reasons and "-30 email invalid" in reasons
    assert "+10 couldn't check website" not in reasons
    assert targeting.SCORE_VERSION == 5


# --- CBK importer: payment service providers and money remittance providers ---

PSP_ROWS = [
    [1, ["Safaricom PLC", "Postal Address: P.O. Box 66827-00800, Nairobi", "Telephone: +254722003272",
         "Email: customercare@safaricom.co.ke, ceo@safaricom.co.ke", "Website: https://www.safaricom.co.ke/",
         "Physical Address: Safaricom House, Waiyaki Way, Westlands, Nairobi", "Date First Licensed: January 1, 2016",
         "Chief Executive Officer: Mr. Peter Ndegwa"], ["To issue mobile money"]],
    [2, ["Wakandi Kenya Limited", "Telephone: + 254 748 760674", "Email: post@wakandi.ke",
         "Website: https://wakandi.co.ke/", "Date First Licensed: November 25, 2022",
         "Country Manager: Mr. Kavit Bhakoo"], [""]],
    [3, ["Unlimit Kenya Limited", "Postal Adress: P.O. Box 10643 - 00100", "Email: business.kenya@unlimint.com",
         "Website: https://www.unlimit.com/", "Physical address: Physical address: Chiromo Road, Nairobi",
         "Date First licensed: April 6, 2023", "Director: Mr. Trevor Goott", "post@wakandi.ke"], [""]],
]
MRP_ROWS = [
    [1, ["Afrisend Money", "Transfer Limited"], ["Stellato Mall, Muthithi", "Road, Westlands,", "Nairobi"],
     ["P.O. Box 1145-00606", "Nairobi", "Tel: 0111050640", "info@afrisend.com"], ["February 10, 2022"]],
    [2, ["Airtel Money", "Transfer Limited"], ["Parkside Towers"],
     ["P.O. BOX 73146 – 00200", "Tel: 0734110000", "corporatecommunications@ke.airtel.c"], ["July 6, 2017"]],
    [3, ["Kingscross Money", "Remittance Ltd"], ["Nairobi"], ["Tel: +254797630627", "Email:"], ["May 12, 2026"]],
    [4, ["Safaricom Money Transfer", "Services Limited"], ["Safaricom House"],
     ["Tel: 0722 00 3272", "ceo@safaricom.co.ke"], ["December 3, 2014"]],
]
PSP_URL = "https://www.centralbank.go.ke/wp-content/uploads/2026/09/Directory-of-Authorized-Payment-Service-Providers-August-10-2026.pdf"
MRP_URL = ("https://www.centralbank.go.ke/wp-content/uploads/2026/10/"
           "Directory-of-Licenced-Money-Remittance-Providers-as-at-September-30-2026.pdf")


@pytest.fixture
def cbk(world, monkeypatch):
    pages = {targeting.CBK_NPS_URL: f'<a href="{PSP_URL}">PSPs</a><a href="{PSP_URL.replace("2026/09", "2024/03")}">old</a>',
             targeting.CBK_SUPERVISION_URL: f'<a href="{MRP_URL}">MRPs</a>'}
    fetched = []

    class Page:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    monkeypatch.setattr(targeting.requests, "get", lambda url, **k: fetched.append(url) or Page(pages[url]))
    monkeypatch.setattr(targeting, "fetch", lambda src: fetched.append(src) or src.encode())
    monkeypatch.setattr(targeting, "table_rows", lambda data: PSP_ROWS if b"Payment" in data else MRP_ROWS)
    monkeypatch.setattr(targeting, "pdf_addresses",
                        lambda data: {"corporatecommunications@ke.airtel.com", "info@afrisend.com"})
    monkeypatch.setattr(verify, "domain_ok", lambda d: True)
    monkeypatch.setattr(targeting, "swap_emails", lambda: 0)
    return fetched


def test_reads_payment_service_providers(cbk):
    safaricom, wakandi, unlimit = targeting.parse_psp(PSP_URL.encode())
    assert (safaricom["name"], safaricom["email"], safaricom["website"], safaricom["licensed"], safaricom["phone"]) == \
        ("Safaricom PLC", "customercare@safaricom.co.ke", "safaricom.co.ke", "2016-01-01", "+254722003272")
    assert safaricom["address"] == "Safaricom House, Waiyaki Way, Westlands, Nairobi"
    assert wakandi["licensed"] == "2022-11-25"  # the Country Manager line isn't part of the date
    assert unlimit["emails"] == ["business.kenya@unlimint.com"]  # not the stray address after "Director:"
    assert unlimit["address"] == "Chiromo Road, Nairobi" and unlimit["licensed"] == "2023-04-06"


def test_reads_money_remittance_providers(cbk):
    afrisend, airtel, kingscross, safaricom = targeting.parse_mrp(MRP_URL.encode())
    assert (afrisend["name"], afrisend["email"], afrisend["phone"], afrisend["licensed"], afrisend["address"]) == \
        ("Afrisend Money Transfer Limited", "info@afrisend.com", "0111050640", "2022-02-10",
         "Stellato Mall, Muthithi Road, Westlands, Nairobi")
    assert airtel["email"] == "corporatecommunications@ke.airtel.com"  # cut short by the table, put back
    assert kingscross["email"] is None


def test_join_lines_puts_split_addresses_back():
    assert targeting.join_lines(["Tel: 1", "info@x.c", "om"]) == ["Tel: 1", "info@x.com"]
    assert targeting.join_lines(["info@x.com", "nairobi"]) == ["info@x.com", "nairobi"]
    assert targeting.join_lines(["a@x.c"], known={"a@x.com", "a@x.co.ke"}) == ["a@x.c"]  # two fit: left as it is


def test_finds_the_latest_directories_and_names_them(cbk):
    assert targeting.latest_cbk_pdf(targeting.CBK_NPS_URL, targeting.PSP_LINK, "x") == PSP_URL
    assert targeting.cbk_source("CBK PSP directory", PSP_URL) == "CBK PSP directory Aug 2026"
    assert targeting.cbk_source("CBK MRP directory", MRP_URL) == "CBK MRP directory Sep 2026"


def test_imports_both_lists_as_payment_providers_once_each(cbk):
    targeting.import_payment_providers()
    assert cbk == [targeting.CBK_NPS_URL, PSP_URL, targeting.CBK_SUPERVISION_URL, MRP_URL]
    with db.conn() as c:
        leads = [dict(r) for r in c.execute("SELECT * FROM leads ORDER BY id")]
    assert [l["company"] for l in leads] == ["Safaricom PLC", "Wakandi Kenya Limited", "Unlimit Kenya Limited",
                                             "Afrisend Money Transfer Limited", "Airtel Money Transfer Limited"]
    assert {l["industry"] for l in leads} == {"payment_provider"}
    assert [(l["source"], l["source_url"]) for l in leads[:3]] == [("CBK PSP directory Aug 2026", PSP_URL)] * 3
    assert [(l["source"], l["source_url"]) for l in leads[3:]] == [("CBK MRP directory Sep 2026", MRP_URL)] * 2
    _, result, _ = targeting.last_run()  # the MRP run: Safaricom is already a lead, Kingscross has no email
    assert [d["name"] for d in result["duplicates"]] == ["Safaricom Money Transfer Services Limited"]
    assert [m["name"] for m in result["missing_email"]] == ["Kingscross Money Remittance Ltd"]
    targeting.import_payment_providers()
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 5  # nothing added twice
