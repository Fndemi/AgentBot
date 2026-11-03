"""Named-contact campaigns (campaigns.py, python main.py send-group FILE): entries are sent word for
word, once, at most GROUP_DAILY_LIMIT a day counted toward DAILY_LIMIT, from Mary at the bot's own
mailbox, after their address is checked; each becomes a lead in the campaign, which the bot's
templates and follow-ups never reach, nor anyone on its domains; its one follow-up goes out in the
same thread after 4 working days without a reply; the CRM report compares campaigns with the
bot's own emails. Mail server, DNS and websites are fakes: nothing is sent or looked up."""
from datetime import date

import pytest
import yaml

import auto
import campaigns
import config
import crm
import db
import discover
import ranking
import rotation
import sender

TUESDAY = date(2026, 11, 3)


class FakeSMTP:
    def __init__(self, refuse=()):
        self.sent, self.refuse = [], set(refuse)

    def send_message(self, msg):
        if msg["To"] in self.refuse:
            import smtplib
            raise smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"no such user")})
        self.sent.append(msg)

    def quit(self):
        pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 25)
    monkeypatch.setattr(config, "GROUP_DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "GROUP_FROM_NAME", "Mary from TalkSasa")
    monkeypatch.setattr(config, "GROUP_FROM_EMAIL", "sales@talksasa.co.ke")
    monkeypatch.setattr(campaigns, "SEND_DELAY_SECONDS", 0)
    filed = []
    monkeypatch.setattr(sender, "save_to_sent", lambda msg, at: filed.append(msg["Message-ID"]) or True)
    bad_domains = set()
    monkeypatch.setattr(campaigns, "check", lambda e: {"domain_ok": db.email_domain(e["to"]) not in bad_domains,
                                                       "page": f"https://{db.email_domain(e['to'])}/contact"})
    db.init()
    smtp = FakeSMTP()
    return {"tmp": tmp_path, "smtp": smtp, "filed": filed, "bad": bad_domains,
            "connect": lambda: smtp}


def entry(n, **kw):
    return dict({"to": f"jane{n}@acme{n}.co.ke", "contact_name": f"Jane {n}", "company": f"Acme {n}",
                 "website": f"acme{n}.co.ke", "subject": f"A note for Acme {n}",
                 "body": f"Hello Jane,\n\nWe met at the expo.  Two spaces kept.\n\nMary", "followup_body":
                 f"Hello Jane,\n\nJust checking you saw my note.\n\nMary"}, **kw)


def campaign_file(world, entries, name="group_a.yaml"):
    path = world["tmp"] / name
    path.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    return path


def send(world, path, dry_run=False):
    return campaigns.send_group(path, dry_run=dry_run, connect=world["connect"])


def lead(address):
    return dict(db.find_lead_by_email(address))


# --- The file ---

def test_the_campaign_is_named_after_its_file():
    assert campaigns.campaign_name("campaigns/group_a.yaml") == "Group A"
    assert campaigns.campaign_name("campaigns/fintech_founders.yaml") == "Fintech Founders"


def test_unknown_fields_are_refused(world):
    path = campaign_file(world, [dict(entry(1), cc="x@y.co.ke")])
    with pytest.raises(ValueError, match="unknown fields: cc"):
        campaigns.load(path)


def test_the_committed_group_a_file_loads_and_nothing_incomplete_is_sent(world):
    entries = campaigns.load("campaigns/group_a.yaml")
    assert [e["to"] for e in entries] == ["stephen@groundworkpm.com", "info@boxleocourier.com",
                                          "pmunyua@kilimoafya.com", "newton@bomahut.com"]
    outcome = send(world, "campaigns/group_a.yaml")
    # only complete entries go out (here to a fake mail server); the others wait for their text
    complete = [e["to"] for e in entries if not campaigns.missing(e)]
    assert [m["To"] for m in world["smtp"].sent] == complete
    assert all(outcome[e["to"]].startswith("incomplete") for e in entries if e["to"] not in complete)
    bomahut = {e["to"]: e for e in entries}["newton@bomahut.com"]
    assert bomahut["subject"] == "A second SMS route for Bomahut" and bomahut["body"].startswith("Hi Newton,\n\n")


# --- Sending ---

def test_entries_go_out_word_for_word_from_mary(world):
    path = campaign_file(world, [entry(1)])
    assert send(world, path) == {"jane1@acme1.co.ke": "sent"}
    [msg] = world["smtp"].sent
    assert msg["From"] == "Mary from TalkSasa <sales@talksasa.co.ke>" and msg["To"] == "jane1@acme1.co.ke"
    assert msg["Subject"] == "A note for Acme 1"
    assert msg.get_content() == entry(1)["body"] + "\n"
    assert msg["List-Unsubscribe"] == "<mailto:sales@talksasa.co.ke?subject=unsubscribe>"
    assert msg.get_content_type() == "text/plain" and not msg.is_multipart()
    assert world["filed"] == [msg["Message-ID"]]                                    # a copy in Sent
    l = lead("jane1@acme1.co.ke")
    assert (l["status"], l["industry"], l["campaign"], l["company"], l["contact_name"]) == \
        ("sent", "named_contact", "Group A", "Acme 1", "Jane 1")
    with db.conn() as c:
        row = c.execute("SELECT message_id, sent_subject, template FROM sent").fetchone()
    assert tuple(row) == (msg["Message-ID"], "A note for Acme 1", "campaign:Group A")


def test_only_entries_not_sent_yet_go_out(world):
    path = campaign_file(world, [entry(1), entry(2)])
    send(world, path)
    campaign_file(world, [entry(1), entry(2), entry(3)])                            # one added later
    outcome = send(world, path)
    assert outcome == {"jane1@acme1.co.ke": "already sent", "jane2@acme2.co.ke": "already sent",
                       "jane3@acme3.co.ke": "sent"}
    assert [m["To"] for m in world["smtp"].sent] == ["jane1@acme1.co.ke", "jane2@acme2.co.ke", "jane3@acme3.co.ke"]


def test_at_most_group_daily_limit_a_day_counted_toward_daily_limit(world, monkeypatch):
    monkeypatch.setattr(config, "GROUP_DAILY_LIMIT", 2)
    path = campaign_file(world, [entry(n) for n in range(1, 5)])
    outcome = send(world, path)
    assert list(outcome.values()).count("sent") == 2
    assert outcome["jane3@acme3.co.ke"].startswith("waiting: today's limit")
    assert db.sent_today() == 2                                                     # they count toward DAILY_LIMIT
    monkeypatch.setattr(config, "GROUP_DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "DAILY_LIMIT", 3)                                   # only one slot left today
    assert list(send(world, path).values()).count("sent") == 1


def test_an_address_whose_domain_fails_is_skipped(world):
    world["bad"].add("acme2.co.ke")
    outcome = send(world, campaign_file(world, [entry(1), entry(2)]))
    assert outcome["jane2@acme2.co.ke"].startswith("skipped: its domain receives no mail")
    assert db.find_lead_by_email("jane2@acme2.co.ke") is None


def test_a_dry_run_sends_and_saves_nothing(world, capsys):
    outcome = send(world, campaign_file(world, [entry(1), dict(entry(2), body="")]), dry_run=True)
    assert outcome == {"jane1@acme1.co.ke": "would send", "jane2@acme2.co.ke": "incomplete: no body"}
    assert world["smtp"].sent == [] and db.find_lead_by_email("jane1@acme1.co.ke") is None
    assert db.do_not_contact_domains() == set()
    out = capsys.readouterr().out
    assert "jane1@acme1.co.ke" in out and "yes, https://acme1.co.ke/contact" in out


def test_someone_the_bot_already_emailed_is_not_sent_again(world):
    db.add_lead("Acme 1", "acme1.co.ke", "fintech", "jane1@acme1.co.ke", "search")
    lead_id = db.find_lead_by_email("jane1@acme1.co.ke")["id"]
    db.record_sent(lead_id, "<bot@x>", "fintech_A")
    assert send(world, campaign_file(world, [entry(1)])) == {"jane1@acme1.co.ke": "already emailed by LeadBot"}


def test_a_refused_address_is_marked_bounced(world):
    world["smtp"].refuse.add("jane1@acme1.co.ke")
    assert send(world, campaign_file(world, [entry(1)])) == {"jane1@acme1.co.ke": "bounced"}
    assert lead("jane1@acme1.co.ke")["status"] == "bounced"


# --- Never the bot's emails, to them or their domains ---

def test_the_bot_leaves_campaign_leads_and_their_domains_alone(world):
    db.add_lead("Acme 1 sales", "acme1.co.ke", "fintech", "sales@acme1.co.ke", "search")
    db.set_status(db.find_lead_by_email("sales@acme1.co.ke")["id"], "verified", "verify")
    db.add_lead("Elsewhere", "other.co.ke", "fintech", "info@other.co.ke", "search")
    db.set_status(db.find_lead_by_email("info@other.co.ke")["id"], "verified", "verify")
    send(world, campaign_file(world, [entry(1), dict(entry(2), to="jane@gmail.com")]))
    assert db.do_not_contact_domains() == {"acme1.co.ke", "acme2.co.ke"}            # never gmail.com
    assert [l["email"] for l in ranking.waiting()] == ["info@other.co.ke"]
    for address in ("jane1@acme1.co.ke", "sales@acme1.co.ke"):
        with pytest.raises(RuntimeError, match="campaign|do-not-contact"):
            sender.deliver(FakeSMTP(), dict(lead(address)), None, "fintech_A")
    assert db.awaiting_followup(1) == []                                           # no bot follow-up either


def test_discovery_skips_do_not_contact_domains(world, monkeypatch):
    db.add_do_not_contact("acme1.co.ke", "Group A campaign")
    tally = discover.Tally()
    assert discover.consider("https://www.acme1.co.ke/", "fintech", "q", set(), tally) is None
    assert tally.dropped == {"do-not-contact list": 1}


# --- The one follow-up ---

def sent_days_ago(address, days):
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on = date(?, ?) WHERE lead_id = (SELECT id FROM leads WHERE email = ?)",
                  (TUESDAY.isoformat(), f"-{days} days", address))


def test_the_follow_up_goes_once_after_four_working_days_in_the_same_thread(world, monkeypatch):
    monkeypatch.setattr(rotation, "today", lambda: TUESDAY)
    send(world, campaign_file(world, [entry(1)]))
    first = world["smtp"].sent[0]
    sent_days_ago("jane1@acme1.co.ke", 4)                    # Friday 30 Oct: 2 working days ago
    assert campaigns.followups_due() == []
    sent_days_ago("jane1@acme1.co.ke", 6)                    # Wednesday 28 Oct: 4 working days ago
    assert campaigns.send_followups(connect=world["connect"]) == 1
    followup = world["smtp"].sent[1]
    assert followup["Subject"] == "Re: A note for Acme 1"
    assert followup["In-Reply-To"] == first["Message-ID"] and followup["References"] == first["Message-ID"]
    assert followup.get_content() == entry(1)["followup_body"] + "\n"
    assert campaigns.send_followups(connect=world["connect"]) == 0                  # once only
    assert lead("jane1@acme1.co.ke")["status"] == "sent"


def test_no_follow_up_after_a_reply(world, monkeypatch):
    monkeypatch.setattr(rotation, "today", lambda: TUESDAY)
    send(world, campaign_file(world, [entry(1)]))
    sent_days_ago("jane1@acme1.co.ke", 10)
    lead_id = lead("jane1@acme1.co.ke")["id"]
    db.add_reply(lead_id, "jane1@acme1.co.ke", "Re: A note", "Yes, let's talk", "interested", "", "uid1")
    db.set_status(lead_id, "replied", "inbox")
    assert campaigns.followups_due() == []


def test_the_bot_sends_due_follow_ups_with_every_send(monkeypatch):
    calls = []
    monkeypatch.setattr(campaigns, "send_followups", lambda: calls.append("campaign follow-ups") or 0)
    monkeypatch.setattr(sender, "run", lambda: calls.append("bot send"))
    monkeypatch.setattr(db, "sent_today", lambda: 0)
    auto._send()
    assert calls == ["campaign follow-ups", "bot send"]


# --- The CRM ---

def test_the_crm_report_compares_campaigns_with_the_bots_emails(world):
    crm.ensure_schema()
    send(world, campaign_file(world, [entry(1), entry(2)]))
    lead_id = lead("jane1@acme1.co.ke")["id"]
    db.add_reply(lead_id, "jane1@acme1.co.ke", "Re: A note", "Yes", "interested", "", "uid1")
    db.set_status(lead_id, "meeting", "crm")
    db.add_lead("Bot lead", "bot.co.ke", "fintech", "info@bot.co.ke", "search")
    db.record_sent(db.find_lead_by_email("info@bot.co.ke")["id"], "<b@x>", "fintech_A")
    r = crm.report(0, {"id": 1, "role": "admin"})
    groups = {g["name"]: g for g in r["groups"]["campaign"]}
    assert list(groups) == [crm.BOT_CAMPAIGN, "Group A"]
    assert (groups["Group A"]["emailed"], groups["Group A"]["replied"], groups["Group A"]["interested"],
            groups["Group A"]["meetings"]) == (2, 1, 1, 1)
    assert groups[crm.BOT_CAMPAIGN]["emailed"] == 1
    assert "campaign" in crm.SALES_FIELDS
