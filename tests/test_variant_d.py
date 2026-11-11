"""Variant D, modelled on our best-performing email: plain text with no links at all (no
HTML part, no booking line, no profile line, no website; the phone number in the signature
only), written from each niche's about, noun and sms_uses in uses.yaml. Its follow-up 1 is
plain too; follow-up 2 stays shared. D joins A/B/C with the same share of new leads from
the day it starts, and the CRM report counts replies per variant. Temporary database and
fake mail server: nothing is sent."""
import re
from datetime import date, timedelta

import pytest

import config
import crm
import db
import personalise
import ranking
import rotation
import sender

APPROVED = sorted(k for k, v in personalise.USES.items() if k != "default" and v.get("approved"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    monkeypatch.setattr(config, "DAILY_LIMIT", 100)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(config, "BOOKING_URL", "https://cal.example.com/talksasa")
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    monkeypatch.setattr(sender.notify, "send", lambda text: None)
    db.init()


def lead(niche="sacco", **extra):
    return {"id": 7, "company": "Umoja Sacco Society Ltd", "industry": niche, "email": "info@umoja.co.ke",
            "template_variant": "D", **extra}


def plain(msg):
    return msg.get_body(("plain",)).get_content()


NO_LINKS = re.compile(r"https?://|www\.|\.com\b|\.co\.ke\b|cal\.example|profile", re.I)


# --- The templates and their words ---

@pytest.mark.parametrize("niche", APPROVED)
def test_every_approved_niche_has_d_and_its_words(niche):
    assert "D" in sender.variants({"industry": niche})
    assert (sender.FOLLOWUP_DIR / f"{niche}_D.txt").is_file()
    entry = personalise.USES[niche]
    assert entry["about"] and entry["noun"].split()[0] in ("a", "an")
    assert 4 <= len(entry["sms_uses"]) <= 5
    assert all(" and " not in u for u in entry["sms_uses"][:2])   # "sends a and b" reads cleanly


def test_niches_not_approved_have_no_d():
    for niche, entry in personalise.USES.items():
        if not entry.get("approved"):
            assert "D" not in sender.variants({"industry": niche}), niche


@pytest.mark.parametrize("items, joined", [(["a"], "a"), (["a", "b"], "a and b"),
                                           (["a", "b", "c", "d", "e"], "a, b, c, d and e")])
def test_uses_are_joined_with_commas_and_and(items, joined):
    assert sender.join_and(items) == joined


# --- The first email ---

def test_d_first_email_word_for_word(world):
    msg, tname = sender.build(lead())
    assert tname == "sacco_D" and msg["Subject"] == "SMS for Umoja Sacco"
    assert plain(msg) == (
        "Hello,\n\n"
        "I came across Umoja Sacco and the savings and loan services you provide to your members.\n\n"
        f"I'm {config.SENDER_NAME} from TalkSasa Communications, a Nairobi-based Bulk SMS provider. We help "
        "businesses automate customer communications through Bulk SMS and API integration.\n\n"
        "For a SACCO like Umoja Sacco, SMS could support loan repayment reminders, deposit confirmations, "
        "dividend notices, AGM notices and OTPs for mobile banking.\n\n"
        "We also provide delivery reporting and free API integration support to help you connect our SMS "
        "gateway to your existing system.\n\n"
        "Would you be available for a brief 20-minute meeting to explore how TalkSasa could support "
        "Umoja Sacco's communication needs?\n\n"
        f"Kind regards,\n{config.SENDER_NAME}\nTalkSasa Communications Ltd\n{config.SENDER_PHONE}\n\n"
        "--\nIf you'd rather not hear from us, just reply \"unsubscribe\" and we won't email you again.\n")


@pytest.mark.parametrize("niche", APPROVED)
def test_d_is_plain_text_with_no_links(world, niche):
    for msg in (sender.build(lead(niche))[0], sender.build_followup(lead(niche), 1)[0]):
        assert msg.get_body(("html",)) is None and not msg.is_multipart()
        text = plain(msg)
        assert not NO_LINKS.search(text), NO_LINKS.search(text).group(0)
        assert text.count(config.SENDER_PHONE) == 1 and "{" not in text and "}" not in text
        assert "20-minute" in text and "15 minutes" not in text


def test_a_named_contact_is_greeted_by_first_name(world):
    named = lead(contact_name="Jane Wanjiru", contact_email="info@umoja.co.ke")
    assert plain(sender.build(named)[0]).startswith("Hello Jane,\n\n")
    assert plain(sender.build_followup(named, 1)[0]).startswith("Hello Jane,\n\n")


def test_water_company_and_property_company_words(world):
    water = plain(sender.build(lead("water_utility", company="Kikuyu Water Company Limited"))[0])
    assert "I came across Kikuyu Water and the water services you provide to your customers." in water
    assert "For a water company like Kikuyu Water, SMS could support bill reminders, water interruption " \
           "notices, payment confirmations, meter reading alerts and new connection updates." in water
    prop = plain(sender.build(lead("proptech", company="Advent Valuers Limited"))[0])
    assert "For a property company like Advent Valuers, SMS could support rent reminders," in prop
    assert "could support Advent Valuers' communication needs?" in prop   # not "Valuers's"


# --- Follow-ups ---

def test_d_follow_up_one_word_for_word_and_follow_up_two_shared(world):
    first, _ = sender.build(lead())
    f1, f1name = sender.build_followup(dict(lead(), sent_subject=first["Subject"]), 1)
    assert f1name == "followup/sacco_D" and f1["Subject"] == "Re: SMS for Umoja Sacco"
    assert plain(f1) == (
        "Hello,\n\n"
        "Just following up on my earlier email. I'd be interested to understand how Umoja Sacco currently sends "
        "loan repayment reminders and deposit confirmations, and whether TalkSasa could support it.\n\n"
        "Would a short 20-minute call next week work for you?\n\n"
        f"Kind regards,\n{config.SENDER_NAME}\nTalkSasa Communications Ltd\n{config.SENDER_PHONE}\n\n"
        "--\nIf you'd rather not hear from us, just reply \"unsubscribe\" and we won't email you again.\n")
    f2, f2name = sender.build_followup(dict(lead(), sent_subject=first["Subject"]), 2)
    assert f2name == "followup2/sacco"   # the same follow-up 2 as A, B and C, HTML and all
    assert f2.get_body(("html",)) is not None


def test_other_variants_keep_their_html(world):
    for v in "ABC":
        assert sender.build(lead(template_variant=v))[0].get_body(("html",)) is not None


# --- Its share of new leads ---

def waiting(n, industry="sacco", start=0):
    ids = []
    for i in range(start, start + n):
        email = f"info@{industry}{i}.co.ke"
        db.add_lead(f"{industry.title()} {i}", f"{industry}{i}.co.ke", industry, email, "search")
        ids.append(db.find_lead_by_email(email)["id"])
        db.set_status(ids[-1], "verified", "verify")
    return ids


def variant_of(lead_id):
    with db.conn() as c:
        return c.execute("SELECT template_variant FROM leads WHERE id=?", (lead_id,)).fetchone()[0]


def history(per_variant, days_ago=30):
    """Leads first emailed `days_ago` days ago, per_variant of each of A, B and C."""
    day = (date.today() - timedelta(days=days_ago)).isoformat()
    for v in "ABC":
        for i in waiting(per_variant, start=100 + "ABC".index(v) * per_variant):
            db.record_sent(i, f"<{i}@x>", f"sacco_{v}", template_variant=v)
            with db.conn() as c:
                c.execute("UPDATE leads SET status='sent', template_variant=? WHERE id=?", (v, i))
                c.execute("UPDATE sent SET sent_on=? WHERE lead_id=?", (day, i))


def test_d_gets_the_same_share_not_every_lead_until_it_catches_up(world, smtp):
    history(30)   # A, B and C have 30 leads each from before D
    ids = waiting(8)
    sender.run()
    assert sorted(variant_of(i) for i in ids) == list("AABBCCDD")


def test_once_d_has_started_shares_are_evened_from_its_first_day(world, smtp):
    history(30)
    sender.run() if waiting(4) else None            # today: one each
    assert rotation.counts("sacco", rotation.balance_from("sacco", list("ABCD"))) == dict.fromkeys("ABCD", 1)
    assert rotation.counts("sacco") == {"A": 31, "B": 31, "C": 31, "D": 1}


def test_a_niche_limited_to_some_variants_gets_d_alongside_them(world, smtp):
    assert personalise.USES["software_agency"]["variants"] == ["A", "B", "D"]
    ids = waiting(6, "software_agency")
    sender.run()
    assert sorted(variant_of(i) for i in ids) == list("AABBDD")


def test_d_emails_go_out_plain_and_are_recorded_as_d(world, smtp, monkeypatch):
    monkeypatch.setattr(rotation, "pick", lambda niche, available, rng=None: "D")
    [lead_id] = waiting(1)
    sender.run()
    assert variant_of(lead_id) == "D"
    with db.conn() as c:
        row = c.execute("SELECT template, template_variant, sent_body FROM sent WHERE lead_id=?", (lead_id,)).fetchone()
    assert (row["template"], row["template_variant"]) == ("sacco_D", "D")
    assert "SMS could support" in row["sent_body"] and not NO_LINKS.search(row["sent_body"])


# --- Replies per variant in the CRM report ---

def test_crm_report_counts_replies_per_variant_d_included(world):
    crm.ensure_schema()
    ids = waiting(5)
    for i, (v, status) in zip(ids, [("D", "replied"), ("D", "sent"), ("D", "meeting"), ("A", "sent"), ("A", "replied")]):
        db.record_sent(i, f"<{i}@x>", f"sacco_{v}", template_variant=v)
        with db.conn() as c:
            c.execute("UPDATE leads SET template_variant=? WHERE id=?", (v, i))
        db.set_status(i, status, "test")
    with db.conn() as c:
        c.execute("INSERT INTO replies (lead_id, from_email, label) VALUES (?, 'x@y', 'interested')", (ids[0],))
    r = crm.report(0, {"id": 1, "role": "admin"})
    technique = {g["name"]: g for g in r["groups"]["technique"]}
    assert list(technique) == ["A · Quick question", "D · Plain intro"]
    d = technique["D · Plain intro"]
    assert (d["emailed"], d["replied"], d["meetings"]) == (3, 1, 1) and d["reply_rate"] == pytest.approx(33.3)
    assert {g["name"] for g in r["groups"]["template"]} == {"sacco · A", "sacco · D"}


def test_crm_template_editor_accepts_ds_gaps():
    for gap in ("about", "a_niche_noun", "uses_list", "first_two_uses"):
        assert gap in crm.TEMPLATE_FIELDS
