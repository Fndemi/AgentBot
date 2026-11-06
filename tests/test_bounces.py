"""Bounces (bounces.py): each one's kind (hard: no such user, domain not found; soft: mailbox
full, try later) and the server's reason are recorded, from a delivery report or a refusal
while sending. A hard one stops all email to that address at once; a soft one stops nothing
and pauses the lead for a day. `python main.py bounces [days]` sums them up without any lead's
name or address. No real email is sent: the mail server and the mailbox are fakes."""
import re
import smtplib

import pytest

import bounces
import config
import db
import ranking
import sender
from fakemail import SENT_ID, message, run, status

POSTFIX_HARD = """This is the mail system at host mail.talksasa.com.

I'm sorry to have to inform you that your message could not
be delivered to one or more recipients. It's attached below.

                   The mail system

<{email}>: host mx.nyerihigh.com[192.250.227.16] said: 550 No Such
    User Here (in reply to RCPT TO command)
"""


def dsn(email, status_code, diagnostic, returned_id=None, action="failed"):
    """A delivery report from our mail server, as Postfix writes one (the readable part, then
    the machine-readable fields, then the headers of the email it returns)."""
    body = (f"Your message could not be delivered.\n\nFinal-Recipient: rfc822; {email}\nAction: {action}\n"
            f"Status: {status_code}\nDiagnostic-Code: smtp; {diagnostic}\n")
    if returned_id:
        body += f"\nContent-Type: text/rfc822-headers\n\nMessage-ID: {returned_id}\nSubject: SMS for your school\n"
    return message("MAILER-DAEMON@mail.talksasa.com", "Undelivered Mail Returned to Sender", body,
                   message_id="<report.1@mail.talksasa.com>")


def bounce_rows():
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM bounces ORDER BY id")]


def suppressed():
    with db.conn() as c:
        return {r[0] for r in c.execute("SELECT email FROM suppression")}


# --- What kind of bounce ---

@pytest.mark.parametrize("reason, status_code, kind", [
    ("550 No Such User Here (in reply to RCPT TO command)", None, "hard"),
    ("550-5.1.1 The email account that you tried to reach does not exist.", None, "hard"),
    ("550 5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup", None, "hard"),
    ("Host or domain name not found. Name service error for name=acme.co.ke type=MX", None, "hard"),
    ("554 5.7.7 Email policy violation detected", None, "hard"),
    ("552 5.2.2 Mailbox full", None, "soft"),
    ("550 Requested action not taken: mailbox is full", None, "soft"),
    ("User is over quota", "5.2.2", "soft"),
    ("451 4.7.1 Greylisted, please try again later", None, "soft"),
    ("Service temporarily unavailable", None, "soft"),
    ("delivery problem", "4.4.1", "soft"),
    ("delivery problem", "5.0.0", "hard"),
    ("", None, "unknown"),
    ("connect to mx.acme.co.ke[1.2.3.4]:25: Connection timed out", None, "unknown"),
])
def test_each_bounce_gets_its_kind(reason, status_code, kind):
    assert bounces.classify(reason, status_code)[0] == kind


def test_the_code_is_the_status_or_the_first_smtp_code():
    assert bounces.classify("550 5.1.1 user unknown", "5.1.1") == ("hard", "5.1.1")
    assert bounces.classify("550 No Such User Here") == ("hard", "550")


def test_the_reason_is_the_servers_own_words():
    assert bounces.reason_of(POSTFIX_HARD.format(email="info@nyerihigh.com")) == \
        "550 No Such User Here (in reply to RCPT TO command)"
    assert bounces.reason_of("Diagnostic-Code: smtp; 550 5.1.1 <a@b.co.ke>:\n    Recipient address rejected") == \
        "550 5.1.1 <a@b.co.ke>: Recipient address rejected"


def test_exim_and_google_groups_reports_are_hard():
    exim = ("A message that you sent could not be delivered to one or more of its\nrecipients. This is a "
            "permanent error. The following address(es) failed:\n\n  sales@acme.co.ke\n    Unrouteable address\n")
    assert bounces.from_report(exim)[0::2] == ("hard", "Unrouteable address")
    groups = ("We're writing to let you know that the group you tried to contact (sales) may not exist, "
              "or you may not have permission to post messages to the group.")
    assert bounces.from_report(groups)[0] == "hard"


def test_our_own_email_returned_in_the_report_is_never_read_as_the_reason():
    raw = ("Final-Recipient: rfc822; a@acme.co.ke\nAction: failed\n\n"
           "Content-Type: message/rfc822\n\nSubject: hi\n\nIf your mailbox is full, try again later.\n")
    assert bounces.from_report(raw)[0] == "unknown"


# --- From the inbox ---

def test_hard_bounce_report_is_recorded_and_stops_that_address(leads, ai, alerts, monkeypatch):
    email = "info@nyerihigh.com"
    run(monkeypatch, dsn(email, "5.1.1", "550 5.1.1 <info@nyerihigh.com>: User unknown", returned_id=SENT_ID))
    [row] = bounce_rows()
    assert (row["lead_id"], row["email"], row["kind"], row["code"], row["step"], row["via"]) == \
        (leads[email], email, "hard", "5.1.1", 0, "inbox")
    assert "User unknown" in row["reason"] and row["reply_id"]
    assert status(email) == "bounced" and email in suppressed()


def test_soft_bounce_report_is_recorded_and_stops_nothing(leads, ai, alerts, monkeypatch):
    email = "info@dazzcode.com"
    run(monkeypatch, dsn(email, "4.2.2", "452 4.2.2 Mailbox full"))
    [row] = bounce_rows()
    assert (row["kind"], row["code"], row["step"], row["lead_id"]) == ("soft", "4.2.2", 0, leads[email])
    assert status(email) == "sent"            # still waiting for a reply, and for its follow-up
    assert email not in suppressed()
    with db.conn() as c:
        assert c.execute("SELECT summary FROM replies").fetchone()[0].startswith(
            f"delivery to {email} failed (soft bounce")


def test_soft_bounce_pauses_the_follow_up_for_a_day(leads, ai, alerts, monkeypatch):
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    monkeypatch.setattr(config, "FOLLOWUP_AFTER_DAYS", 0)
    with db.conn() as c:
        c.execute("UPDATE leads SET industry='sacco'")
    before = {l["email"] for l, _ in sender.followups_due()}
    assert "info@dazzcode.com" in before
    run(monkeypatch, dsn("info@dazzcode.com", "4.2.2", "452 4.2.2 Mailbox full"))
    assert "info@dazzcode.com" not in {l["email"] for l, _ in sender.followups_due()}
    with db.conn() as c:
        c.execute("UPDATE bounces SET created_at = datetime('now', '-25 hours')")
    assert "info@dazzcode.com" in {l["email"] for l, _ in sender.followups_due()}


def test_the_returned_message_id_gives_the_step(leads, ai, alerts, monkeypatch):
    email = "info@nyerihigh.com"
    db.record_sent(leads[email], "<second@talksasa.co.ke>", "school")  # follow-up 1
    run(monkeypatch, dsn(email, "5.1.1", "550 5.1.1 User unknown", returned_id="<second@talksasa.co.ke>"))
    assert bounce_rows()[0]["step"] == 1


def test_a_report_for_an_address_that_is_no_lead_still_suppresses_it(leads, ai, alerts, monkeypatch):
    """The email went to a lead and was forwarded on; the forward's address failed. The lead is
    not bounced for it, but that address is never emailed."""
    run(monkeypatch, dsn("old.person@nyerihigh.com", "5.1.1", "550 5.1.1 User unknown", returned_id=SENT_ID))
    [row] = bounce_rows()
    assert row["lead_id"] is None and row["kind"] == "hard"
    assert "old.person@nyerihigh.com" in suppressed()
    assert status("info@nyerihigh.com") == "sent"


def test_a_report_is_recorded_once(leads, ai, alerts, monkeypatch):
    mail = dsn("info@nyerihigh.com", "5.1.1", "550 5.1.1 User unknown")
    run(monkeypatch, mail)
    run(monkeypatch, mail)
    assert len(bounce_rows()) == 1


def test_a_delay_warning_is_no_bounce(leads, ai, alerts, monkeypatch):
    run(monkeypatch, dsn("info@nyerihigh.com", "4.4.1", "Connection timed out", action="delayed"))
    assert bounce_rows() == [] and status("info@nyerihigh.com") == "sent"


# --- While sending ---

@pytest.fixture
def waiting(tmp_path, monkeypatch):
    """Two verified SACCO leads waiting for their first email."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", False)
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    monkeypatch.setattr(sender.notify, "send", lambda text: None)
    db.init()
    for email in ("one@one.co.ke", "two@two.co.ke"):
        db.add_lead("Test Sacco", email.split("@")[1], "sacco", email, "search")
        db.set_status(db.find_lead_by_email(email)["id"], "verified", "verify")


def lead_status(email):
    return db.find_lead_by_email(email)["status"]


def test_hard_refusal_while_sending_is_recorded_and_suppressed(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPRecipientsRefused({"two@two.co.ke": (550, b"5.1.1 No such user")})]
    sender.run()
    [row] = bounce_rows()
    assert (row["email"], row["kind"], row["code"], row["step"], row["via"]) == \
        ("two@two.co.ke", "hard", "5.1.1", 0, "send")
    assert lead_status("two@two.co.ke") == "bounced" and "two@two.co.ke" in suppressed()
    assert smtp["delivered"] == ["one@one.co.ke"]


def test_soft_refusal_while_sending_fails_that_email_and_waits_a_day(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPRecipientsRefused({"two@two.co.ke": (452, b"4.2.2 Mailbox full")})]
    sender.run()
    [row] = bounce_rows()
    assert (row["kind"], row["code"], row["via"]) == ("soft", "4.2.2", "send")
    assert lead_status("two@two.co.ke") == "verified" and "two@two.co.ke" not in suppressed()
    sender.run()                       # the top-up a few minutes later: not tried again
    assert smtp["delivered"] == ["one@one.co.ke"]
    with db.conn() as c:
        c.execute("UPDATE bounces SET created_at = datetime('now', '-25 hours')")
    sender.run()                       # a day later it goes out
    assert smtp["delivered"] == ["one@one.co.ke", "two@two.co.ke"]


def test_a_hard_bounced_address_gets_nothing_more_from_any_route(waiting, smtp):
    """Suppressed at once: the bot's own sends, a campaign (campaigns.why_not checks the
    suppression list) and the CRM all refuse it."""
    import campaigns
    import crm
    from fastapi import HTTPException
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPRecipientsRefused({"two@two.co.ke": (550, b"No such user")})]
    sender.run()
    lead = db.find_lead_by_email("two@two.co.ke")
    assert db.claim_send(lead["id"], 0, 10) is not None
    entry = {"to": "two@two.co.ke", "company": "Two", "website": "", "subject": "s", "body": "b",
             "contact_name": "", "followup_body": "", "allow_same_company": False}
    assert "suppression" in campaigns.why_not(entry, "Group A")
    with pytest.raises(HTTPException) as refused:
        crm._send(None, {"id": lead["id"]}, {"id": 1}, "two@two.co.ke", "s", None, "b", [], None, "emailed")
    assert refused.value.status_code == 409


# --- The summary ---

def add_lead(company, email, source="search", campaign=None):
    db.add_lead(company, email.split("@")[1], "sacco", email, source)
    lead_id = db.find_lead_by_email(email)["id"]
    if campaign:
        with db.conn() as c:
            c.execute("UPDATE leads SET campaign=? WHERE id=?", (campaign, lead_id))
    return lead_id


def test_summary_groups_bounces_and_names_no_one(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    rows = [  # company, address, source, campaign, kind, step, reason
        ("Zebra Lenders", "info@zebralend.co.ke", "search", None, "hard", 0,
         "550 5.1.1 <info@zebralend.co.ke>: User unknown (in reply to RCPT TO command)"),
        ("Umoja Credit", "jane.wanjiru@umojacredit.co.ke", "CBK DCP directory Sep 2026", None, "hard", 1,
         "host mx.umojacredit.co.ke[102.1.2.3] said: 550 No such user"),
        ("Kilele Pay", "support@kilelepay.co.ke", "csv", None, "soft", 0, "452 4.2.2 Mailbox full"),
        ("Bomahut Ltd", "newton@bomahut.com", "campaign", "Group A", "hard", 0,
         "550 5.1.1 The email account that you tried to reach does not exist https://support.google.com/x"),
    ]
    for company, email, source, campaign, kind, step, reason in rows:
        lead_id = add_lead(company, email, source, campaign)
        bounces.record(lead_id, email, kind, *bounces.classify(reason)[1:], reason, step, "inbox")
    lines = bounces.summary(30)
    text = "\n".join(lines)
    assert lines[0] == "Bounces in the last 30 days: 4 (3 hard, 1 soft, 0 unknown)"
    for name in ("info@", "named person", "role inbox", "search", "CBK", "list", "Group A", "first email",
                 "follow-up 1"):
        assert name in text
    assert "  info@                       1     1     0        0" in lines
    assert "  first email                 3     2     1        0" in lines
    for private in ("Zebra", "Umoja", "Kilele", "Bomahut", "zebralend", "umojacredit", "kilelepay", "bomahut",
                    "jane", "newton", "102.1.2.3", "google.com"):
        assert private.lower() not in text.lower(), private
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)   # no address at all ("info@" is a type)


def test_summary_window_and_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    lead_id = add_lead("Old", "info@old.co.ke")
    bounces.record(lead_id, "info@old.co.ke", "hard", "550", "550 no such user", 0, "send",
                   at="2026-01-02 10:00:00")
    assert bounces.summary(30) == ["Bounces in the last 30 days: 0 (0 hard, 0 soft, 0 unknown)"]


def test_the_command_takes_the_days(tmp_path, monkeypatch, capsys):
    import main
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    lead_id = add_lead("Recent", "info@recent.co.ke")
    bounces.record(lead_id, "info@recent.co.ke", "hard", "550", "550 no such user", 0, "send",
                   at=None)
    monkeypatch.setattr("sys.argv", ["main.py", "bounces", "7"])
    main.main()
    out = capsys.readouterr().out
    assert "Bounces in the last 7 days: 1 (1 hard, 0 soft, 0 unknown)" in out
    assert "recent.co.ke" not in out and "Recent" not in out
    monkeypatch.setattr("sys.argv", ["main.py", "bounces", "week"])
    with pytest.raises(SystemExit, match="Usage"):
        main.main()


def test_test_leads_are_left_out_of_the_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    lead_id = add_lead("Test", "info@test-lead.co.ke", source="manual")
    bounces.record(lead_id, "info@test-lead.co.ke", "hard", "550", "550 no such user", 0, "send")
    assert bounces.summary(30)[0].startswith("Bounces in the last 30 days: 0")


# --- Bounces from before the table ---

def test_older_bounces_are_added_once_with_their_own_time(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    inbox_lead = add_lead("A", "info@a.co.ke")
    db.record_sent(inbox_lead, "<a1@talksasa.co.ke>", "sacco")
    reply_id = db.add_reply(inbox_lead, "mailer-daemon@mail.talksasa.com", "Undelivered",
                            POSTFIX_HARD.format(email="info@a.co.ke"), "bounce", "delivery failed", "INBOX:9")
    send_lead = add_lead("B", "info@b.co.ke")
    db.set_status(send_lead, "bounced", "send")          # refused while sending, before this table
    manual = add_lead("C", "info@c.co.ke")
    db.set_status(manual, "bounced", "dashboard")        # set by hand: not a bounce to count
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on='2026-10-19'")
        c.execute("UPDATE replies SET received_at='2026-10-20 08:00:00'")
        c.execute("UPDATE lead_events SET created_at='2026-10-21 09:00:00' WHERE event_type='bounced'")
    assert bounces.backfill() == 2
    assert bounces.backfill() == 0
    rows = {r["email"]: r for r in bounce_rows()}
    assert set(rows) == {"info@a.co.ke", "info@b.co.ke"}
    a, b = rows["info@a.co.ke"], rows["info@b.co.ke"]
    assert (a["kind"], a["code"], a["step"], a["via"], a["reply_id"], a["created_at"]) == \
        ("hard", "550", 0, "inbox", reply_id, "2026-10-20 08:00:00")
    assert (b["kind"], b["step"], b["via"], b["created_at"]) == ("unknown", 0, "send", "2026-10-21 09:00:00")
    assert all(r["backfilled"] == 1 for r in rows.values())


def test_bounces_recorded_now_are_never_added_again_by_the_backfill(waiting, smtp):
    smtp["errors"]["two@two.co.ke"] = [smtplib.SMTPRecipientsRefused({"two@two.co.ke": (550, b"No such user")})]
    sender.run()
    assert len(bounce_rows()) == 1
    assert bounces.backfill() == 0
