"""A/B/C template rotation (templates v4): which variant a lead gets, the meeting times every
email suggests, the facts it quotes, and leads from before the rotation finishing with their
old follow-up 1. Temporary database and fake mail server: nothing is sent."""
import random
import re
from datetime import date

import pytest

import config
import crm
import db
import personalise
import ranking
import rotation
import sender

NICHES = ("sacco", "school", "church", "fintech", "digital_lender", "hr_payroll", "saas", "tech_startup")
SIGNATURE = "Kind regards,\nFlorence\nTalkSasa Communications Ltd\n+254 762 000 008\ntalksasa.com\n\n--\n"
SLOT = r"(Monday|Tuesday|Wednesday|Thursday|Friday) \d{1,2} [A-Z][a-z]{2} at (10am|11:30am|2pm|3:30pm)"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    monkeypatch.setattr(config, "FOLLOWUP_AFTER_DAYS", 5)
    monkeypatch.setattr(config, "DAILY_LIMIT", 100)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    monkeypatch.setattr(sender.notify, "send", lambda text: None)
    db.init()


@pytest.fixture
def mail(smtp, monkeypatch):
    """The fake mail server, also keeping each whole message."""
    smtp["messages"] = []
    real = sender.deliver

    def deliver(s, lead, msg, tname, followup=0):
        real(s, lead, msg, tname, followup)
        smtp["messages"].append(msg)

    monkeypatch.setattr(sender, "deliver", deliver)
    return smtp


def waiting(n, industry="sacco"):
    ids = []
    for i in range(n):
        email = f"info@{industry}{i}.co.ke"
        db.add_lead(f"{industry.title()} {i}", f"{industry}{i}.co.ke", industry, email, "search")
        ids.append(db.find_lead_by_email(email)["id"])
        db.set_status(ids[-1], "verified", "verify")
    return ids


def plain(msg):
    return msg.get_body(("plain",)).get_content()


def age_all_sends(days):
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on = date(sent_on, ?)", (f"-{days} days",))


def lead_variant(lead_id):
    with db.conn() as c:
        return c.execute("SELECT template_variant FROM leads WHERE id=?", (lead_id,)).fetchone()[0]


def sent_variants(lead_id):
    with db.conn() as c:
        return [r[0] for r in c.execute("SELECT template_variant FROM sent WHERE lead_id=? ORDER BY id", (lead_id,))]


# --- The templates ---

@pytest.mark.parametrize("niche", NICHES)
def test_every_niche_has_three_variants_and_their_follow_ups(world, niche):
    lead = {"id": 1, "company": "Umoja", "industry": niche, "email": "info@umoja.co.ke"}
    assert sender.variants(lead) == ["A", "B", "C"]
    for v in "ABC":
        lead["template_variant"] = v
        first, tname = sender.build(lead)
        assert tname == f"{niche}_{v}"
        assert list(first.iter_attachments()) == []  # no PDF: {profile_line} instead
        for msg in (first, sender.build_followup(lead, 1)[0], sender.build_followup(lead, 2)[0]):
            text = plain(msg)
            assert SIGNATURE in text and "+254 712 295 880" not in text  # one phone, from SENDER_PHONE
            assert 'just reply "unsubscribe"' in text
            assert "{" not in text and "}" not in text  # every gap filled
            assert len(re.findall(SLOT, text)) == 2 and re.search(r"15[ -]minute", text)
        assert sender.build_followup(lead, 1)[1] == f"followup/{niche}_{v}"
        assert sender.build_followup(lead, 2)[1] == f"followup2/{niche}"


def test_facts_come_from_the_settings(world, monkeypatch):
    monkeypatch.setattr(config, "OFFER_FREE_SMS", "50")
    monkeypatch.setattr(config, "SETUP_TIME", "1 day")
    lead = {"id": 1, "company": "Umoja SACCO", "industry": "sacco", "email": "a@umoja.co.ke", "template_variant": "B"}
    text = plain(sender.build(lead)[0])
    assert "give you 50 free SMS" in text and "from KES 0.25 per SMS" in text
    assert "which usually takes 1 day" in plain(sender.build_followup(lead, 1)[0])


def test_profile_line_and_booking_line(world, monkeypatch):
    lead = {"id": 1, "company": "Umoja SACCO", "industry": "sacco", "email": "a@umoja.co.ke", "template_variant": "A"}
    text = plain(sender.build(lead)[0])
    assert "Or pick any time" not in text and "Company profile" not in text
    assert "If neither works, just tell me which day suits you better.\n\nKind regards," in text  # no gap left
    monkeypatch.setattr(config, "BOOKING_URL", "https://cal.example/talksasa")
    monkeypatch.setattr(config, "PROFILE_URL", "https://talksasa.com/profile.pdf")
    msg = sender.build(lead)[0]
    assert ("which day suits you better.\n\nOr pick any time that suits you here: https://cal.example/talksasa\n\n"
            "Company profile: https://talksasa.com/profile.pdf\n\nKind regards,") in plain(msg)
    assert '<a href="https://cal.example/talksasa">' in sender.html_of(msg)


@pytest.mark.parametrize("niche, first", [("sacco", "at your office"), ("school", "at your office"),
                                          ("church", "at your office"), ("fintech", "on a video call"),
                                          ("saas", "on a video call")])
def test_meet_options_by_niche(niche, first):
    assert rotation.meet_options(niche).startswith(first)


# --- Who gets which variant ---

def test_each_new_lead_gets_the_variant_used_least(world, mail):
    ids = waiting(9)
    sender.run()
    variants = [lead_variant(i) for i in ids]
    assert sorted(variants) == list("AAABBBCCC")
    for i, v in zip(ids, variants):
        assert sent_variants(i) == [v]
    assert [m["Subject"] for m in mail["messages"]][0] in {
        sender.fill(sender.load_template("sacco", v)[1], {"company": "Sacco 0", "industry": "sacco"}) for v in "ABC"}


def test_tie_is_broken_at_random(world, monkeypatch):
    picked = []
    monkeypatch.setattr(rotation.random, "choice", lambda options: picked.append(options) or options[-1])
    assert rotation.pick("sacco", ["A", "B", "C"]) == "C"
    assert picked == [["A", "B", "C"]]


def test_counts_are_per_niche(world, mail):
    for i, v in zip(waiting(2, "school"), "AB"):
        db.record_sent(i, f"<{i}@x>", f"school_{v}", template_variant=v)
    assert rotation.counts("school") == {"A": 1, "B": 1}
    assert rotation.counts("sacco") == {}
    assert rotation.pick("school", ["A", "B", "C"]) == "C"  # the one school hasn't used


def test_a_variant_switched_off_in_uses_yaml_is_never_given(world, mail, monkeypatch):
    monkeypatch.setitem(personalise.USES, "sacco", dict(personalise.USES["sacco"], variants=["A", "B"]))
    ids = waiting(6)
    sender.run()
    assert sorted(lead_variant(i) for i in ids) == list("AAABBB")


def test_every_variant_switched_off_sends_nothing(world, mail, monkeypatch):
    monkeypatch.setitem(personalise.USES, "sacco", dict(personalise.USES["sacco"], variants=[]))
    waiting(1)
    sender.run()
    assert mail["delivered"] == []


def test_follow_ups_keep_the_variant(world, mail, monkeypatch):
    monkeypatch.setitem(personalise.USES, "fintech", dict(personalise.USES["fintech"], variants=["C"]))
    [lead_id] = waiting(1, "fintech")
    sender.run()
    first = mail["messages"][0]
    assert first["Subject"] == "SMS cost check for Fintech 0"
    # switching C off later changes nothing for a lead that already has it
    monkeypatch.setitem(personalise.USES, "fintech", dict(personalise.USES["fintech"], variants=["A"]))
    age_all_sends(10)
    sender.run()
    age_all_sends(10)
    sender.run()
    _, f1, f2 = mail["messages"]
    assert f1["Subject"] == f2["Subject"] == "Re: SMS cost check for Fintech 0"
    assert "Following up on the cost check." in plain(f1)  # fintech C's follow-up 1
    assert "Whether it's a backup route or a lower price per SMS" in plain(f2)  # fintech's follow-up 2
    assert sent_variants(lead_id) == ["C", "C", "C"] and lead_variant(lead_id) == "C"
    with db.conn() as c:
        assert [r[0] for r in c.execute("SELECT template FROM sent ORDER BY id")] == \
            ["fintech_C", "followup/fintech_C", "followup2/fintech"]


# --- Leads from before the rotation ---

def old_lead(industry="sacco", subject="Loan reminders by SMS"):
    db.add_lead("Hurima Sacco", "hurima.co.ke", industry, "info@hurima.co.ke", "search")
    lead_id = db.find_lead_by_email("info@hurima.co.ke")["id"]
    db.record_sent(lead_id, "<first@talksasa.co.ke>", industry, subject, "first email")
    db.set_status(lead_id, "sent", "send")
    age_all_sends(10)
    return lead_id


def test_old_lead_finishes_with_its_old_follow_up_1_and_a_meeting_request(world, mail, monkeypatch):
    monkeypatch.setattr(config, "BOOKING_URL", "https://cal.example/talksasa")
    lead_id = old_lead()
    sender.run()
    [f1] = mail["messages"]
    text = plain(f1)
    assert f1["Subject"] == "Re: Loan reminders by SMS"
    assert re.search(
        r"Just following up on my email about sending loan reminders and deposit confirmations by SMS\. "
        r"If that's something you're looking at, I'd be glad to visit or set up a quick call, whichever suits you\.\n\n"
        r"It takes 15 minutes, at your office if you're in or near Nairobi, on a video call or by phone\. "
        rf"Would {SLOT} or {SLOT} suit you\? If neither works, just tell me which day suits you better\.\n\n"
        r"Or pick any time that suits you here: https://cal\.example/talksasa\n\n"
        r"Kind regards,\nFlorence\nTalkSasa Communications Ltd\n", text)
    assert sent_variants(lead_id) == [None, None] and lead_variant(lead_id) is None
    age_all_sends(10)
    sender.run()
    f2 = mail["messages"][1]
    assert "One last note from me. If reaching members on time" in plain(f2)  # the new follow-up 2
    assert "I won't keep filling your inbox" not in plain(f2)


def test_old_wording_lead_still_gets_the_bulk_sms_line_and_the_meeting_request(world, mail):
    old_lead(subject=None)
    sender.run()
    text = plain(mail["messages"][0])
    assert "Just following up on my earlier email about bulk SMS." in text
    assert "It takes 15 minutes," in text and "loan reminders and deposit" not in text


def test_new_leads_and_old_leads_in_one_batch(world, mail):
    old_id = old_lead()
    [new_id] = waiting(1)
    sender.run()
    assert lead_variant(old_id) is None and lead_variant(new_id) in "ABC"


# --- The facts must be set ---

@pytest.mark.parametrize("key", ["OFFER_FREE_SMS", "SETUP_TIME", "PRICE_FROM"])
def test_nothing_is_sent_while_a_fact_is_missing(world, mail, monkeypatch, capsys, key):
    monkeypatch.setattr(config, key, "")
    waiting(2)
    old_lead()
    sender.run()
    assert mail["delivered"] == []
    out = capsys.readouterr().out
    assert f"WARNING: Not sending: {key} is empty in .env" in out
    with pytest.raises(RuntimeError, match=key):  # and deliver itself refuses, whoever calls it
        sender.deliver(None, db.find_lead_by_email("info@sacco0.co.ke"), None, "sacco_A")


def test_dry_run_warns_about_a_missing_fact(world, mail, monkeypatch, capsys):
    monkeypatch.setattr(config, "PRICE_FROM", "")
    waiting(1)
    sender.run(dry_run=True)
    assert "a real send would refuse to send: PRICE_FROM is empty" in capsys.readouterr().out


# --- Meeting times ---

def test_slots_skip_weekends_and_holidays():
    # Wed 2026-12-23: Christmas is a Friday; Boxing Day is a Saturday, which gives no extra day off
    on = date(2026, 12, 23)
    assert rotation.working_days_after(on, 2) == date(2026, 12, 28)  # Thu 24, (Fri 25 off), Mon 28
    days = {rotation.working_days_after(on, n) for n in range(2, 8)}
    assert all(d.weekday() < 5 and d not in rotation.holidays(d.year) for d in days)
    assert min(days) == date(2026, 12, 28) and max(days) == date(2027, 1, 5)  # skips 1 Jan


def test_holiday_on_a_sunday_gives_the_monday_off():
    assert date(2027, 12, 12).weekday() == 6  # Jamhuri Day 2027 is a Sunday
    assert date(2027, 12, 13) in rotation.holidays(2027)
    assert {date(2026, 4, 3), date(2026, 4, 6)} <= rotation.holidays(2026)  # Good Friday, Easter Monday


def test_two_slots_on_different_days_in_the_window():
    on = date(2026, 10, 28)  # a Wednesday: the window is Fri 30 Oct to Fri 6 Nov
    for lead_id in range(200):
        s1, s2 = rotation.slots(lead_id, 1, on)
        assert re.fullmatch(SLOT, s1) and re.fullmatch(SLOT, s2)
        d1, d2 = (s.split(" at ")[0] for s in (s1, s2))
        assert d1 != d2
        days = [int(s.split()[1]) for s in (s1, s2)]
        assert all(d in (30, 2, 3, 4, 5, 6) for d in days)
    assert rotation.slots(5, 1, on) == rotation.slots(5, 1, on)  # the same email, the same times


def test_slots_are_spread_across_leads():
    on = date(2026, 10, 28)
    pairs = [rotation.slots(i, 1, on) for i in range(1, 46)]  # a day's batch
    assert len(set(pairs)) >= 35
    assert len({p[0] for p in pairs}) >= 10


def test_slot_format_and_meeting_times(monkeypatch):
    assert rotation.slot_text(date(2026, 11, 3), "10am") == "Tuesday 3 Nov at 10am"
    monkeypatch.setattr(config, "MEETING_TIMES", " 9am , 4pm ")
    assert rotation.meeting_times() == ["9am", "4pm"]
    monkeypatch.setattr(config, "MEETING_TIMES", "")
    assert rotation.meeting_times() == ["10am", "11:30am", "2pm", "3:30pm"]


def test_today_is_nairobi_time():
    assert rotation.NAIROBI.utcoffset(None).total_seconds() == 3 * 3600


# --- What's working and the CRM ---

def test_report_by_template_and_by_technique(world):
    admin = {"id": 1, "role": "admin"}
    crm.ensure_schema()
    ids = waiting(4) + waiting(2, "fintech")
    for i, (v, status) in zip(ids, [("A", "replied"), ("A", "sent"), ("B", "meeting"), ("C", "sent"),
                                    ("A", "sent"), ("B", "sent")]):
        db.record_sent(i, f"<{i}@x>", "t", template_variant=v)
        db.set_status(i, status, "test")
    old_lead()  # no variant: in neither table
    r = crm.report(0, admin)
    assert r["total"]["emailed"] == 7
    template = {g["name"]: g for g in r["groups"]["template"]}
    assert list(template) == ["fintech · A", "fintech · B", "sacco · A", "sacco · B", "sacco · C"]
    assert template["sacco · A"]["emailed"] == 2 and template["sacco · B"]["meetings"] == 1
    technique = {g["name"]: g for g in r["groups"]["technique"]}
    assert list(technique) == ["A · Quick question", "B · Free test", "C · Niche angle"]
    assert technique["A · Quick question"]["emailed"] == 3
    assert technique["B · Free test"]["interested"] == 1 and technique["B · Free test"]["meetings"] == 1
    assert all(g["small_sample"] for g in r["groups"]["technique"])  # "too few to judge"


def test_crm_lists_and_edits_the_variants(world, tmp_path, monkeypatch):
    import shutil
    templates = tmp_path / "templates"
    shutil.copytree(sender.TEMPLATE_DIR, templates)
    monkeypatch.setattr(crm, "TEMPLATE_DIR", templates)
    monkeypatch.setattr(crm, "TEMPLATE_KINDS", {"first": templates, "followup": templates / "followup",
                                                "followup2": templates / "followup2"})
    monkeypatch.setattr(sender, "TEMPLATE_DIR", templates)
    monkeypatch.setattr(sender, "FOLLOWUP_DIR", templates / "followup")
    monkeypatch.setattr(sender, "FOLLOWUP2_DIR", templates / "followup2")
    monkeypatch.setitem(personalise.USES, "sacco", dict(personalise.USES["sacco"], variants=["A", "B"]))
    crm.ensure_schema()
    with crm.conn() as c:
        uid = c.execute("INSERT INTO users (name, email, role, password_hash, must_change) VALUES (?,?,?,?,0)"
                        " RETURNING id", ("Joyce", "joyce@example.com", "admin", "x")).fetchone()[0]
    admin = {"id": uid, "role": "admin"}
    listed = {t["name"]: t for t in crm.list_templates(admin)["templates"]}
    assert "sacco_A" not in listed  # a variant is part of its industry, not an industry
    assert [(v["variant"], v["kinds"], v["enabled"]) for v in listed["sacco"]["variants"]] == \
        [("A", ["first", "followup"], True), ("B", ["first", "followup"], True), ("C", ["first", "followup"], False)]
    assert listed["default"]["variants"] == []
    t = crm.get_template("first", "sacco_B", admin)
    assert t["subject"] == "Free SMS test for {company}" and "slot1" in t["fields"]
    prev = crm.preview_template("first", "sacco_B", crm.TemplateIn(subject=t["subject"], body=t["body"]), admin)
    assert prev["problem"] is None and prev["attachments"] == [] and "give you 500 free SMS" in prev["text"]
    f1 = crm.preview_template("followup", "sacco_B", crm.TemplateIn(body="Hi {company}, {slot1}?"), admin)
    assert f1["problem"] is None and f1["subject"] == "Re: Free SMS test for Example Company"
    legacy = crm.get_template("followup", "sacco", admin)
    shown = crm.preview_template("followup", "sacco", crm.TemplateIn(body=legacy["body"]), admin)
    assert "It takes 15 minutes," in shown["text"]  # the legacy follow-up 1 as old leads get it
    crm.save_template("followup", "sacco_C", crm.TemplateIn(body="Hello {company} team,\n\nNew C text."), admin)
    msg, _ = sender.build_followup({"id": None, "company": "Umoja", "industry": "sacco", "email": "a@u.co.ke",
                                    "template_variant": "C"}, 1)
    assert "New C text." in plain(msg)
    with pytest.raises(crm.HTTPException):
        crm.get_template("followup2", "sacco_A", admin)  # follow-up 2 is one per industry


@pytest.mark.parametrize("website, shown", [("https://talksasa.com", "talksasa.com"),
                                            ("https://www.talksasa.com/", "talksasa.com"),
                                            ("http://www.talksasa.co.ke/sms", "talksasa.co.ke/sms")])
def test_website_shows_as_its_address_and_links_to_it(world, monkeypatch, website, shown):
    monkeypatch.setattr(config, "WEBSITE", website)
    msg, _ = sender.build({"id": 1, "company": "Umoja", "industry": "sacco", "email": "a@u.co.ke",
                           "template_variant": "A"})
    assert f"+254 762 000 008\n{shown}\n\n--\n" in plain(msg)
    assert f'<a href="{website}">{shown}</a>' in sender.html_of(msg)
    assert ">TalkSasa<" not in sender.html_of(msg)
