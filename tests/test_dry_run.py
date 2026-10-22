"""G-12: `send --dry-run` and `preview` change nothing and reach nothing: no email, no
follow-up, no score or start-year saved, no WHOIS or website lookup, no AI. Every way
out to the network is made to fail the test."""
import socket
import sys

import pytest

import classifier
import config
import db
import main
import ranking
import sender


@pytest.fixture
def unreachable(monkeypatch):
    """Fails the test on any network use, email or AI call."""
    def blocked(*a, **k):
        pytest.fail(f"network or side effect during a dry run: {a[:2]}")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(sender.smtplib, "SMTP", blocked)
    monkeypatch.setattr(sender.imaplib, "IMAP4_SSL", blocked)
    monkeypatch.setattr(sender.notify, "send", blocked)
    monkeypatch.setattr(classifier, "ask", blocked)
    monkeypatch.setattr(ranking, "whois_year", blocked)
    monkeypatch.setattr(ranking.scraper, "site_pages", blocked)


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """Unranked, unpersonalised leads waiting, and one follow-up due."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 10)
    monkeypatch.setattr(config, "USE_AI_OPENER", True)  # the AI would be asked in a real send
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    monkeypatch.setattr(config, "FROM_EMAIL", "florence@talksasa.co.ke")
    db.init()
    for i in range(4):
        db.add_lead(f"Sacco {i}", f"https://sacco{i}.co.ke", "sacco", f"info@sacco{i}.co.ke", "search")
        db.set_status(db.find_lead_by_email(f"info@sacco{i}.co.ke")["id"], "verified", "verify")
    first = db.find_lead_by_email("info@sacco0.co.ke")
    db.record_sent(first["id"], "<x@talksasa.co.ke>", "sacco", "Loan reminders by SMS", "body")
    db.set_status(first["id"], "sent", "send")
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on='2026-01-05'")  # long past its follow-up day


TABLES = ("leads", "sent", "replies", "skipped", "lead_events", "suppression", "alert_outbox",
          "send_claims", "locks")


def dump():
    with db.conn() as c:
        return {t: [tuple(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY 1")] for t in TABLES}


def test_dry_run_changes_and_reaches_nothing(ready, unreachable, capsys):
    before = dump()
    sender.run(dry_run=True)
    assert dump() == before
    out = capsys.readouterr().out
    assert "FOLLOW-UP" in out and "FIRST EMAIL" in out
    assert "would send 1 follow-ups and 3 first emails" in out


def test_dry_run_from_the_command_line(ready, unreachable, monkeypatch):
    before = dump()
    monkeypatch.setattr(sys, "argv", ["main.py", "send", "--dry-run"])
    main.main()  # runs db.init() too
    assert dump() == before


def test_preview_changes_and_reaches_nothing(ready, unreachable):
    before = dump()
    sender.preview(limit=3)
    sender.preview("school")
    assert dump() == before


def test_dry_run_while_sending_is_paused_still_changes_nothing(ready, unreachable, monkeypatch):
    monkeypatch.setattr(sender, "bounce_gate", lambda: "too many bounces")
    before = dump()
    sender.run(dry_run=True)
    assert dump() == before
