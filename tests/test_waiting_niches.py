"""The five niches that were waiting (ecommerce, delivery_app, edtech, healthtech,
microfinance): their A/B/C templates, uses and approval, their need-score rules, and the
importer for CBK's directory of licensed microfinance banks. The directory's text and CBK's
page are given here, so nothing is downloaded and nothing is sent."""
import re

import pytest

import config
import db
import personalise
import ranking
import sender
import targeting
import verify

NICHES = ("ecommerce", "delivery_app", "edtech", "healthtech", "microfinance")
SIGNATURE = "Kind regards,\nFlorence\nTalkSasa Communications Ltd\n+254 762 000 008\ntalksasa.com\n\n--\n"
USES = {"ecommerce": ["order confirmations", "delivery updates", "OTPs"],
        "delivery_app": ["delivery codes", "order and rider updates", "OTPs"],
        "edtech": ["class reminders", "payment reminders", "OTPs"],
        "healthtech": ["appointment reminders", "results-ready alerts", "OTPs"],
        "microfinance": ["loan repayment reminders", "disbursement confirmations", "group meeting reminders"]}


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


# --- Templates, uses, approval ---

@pytest.mark.parametrize("niche", NICHES)
def test_approved_with_the_listed_uses(niche):
    assert personalise.USES[niche]["approved"] is True
    assert personalise.USES[niche]["uses"] == USES[niche]


@pytest.mark.parametrize("niche", NICHES)
def test_three_variants_with_follow_ups_signature_and_footer(world, niche):
    lead = {"id": 4, "company": "Duka", "industry": niche, "email": "info@duka.co.ke"}
    assert sender.variants(lead) == ["A", "B", "C"]
    for v in "ABC":
        lead["template_variant"] = v
        first, tname = sender.build(lead)
        assert tname == f"{niche}_{v}" and list(first.iter_attachments()) == []
        f1, f1name = sender.build_followup(lead, 1)
        f2, f2name = sender.build_followup(lead, 2)
        assert (f1name, f2name) == (f"followup/{niche}_{v}", f"followup2/{niche}")
        assert f1["Subject"] == f2["Subject"] == "Re: " + first["Subject"]
        for msg in (first, f1, f2):
            text = sender.plain_of(msg)
            assert text.startswith("Hello Duka team,\n\n") and SIGNATURE in text
            assert 'just reply "unsubscribe"' in text and "{" not in text
            assert len(re.findall(r"(?:Mon|Tues|Wednes|Thurs|Fri)day \d+ \w{3} at \d", text)) == 2  # two times


def test_old_single_templates_are_kept_as_legacy():
    for niche in NICHES:
        assert (sender.TEMPLATE_DIR / f"{niche}.txt").is_file()


def test_new_leads_in_these_niches_are_sent_their_variants(world, smtp):
    for i, niche in enumerate(NICHES):
        db.add_lead(f"Co {i}", f"co{i}.co.ke", niche, f"info@co{i}.co.ke", "search")
        db.set_status(db.find_lead_by_email(f"info@co{i}.co.ke")["id"], "verified", "verify")
    sender.run()
    assert len(smtp["delivered"]) == 5
    with db.conn() as c:
        assert sorted(r[0].rsplit("_", 1)[0] for r in c.execute("SELECT template FROM sent")) == sorted(NICHES)


# --- Need score ---

def score(industry="ecommerce", email="info@duka.co.ke", website="duka.co.ke", status="verified", text=""):
    return targeting.need_score({"industry": industry, "email": email, "website": website, "status": status}, text)


def test_starts_at_50_plus_15_for_its_own_domain():
    assert score(text="We sell shoes.") == (65, ["+50 start (ecommerce)", "+15 own website domain (duka.co.ke)"])


@pytest.mark.parametrize("text, points", [
    ("Download our mobile app today", 15), ("Developers: see our API", 15), ("Order online and relax", 15),
    ("Online booking for appointments", 15), ("Online learning for every class", 15),
    ("Pay with M-Pesa", 10), ("Paybill 123456", 10), ("Secure payments", 10),
    ("Trusted by 12,000 customers", 10), ("Over 2 million learners", 10), ("500 patients a week", 10),
    ("3,400 members in 12 counties", 10),
])
def test_each_site_signal(text, points):
    total, reasons = score(text=text)
    assert total == 65 + points, reasons


def test_all_signals_and_the_cap():
    total, _ = score(text="Order online in our app, pay by M-Pesa. 50,000 customers served.")
    assert total == 100


def test_free_email_and_invalid_email():
    total, reasons = score(email="duka@gmail.com", website=None, status="invalid", text=None)
    assert total == 0 and "-20 email is free mail (gmail.com)" in reasons and "-30 email invalid" in reasons
    assert "+10 couldn't check website" not in reasons  # these rules give nothing for an unreadable site


@pytest.mark.parametrize("niche", NICHES)
def test_every_niche_uses_these_rules(niche):
    assert score(industry=niche, text="")[1][0] == f"+50 start ({niche})"


def test_other_industries_keep_their_rules():
    assert score(industry="retail", text="")[1][0] == "+40 start (retail)"
    assert targeting.SCORE_VERSION == 4  # so every lead is scored again with the new rules


# --- Microfinance importer ---

DIRECTORY = """CENTRAL BANK OF KENYA
NO. DIRECTORY OF LICENCED MICROFINANCE BANKS
1. Caritas Microfinance Bank Limited
Postal Address: P. O. Box 15352 - 00100, Nairobi
Telephone: +254 - 020 - 5151500
Email: info@caritas-mfb.co.ke
Website: www.caritas-mfb.co.ke
Physical Address: Cardinal Maurice Otunga Plaza, Ground floor, Kaunda Street, Nairobi
Date Licenced: 02.06.2015
Branches: 2
2. Branch Microfinance Bank Limited
Telephone: +254 711 082 130
E-mail: Kenya@branch.co; finance-ke@branch.co
Website: https://branch.co.ke/
Physical Address: Reliable Towers, 9th Floor, Mogotio Road
Branches: 2
Page 1 of 3
C2: CBK - Official
CENTRAL BANK OF KENYA
NO. DIRECTORY OF LICENCED MICROFINANCE BANKS
3. Umba Microfinance Bank Limited
Telephone:– 0746 555 222
Fax: +254-20-3867504
Email: info@umba.co.ke
Website: www.umba.com
Date Licenced: 12.01.2015
4. Muungano Microfinance Bank PLC
Telephone: 020-4404173 | 0706-974-747
Email: info@muunganomfbank.co.ke
Physical Address: Eastend Mall Kangari Township, Kangari-Githumu Road
Date Licenced: 30.10.2019
Branches: 1
Page 3 of 3
"""
URL = "https://www.centralbank.go.ke/wp-content/uploads/2026/02/Directory-of-Licenced-Microfinance-Banks-Feb-2026.pdf"


def test_reads_every_bank_and_its_details():
    caritas, branch, umba, muungano = targeting.parse_directory(DIRECTORY)
    assert (caritas["name"], caritas["email"], caritas["website"], caritas["licensed"]) == \
        ("Caritas Microfinance Bank Limited", "info@caritas-mfb.co.ke", "caritas-mfb.co.ke", "2015-06-02")
    assert caritas["phone"] == "+254 - 020 - 5151500"
    assert caritas["address"] == "Cardinal Maurice Otunga Plaza, Ground floor, Kaunda Street, Nairobi"
    assert branch["emails"] == ["kenya@branch.co", "finance-ke@branch.co"] and branch["website"] == "branch.co.ke"
    assert umba["phone"] == "0746 555 222" and umba["website"] == "umba.com"  # the directory's own Website line
    assert muungano["website"] == "muunganomfbank.co.ke"  # no Website line: the email's domain
    assert [e["number"] for e in (caritas, branch, umba, muungano)] == [1, 2, 3, 4]


@pytest.fixture
def cbk(world, monkeypatch):
    """CBK's bank supervision page links two directories; the PDF's text is DIRECTORY."""
    page = (f'<a href="https://www.centralbank.go.ke/wp-content/uploads/2022/04/'
            f'Directory-of-Licenced-Microfinance-Banks-April-2022.pdf">old</a><a href="{URL}">new</a>')
    fetched = []

    class Page:
        text = page

        def raise_for_status(self):
            pass

    monkeypatch.setattr(targeting.requests, "get", lambda url, **k: fetched.append(url) or Page())
    monkeypatch.setattr(targeting, "fetch", lambda src: fetched.append(src) or b"%PDF")
    monkeypatch.setattr(targeting, "pdf_text", lambda data: DIRECTORY)
    monkeypatch.setattr(verify, "domain_ok", lambda d: d != "umba.co.ke")
    monkeypatch.setattr(targeting, "swap_emails", lambda: 0)
    return fetched


def test_finds_the_latest_directory_on_cbks_site(cbk):
    assert targeting.latest_microfinance_url() == URL
    assert targeting.microfinance_source(URL) == "CBK MFB directory Feb 2026"


def test_imports_every_bank_as_a_microfinance_lead(cbk):
    targeting.import_microfinance()
    assert cbk == [targeting.CBK_SUPERVISION_URL, URL]
    run, result, added = targeting.last_run()
    assert run["found"] == 4 and len(added) == 4
    assert {l["industry"] for l in added} == {"microfinance"}
    assert {(l["source"], l["source_url"]) for l in added} == {("CBK MFB directory Feb 2026", URL)}
    caritas = next(l for l in added if l["company"].startswith("Caritas"))
    assert (caritas["email"], caritas["phone"], caritas["date_licensed"], caritas["status"]) == \
        ("info@caritas-mfb.co.ke", "+254 - 020 - 5151500", "2015-06-02", "verified")
    assert next(l for l in added if l["company"].startswith("Umba"))["status"] == "invalid"


def test_running_it_again_adds_no_duplicates(cbk):
    db.add_lead("Branch International", "branch.co.ke", "digital_lender", "kenya@branch.co", "search")
    targeting.import_microfinance()
    _, result, added = targeting.last_run()
    assert len(added) == 3 and [d["name"] for d in result["duplicates"]] == ["Branch Microfinance Bank Limited"]
    targeting.import_microfinance()
    _, result, added = targeting.last_run()
    assert added == [] and len(result["duplicates"]) == 4


def test_a_pdf_given_by_path_is_read_instead(cbk):
    targeting.import_microfinance("mfb.pdf", source_url=URL)
    assert cbk == ["mfb.pdf"]  # CBK's page isn't fetched
    _, _, added = targeting.last_run()
    assert {l["source_url"] for l in added} == {URL}
