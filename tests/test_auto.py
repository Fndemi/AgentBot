"""The bot's daily run (auto.py): sending first, then the work for tomorrow; due on weekdays from
9am until 4pm while a step is left, so a bot started late or restarted mid-run still does the
day; each finished step recorded in the database, never run twice a day; a failing step tried
again at the next check, at most MAX_TRIES times. Steps are fakes: nothing is sent or fetched."""
from datetime import datetime

import pytest

import auto
import config
import db

DAY = datetime(2026, 11, 3, 9, 0, tzinfo=auto.NAIROBI)   # a Tuesday


@pytest.fixture
def steps(tmp_path, monkeypatch):
    """Fake steps: every call is recorded; a name in .failing raises."""
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

    monkeypatch.setattr(auto, "STEPS", tuple((name, fake(name)) for name, _ in auto.STEPS))
    return calls, failing


def at(hour, minute=0, day=3):
    return DAY.replace(day=day, hour=hour, minute=minute)


def test_sending_comes_first_then_the_work_for_tomorrow(steps):
    calls, _ = steps
    auto.daily_run(at(9))
    assert calls == ["prepare", "send", "discover", "verify", "score", "personalise"]


def test_a_finished_step_never_runs_twice_a_day(steps):
    calls, _ = steps
    auto.daily_run(at(9))
    assert not auto.daily_due(at(9, 30))
    auto.daily_run(at(10))
    assert calls.count("send") == 1
    assert auto.daily_due(at(9, day=4))          # the next weekday starts again


@pytest.mark.parametrize("hour, due", [(8, False), (9, True), (11, True), (15, True), (16, False)])
def test_due_from_9am_until_4pm(steps, hour, due):
    assert auto.daily_due(at(hour, 59)) == due


def test_no_run_at_weekends(steps):
    assert not auto.daily_due(at(10, day=1))   # Sunday 1 Nov 2026
    assert not auto.daily_due(at(10, day=7))   # Saturday 7 Nov 2026


def test_a_restart_mid_run_picks_up_where_it_stopped(steps):
    calls, _ = steps
    for name in ("prepare", "send"):            # the run before the restart got this far
        db.daily_step_tried("2026-11-03", name)
        db.daily_step_done("2026-11-03", name)
    db.daily_step_tried("2026-11-03", "discover")   # ...and died during discovery
    assert auto.daily_due(at(11))
    auto.daily_run(at(11))
    assert calls == ["discover", "verify", "score", "personalise"]


def test_a_failing_step_is_tried_again_at_most_three_times(steps):
    calls, failing = steps
    failing.add("send")
    auto.daily_run(at(9))
    assert calls == ["prepare", "send", "discover", "verify", "score", "personalise"]
    assert auto.daily_due(at(9, 5))
    auto.daily_run(at(9, 5))
    auto.daily_run(at(9, 10))
    assert calls.count("send") == 3 and calls.count("discover") == 1
    assert not auto.daily_due(at(9, 15))         # three tries: it waits for tomorrow
    failing.clear()
    auto.daily_run(at(9, day=4))
    assert calls.count("send") == 4
