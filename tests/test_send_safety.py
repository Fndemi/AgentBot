"""G-03 and G-04: overlapping send runs never go over the daily limit or email a lead
twice, and a lead suppressed (or answered) after the batch was made is not emailed.
Temporary database and a fake mail server: no real email is sent."""
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import config
import db
import ranking
import sender

LEADS = [f"info@sacco{i}.co.ke" for i in range(6)]


@pytest.fixture
def waiting(tmp_path, monkeypatch):
    """Six verified SACCO leads; daily limit 3."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 3)
    monkeypatch.setattr(config, "SEND_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", False)
    monkeypatch.setattr(config, "FROM_EMAIL", "florence@talksasa.co.ke")
    monkeypatch.setattr(ranking, "fill_years", lambda *a, **k: 0)
    monkeypatch.setattr(sender, "save_to_sent", lambda *a: True)
    monkeypatch.setattr(sender.notify, "send", lambda t: None)
    db.init()
    for email in LEADS:
        db.add_lead("Test Sacco", email.split("@")[1], "sacco", email, "search")
        db.set_status(db.find_lead_by_email(email)["id"], "verified", "verify")


@pytest.fixture
def slow_smtp(smtp, monkeypatch):
    """The fake mail server, taking a moment per email so two runs overlap."""
    real = sender.smtplib.SMTP

    class Slow(real):
        def send_message(self, msg):
            time.sleep(0.2)
            super().send_message(msg)
            for hook in smtp.setdefault("after", []):
                hook(msg["To"])

    monkeypatch.setattr(sender.smtplib, "SMTP", Slow)
    return smtp


def events(kind):
    with db.conn() as c:
        return c.execute("SELECT l.email, e.detail FROM lead_events e JOIN leads l ON l.id = e.lead_id"
                         " WHERE e.event_type=?", (kind,)).fetchall()


def run_together(n=2):
    threads = [threading.Thread(target=sender.run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_overlapping_runs_send_once_within_the_limit(waiting, slow_smtp):
    run_together()
    assert len(slow_smtp["delivered"]) == 3
    assert len(set(slow_smtp["delivered"])) == 3
    assert db.sent_today() == 3


def test_without_the_lock_each_email_is_still_claimed_once(waiting, slow_smtp, monkeypatch):
    # Even if two runs get past the lock (say it expired), the per-email claims hold
    monkeypatch.setattr(db, "acquire_lock", lambda *a: True)
    run_together(3)
    assert len(slow_smtp["delivered"]) == 3
    assert len(set(slow_smtp["delivered"])) == 3
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 3
        assert c.execute("SELECT COUNT(*) FROM send_claims").fetchone()[0] == 0


def test_second_run_waits_its_turn(waiting, smtp):
    assert db.acquire_lock(sender.SEND_LOCK, "someone else", 60)
    sender.run()
    assert smtp["delivered"] == []
    db.release_lock(sender.SEND_LOCK, "someone else")
    sender.run()
    assert len(smtp["delivered"]) == 3


def test_lock_holds_across_processes(waiting, smtp):
    root = Path(__file__).resolve().parent.parent
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import config; config.DB_PATH = sys.argv[2]; "
            "config.DATABASE_URL = sys.argv[3]; import db; print(db.acquire_lock('send', 'other-process', 60))")
    out = subprocess.run([sys.executable, "-c", code, str(root), config.DB_PATH, config.DATABASE_URL],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "True"
    sender.run()
    assert smtp["delivered"] == []


def test_a_crashed_runs_lock_expires(waiting, smtp):
    assert db.acquire_lock(sender.SEND_LOCK, "crashed run", -1)  # already expired
    sender.run()
    assert len(smtp["delivered"]) == 3


def test_failed_sends_do_not_count_towards_the_limit(waiting, smtp):
    import smtplib
    smtp["errors"][LEADS[0]] = [smtplib.SMTPDataError(451, b"Try again later")]
    sender.run()
    assert smtp["delivered"] == LEADS[1:3]
    assert db.sent_today() == 2  # the failed one is not counted as sent
    assert db.find_lead_by_email(LEADS[0])["status"] == "verified"  # tried again next time
    assert len(events("send_failed")) == 1
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM send_claims").fetchone()[0] == 0


def test_left_over_claim_holds_that_lead(waiting, smtp):
    lead = db.find_lead_by_email(LEADS[0])
    assert db.claim_send(lead["id"], False, 3, "a run that crashed mid-send") is None
    sender.run()
    assert LEADS[0] not in smtp["delivered"]  # it may already have gone out
    assert len(smtp["delivered"]) == 2  # the left-over claim counts towards today's limit


# --- G-04 ---

def test_lead_suppressed_during_the_batch_gets_no_email(waiting, slow_smtp, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 6)
    # an opt-out from LEADS[2] arrives while the first email is going out
    slow_smtp["after"] = [lambda to: to == LEADS[0] and db.unsubscribe(LEADS[2])]
    sender.run()
    assert LEADS[2] not in slow_smtp["delivered"]
    assert len(slow_smtp["delivered"]) == 5
    skipped = events("send_skipped")
    assert [r["email"] for r in skipped] == [LEADS[2]]
    assert "unsubscribed" in skipped[0]["detail"]


def test_address_suppressed_without_a_status_change_gets_no_email(waiting, slow_smtp, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 6)

    def suppress(to):
        if to == LEADS[0]:
            with db.conn() as c:
                db._suppress(c, LEADS[3], "bounced")

    slow_smtp["after"] = [suppress]
    sender.run()
    assert LEADS[3] not in slow_smtp["delivered"]
    assert "suppression list" in events("send_skipped")[0]["detail"]


def test_follow_up_skipped_when_the_lead_replied_during_the_batch(waiting, smtp, monkeypatch):
    monkeypatch.setattr(config, "DAILY_LIMIT", 6)
    monkeypatch.setattr(config, "FOLLOWUPS_APPROVED", True)
    sender.run()  # first emails to all six
    with db.conn() as c:
        c.execute("UPDATE sent SET sent_on='2026-01-05'")
    smtp["delivered"].clear()
    batch = sender.plan()
    assert all(followup for _, followup in batch)
    db.set_status(db.find_lead_by_email(LEADS[1])["id"], "replied", "inbox")  # answered meanwhile
    sender._send_batch(batch, "test")
    assert LEADS[1] not in smtp["delivered"] and len(smtp["delivered"]) == 5
