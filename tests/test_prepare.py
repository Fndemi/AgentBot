"""The prepare job (prepare.py) gets leads ready away from the send: it scores waiting leads in
batches, then niche-checks, contact-checks and personalises the leads the next sends would
pick, until 2 x DAILY_LIMIT are ready. sender.plan() only picks ready leads and does none of
that work itself (only the start year of a lead it picks, if missing). The send lock is
renewed every LOCK_REFRESH_SECONDS during a run, and a lock whose holder process is gone from
this host is taken over. Websites, WHOIS and the AI are fakes: nothing is fetched or sent."""
import socket
import subprocess
import sys
import time

import pytest

import config
import contacts
import db
import nichefit
import personalise
import prepare
import ranking
import sender
import targeting


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "DAILY_LIMIT", 2)           # aim: 4 leads ready
    monkeypatch.setattr(config, "USE_AI_OPENER", False)
    monkeypatch.setattr(sender, "prepared", sender.REAL_PREPARED)
    db.init()


def add(n, industry="fintech", score=None):
    email = f"info@lead{n}.co.ke"
    db.add_lead(f"Lead {n}", f"lead{n}.co.ke", industry, email, "search")
    lead_id = db.find_lead_by_email(email)["id"]
    db.set_status(lead_id, "verified", "verify")
    if score is not None:
        with db.conn() as c:
            c.execute("UPDATE leads SET score=?, score_version=?, scored_at=CURRENT_TIMESTAMP WHERE id=?",
                      (score, targeting.SCORE_VERSION, lead_id))
    return lead_id


def row(lead_id):
    with db.conn() as c:
        return dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


def test_scoring_comes_first_in_batches(world):
    for n in range(5):
        add(n)
    assert prepare.step(score_batch=3) == 3
    assert len(targeting.unscored()) == 2
    assert prepare.step(score_batch=3) == 2 and targeting.unscored() == []


def test_the_job_gets_two_days_of_leads_ready_best_first(world):
    ids = [add(n, score=90 - n) for n in range(6)]               # best first: lead 0 ... lead 5
    result = prepare.run()
    assert "4 ready for first emails (aiming for 4)" in result
    ready = [l["id"] for l in prepare.ready()]
    assert ready == ids[:4]
    assert all(row(i)["niche_checked_at"] and row(i)["contact_checked_at"] for i in ids[:4])
    assert row(ids[5])["contact_checked_at"] is None              # beyond the target: left for later
    assert prepare.step() == 0                                    # nothing more to do


def test_a_lead_that_cant_be_got_ready_doesnt_stall_the_run(world, monkeypatch):
    monkeypatch.setattr(config, "USE_AI_OPENER", True)
    stuck = add(0, score=95)
    for n in range(1, 4):
        add(n, score=80)

    def write(leads):
        for l in leads:   # the AI writes an opening for every lead but the stuck one
            if l["id"] != stuck:
                db.save_personalisation(l["id"], None, None, 0.0, None, None, "no opener")
        return 0

    monkeypatch.setattr(personalise, "run", write)
    prepare.run(seconds=30)
    assert stuck not in [l["id"] for l in prepare.ready()] and len(prepare.ready()) == 3


def test_plan_picks_only_ready_leads_and_does_no_preparing(world, monkeypatch, capsys):
    ready = add(0, score=90)
    unready = add(1, score=95)                                    # scored, but never prepared
    prepare.run()
    with db.conn() as c:                                          # undo the job's work on one lead
        c.execute("UPDATE leads SET niche_checked_at=NULL, contact_checked_at=NULL WHERE id=?", (unready,))

    def forbidden(*a, **k):
        raise AssertionError("the send must not do the prepare job's work")

    for module, name in ((targeting, "score_leads"), (targeting, "score_unscored"), (nichefit, "check"),
                         (contacts, "run"), (personalise, "run")):
        monkeypatch.setattr(module, name, forbidden)
    years = []
    monkeypatch.setattr(ranking, "fill_years", lambda leads=None: years.append([l["id"] for l in leads]) or 0)
    assert [l["id"] for l, f in sender.plan()] == [ready]
    assert years == [[ready]]                                     # start year: only the lead picked
    assert "1 waiting leads aren't prepared yet" in capsys.readouterr().out


def test_plan_looks_up_no_start_year_it_already_has(world, monkeypatch):
    lead_id = add(0, score=90)
    prepare.run()
    db.save_est_year(lead_id, 2020, "site")
    monkeypatch.setattr(ranking, "fill_years", lambda leads=None: pytest.fail("no lookup needed"))
    assert [l["id"] for l, f in sender.plan()] == [lead_id]


# --- The send lock ---

def test_the_lock_is_renewed_while_a_run_holds_it(world, monkeypatch):
    renewed = []
    monkeypatch.setattr(sender, "LOCK_REFRESH_SECONDS", 0.05)
    monkeypatch.setattr(db, "refresh_lock", lambda name, owner, seconds: renewed.append((name, owner, seconds)))
    with sender.keep_lock("me"):
        time.sleep(0.3)
    count = len(renewed)
    time.sleep(0.15)
    assert count >= 3 and len(renewed) == count                   # stops when the run ends
    assert renewed[0] == (sender.SEND_LOCK, "me", sender.LOCK_SECONDS)


def test_a_send_run_renews_its_lock_during_a_slow_batch(world, monkeypatch):
    add(0, score=90)
    prepare.run()
    renewed = []
    monkeypatch.setattr(sender, "LOCK_REFRESH_SECONDS", 0.05)
    real = db.refresh_lock
    monkeypatch.setattr(db, "refresh_lock", lambda *a: (renewed.append(a), real(*a)))
    monkeypatch.setattr(ranking, "fill_years", lambda leads=None: 0)
    monkeypatch.setattr(sender, "_send_batch", lambda batch, owner: time.sleep(0.3))
    sender.run()
    assert len(renewed) >= 3 and db.lock_holder(sender.SEND_LOCK) is None


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@pytest.mark.skipif(sys.platform == "win32", reason="process checks are for the Linux server")
def test_a_lock_whose_process_is_gone_is_taken_over(world, capsys):
    gone = f"{socket.gethostname()}:{dead_pid()}:abc123"
    assert db.acquire_lock(sender.SEND_LOCK, gone, 600)
    assert sender.take_lock("new run")
    assert db.lock_holder(sender.SEND_LOCK)["owner"] == "new run"
    assert "Took over the send lock" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="process checks are for the Linux server")
def test_a_live_or_remote_holder_keeps_the_lock(world):
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        for holder in (f"{socket.gethostname()}:{alive.pid}:abc123",       # still running here
                       f"another-host:{dead_pid()}:abc123",               # can't tell: it expires instead
                       "someone else"):
            with db.conn() as c:
                c.execute("DELETE FROM locks")
            assert db.acquire_lock(sender.SEND_LOCK, holder, 600)
            assert not sender.take_lock("new run")
            assert db.lock_holder(sender.SEND_LOCK)["owner"] == holder
    finally:
        alive.kill()
        alive.wait()
