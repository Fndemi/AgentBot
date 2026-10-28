"""An industry paused with paused_until in uses.yaml gets no first emails and no follow-ups
until that date, its leads stay exactly as they are, and on that date sending resumes by
itself where each lead left off. Temporary database and fake mail server: nothing is sent."""
from datetime import date
from pathlib import Path

import pytest
import yaml

import config
import db
import personalise
import ranking
import rotation
import sender

UNTIL = date(2027, 1, 5)


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
    monkeypatch.setitem(personalise.USES, "school", dict(personalise.USES["school"], paused_until=UNTIL))
    db.init()


def on(monkeypatch, day):
    monkeypatch.setattr(rotation, "today", lambda: day)


def lead(email, industry, emails=0):
    """A lead waiting for its first email (emails=0), or at 'sent' after that many emails, the
    last one long enough ago for the next follow-up to be due."""
    db.add_lead("Test", email.split("@")[1], industry, email, "search")
    lead_id = db.find_lead_by_email(email)["id"]
    db.set_status(lead_id, "verified", "verify")
    for i in range(emails):
        db.record_sent(lead_id, f"<{lead_id}.{i}@talksasa.co.ke>", industry, "Fee reminders by SMS", "x")
    if emails:
        db.set_status(lead_id, "sent", "send")
        with db.conn() as c:
            c.execute("UPDATE sent SET sent_on = date('now', '-20 days') WHERE lead_id = ?", (lead_id,))
    return lead_id


def state(lead_id):
    with db.conn() as c:
        row = c.execute("SELECT status, template_variant FROM leads WHERE id=?", (lead_id,)).fetchone()
    return row["status"], row["template_variant"], db.sent_count(lead_id)


def test_uses_yaml_pauses_school_until_5_january_2027():
    raw = yaml.safe_load((Path(sender.__file__).parent / "uses.yaml").read_text(encoding="utf-8"))
    assert raw["school"]["paused_until"] == UNTIL


def test_paused_industry_gets_nothing_and_its_leads_stay_as_they_are(world, smtp, monkeypatch, capsys):
    on(monkeypatch, date(2027, 1, 4))  # the day before it ends
    new, sent1, sent2 = (lead("info@newschool.ac.ke", "school"), lead("info@f1school.ac.ke", "school", 1),
                         lead("info@f2school.ac.ke", "school", 2))
    sacco = lead("info@umoja.co.ke", "sacco")
    before = {i: state(i) for i in (new, sent1, sent2)}
    sender.run()
    assert smtp["delivered"] == ["info@umoja.co.ke"]  # other industries go on as usual
    assert {i: state(i) for i in (new, sent1, sent2)} == before
    assert "school paused until 2027-01-05" in capsys.readouterr().out
    assert state(sacco)[0] == "sent"


def test_dry_run_says_so(world, smtp, monkeypatch, capsys):
    on(monkeypatch, date(2026, 10, 28))
    lead("info@newschool.ac.ke", "school")
    sender.run(dry_run=True)
    out = capsys.readouterr().out
    assert "school paused until 2027-01-05" in out and "Nothing to send" in out
    assert smtp["delivered"] == []


def test_resumes_by_itself_on_the_date_where_each_lead_left_off(world, smtp, monkeypatch, capsys):
    new, sent1, sent2 = (lead("info@newschool.ac.ke", "school"), lead("info@f1school.ac.ke", "school", 1),
                         lead("info@f2school.ac.ke", "school", 2))
    on(monkeypatch, UNTIL)
    sender.run()
    assert sorted(smtp["delivered"]) == ["info@f1school.ac.ke", "info@f2school.ac.ke", "info@newschool.ac.ke"]
    assert state(new)[0] == "sent" and state(new)[2] == 1          # its first email
    assert state(sent1)[2] == 2 and state(sent2)[2] == 3            # follow-up 1, follow-up 2
    assert "paused until" not in capsys.readouterr().out


def test_deliver_refuses_a_paused_industry(world, monkeypatch):
    on(monkeypatch, date(2026, 12, 1))
    lead_id = lead("info@newschool.ac.ke", "school")
    with pytest.raises(RuntimeError, match="school is paused until 2027-01-05"):
        sender.deliver(None, db.find_lead_by_email("info@newschool.ac.ke"), None, "school_A")
    assert state(lead_id)[2] == 0


def test_a_date_that_isnt_one_is_refused(monkeypatch):
    monkeypatch.setitem(personalise.USES, "school", dict(personalise.USES["school"], paused_until="5 Jan"))
    with pytest.raises(ValueError, match="isn't a date like 2027-01-05"):
        personalise.paused({"industry": "school"}, date(2026, 12, 1))
