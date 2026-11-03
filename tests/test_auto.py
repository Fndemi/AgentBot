"""The bot's day (auto.py): at 9am send first, then discover, verify, personalise and the name
checks; until 5pm the send is topped up while fewer than DAILY_LIMIT have gone out; from 5pm
the prepare job gets the next sends' leads ready. Each finished step is recorded in the
database and never runs twice a day; a failing step is tried again, at most MAX_TRIES times;
a restart picks the day up where it stopped. Steps are fakes: nothing is sent or fetched."""
from datetime import datetime

import pytest

import auto
import config
import db
import prepare
import rotation
import sender

MORNING = ["send", "discover", "verify", "personalise", "names"]


@pytest.fixture
def steps(tmp_path, monkeypatch):
    """Fake steps: every call is recorded; a name in `failing` raises."""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    db.init()
    calls, failing = [], set()

    def fake(name):
        def fn():
            calls.append(name)
            if name in failing:
                raise RuntimeError(f"{name} broke")
            return "ok"
        return fn

    monkeypatch.setattr(auto, "MORNING", tuple((name, fake(name)) for name, _ in auto.MORNING))
    monkeypatch.setattr(auto, "EVENING", tuple((name, fake(name)) for name, _ in auto.EVENING))
    monkeypatch.setattr(auto, "_send", fake("send (top-up)"))
    return calls, failing


def at(hour, minute=0, day=3):
    """A time on Tuesday 3 Nov 2026 (day=1 is a Sunday, 7 a Saturday)."""
    return datetime(2026, 11, day, hour, minute, tzinfo=auto.NAIROBI)


# --- The day's steps ---

def test_at_9am_sending_comes_first(steps):
    calls, _ = steps
    auto.daily_run(at(9))
    assert calls == MORNING


def test_the_prepare_job_runs_from_5pm(steps):
    calls, _ = steps
    assert [n for n, _ in auto.steps_for(at(16, 59))] == MORNING
    assert [n for n, _ in auto.steps_for(at(17))] == ["prepare"]
    auto.daily_run(at(17, 5))
    assert calls == ["prepare"]
    assert not auto.daily_due(at(20))                     # once a day
    assert auto.daily_due(at(17, day=4))


@pytest.mark.parametrize("hour, due", [(8, False), (9, True), (12, True), (16, True), (17, True), (23, True)])
def test_due_hours(steps, hour, due):
    assert auto.daily_due(at(hour, 30)) == due


def test_no_steps_at_weekends(steps):
    assert not auto.daily_due(at(10, day=1)) and not auto.daily_due(at(18, day=7))


def test_a_finished_step_never_runs_twice_a_day(steps):
    calls, _ = steps
    auto.daily_run(at(9))
    assert not auto.daily_due(at(11))
    auto.daily_run(at(11))
    assert calls.count("send") == 1


def test_a_restart_mid_morning_picks_up_where_it_stopped(steps):
    calls, _ = steps
    db.daily_step_tried("2026-11-03", "send")
    db.daily_step_done("2026-11-03", "send")
    db.daily_step_tried("2026-11-03", "discover")         # died during discovery
    auto.daily_run(at(11))
    assert calls == ["discover", "verify", "personalise", "names"]


def test_a_failing_step_is_tried_again_at_most_three_times(steps):
    calls, failing = steps
    failing.add("discover")
    for minute in (0, 5, 10, 15):
        auto.daily_run(at(9, minute))
    assert calls.count("discover") == 3 and calls.count("send") == 1
    assert not auto.daily_due(at(9, 20))


def test_markers_are_in_the_database_not_on_disk(steps, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    auto.daily_run(at(9))
    assert db.daily_steps("2026-11-03")["send"]["done_at"]
    assert not hasattr(auto, "MARKER") and list(tmp_path.glob(".auto*")) == []


# --- Topping up the send until 5pm ---

@pytest.fixture
def topping(steps, monkeypatch):
    calls, _ = steps
    sent, plan = [0], [[("lead", 0)]]
    monkeypatch.setattr(config, "DAILY_LIMIT", 20)
    monkeypatch.setattr(db, "sent_today", lambda: sent[0])
    monkeypatch.setattr(sender, "bounce_gate", lambda: None)
    monkeypatch.setattr(rotation, "refusal", lambda: None)
    monkeypatch.setattr(sender, "plan", lambda dry_run=False, quiet=False: plan[0])
    db.daily_step_tried("2026-11-03", "send")
    return calls, sent, plan


def test_the_send_is_topped_up_while_under_the_limit(topping):
    calls, sent, _ = topping
    assert auto.top_up(at(11)) and calls == ["send (top-up)"]
    sent[0] = 20
    assert not auto.top_up(at(11, 5))                    # the day's limit has gone out


@pytest.mark.parametrize("when", [at(8, 55), at(17), at(11, day=7)])
def test_no_top_up_outside_sending_hours(topping, when):
    assert not auto.top_up(when)


def test_no_top_up_before_the_mornings_send(topping):
    with db.conn() as c:
        c.execute("DELETE FROM daily_steps")
    assert not auto.top_up(at(11))


def test_no_top_up_with_nothing_to_send_or_sending_paused(topping, monkeypatch):
    calls, _, plan = topping
    plan[0] = []
    assert not auto.top_up(at(11))
    plan[0] = [("lead", 0)]
    monkeypatch.setattr(sender, "bounce_gate", lambda: "3 of 30 bounced")   # no repeat of the alert
    assert not auto.top_up(at(11))
    assert calls == []


# --- Between inbox checks ---

def test_between_checks_takes_one_small_step_of_each(steps, monkeypatch):
    import names
    asked = []
    monkeypatch.setattr(names, "prepare", lambda: (asked.append("names"), (0, 0))[1])
    monkeypatch.setattr(prepare, "step", lambda score_batch, lead_batch: (asked.append((score_batch, lead_batch)), 0)[1])
    auto.between_checks()
    assert asked == ["names", (prepare.SCORE_BATCH // 2, 2)]
