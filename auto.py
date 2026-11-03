"""Runs LeadBot unattended. Leave `python main.py auto` running.

Weekdays from 9am Nairobi time, the daily run: prepare (check company names, names.py), send
(up to DAILY_LIMIT, from leads found and scored on earlier days), then for tomorrow: discover,
verify, score (start years and need scores) and personalise. Sending comes first so a long
discovery can't hold it up.

Each step is recorded in the database when it finishes (daily_steps), so a crash, restart or
redeploy picks the run up where it stopped, any time until LAST_HOUR; a step that fails is
tried again at the next check, at most MAX_TRIES times a day. A step that finished is never
run twice in a day.

The rest of the time: check the inbox every POLL_MINUTES, and check one small batch of
company names after each inbox check.
"""
import time
from datetime import datetime, timedelta, timezone

import config
import db

NAIROBI = timezone(timedelta(hours=3))  # Kenya has no daylight saving
RUN_HOUR = 9     # the daily run starts at 9am...
LAST_HOUR = 16   # ...and a step not started by 4pm waits for the next weekday
MAX_TRIES = 3    # a failing step is tried this many times a day at most


def log(msg):
    print(f"{datetime.now(NAIROBI):%Y-%m-%d %H:%M} {msg}", flush=True)


def step(name, fn):
    """Run one step and log what it did. Returns True if it finished without an error."""
    try:
        log(f"{name}: {fn()}")
        return True
    except Exception as e:
        log(f"{name}: FAILED - {e}")
        return False


def _discover():
    import discover
    return f"{len(discover.run())} new leads"


def _verify():
    import verify
    verify.run()
    return f"{len(db.leads_by_status('verified'))} verified leads waiting"


def _score():
    import ranking
    import targeting
    years = ranking.fill_years()
    return f"start years looked up for {years} leads, {targeting.score_unscored()} leads scored"


def _personalise():
    import personalise
    return f"{personalise.run()} leads given a personal opening"


# The prepare step checks names in batches for at most this long; the rest wait for the
# batches between inbox checks (names.prepare reads at most names.BATCH sites at a time)
PREPARE_SECONDS = 600


def _prepare():
    import names
    done = left = 0
    start = time.monotonic()
    while time.monotonic() - start < PREPARE_SECONDS:
        checked, left = names.prepare()
        done += checked
        if not left:
            break
    return f"checked the names of {done} leads, {left} still to check"


def _prepare_batch():
    """One batch of names between inbox checks; logged only when there was something to do."""
    import names
    try:
        checked, left = names.prepare()
    except Exception as e:
        log(f"names: FAILED - {e}")
        return
    if checked:
        log(f"names: checked {checked} leads, {left} still to check")


def _send():
    import sender
    sender.run()
    return f"{db.sent_today()} of {config.DAILY_LIMIT} sent today"


def _inbox():
    import inbox
    return f"{inbox.check_once()} replies handled"


# The daily run, in order: today's emails first, then the work for tomorrow's
STEPS = (("prepare", _prepare), ("send", _send), ("discover", _discover), ("verify", _verify),
         ("score", _score), ("personalise", _personalise))


def pending(day):
    """The daily run's steps still to do on `day` (YYYY-MM-DD): not finished, and tried
    fewer than MAX_TRIES times."""
    state = db.daily_steps(day)
    return [(name, fn) for name, fn in STEPS
            if not state.get(name, {}).get("done_at") and state.get(name, {}).get("tries", 0) < MAX_TRIES]


def daily_due(now):
    """Whether the daily run has work to do now: a weekday, from RUN_HOUR until LAST_HOUR,
    with a step left. A bot started or restarted at 11am still runs the day."""
    if now.weekday() >= 5 or not RUN_HOUR <= now.hour < LAST_HOUR:
        return False
    return bool(pending(now.date().isoformat()))


def daily_run(now):
    """Run each step still to do today, in order. A failed step doesn't stop the others; it
    is tried again at the next check (daily_due)."""
    day = now.date().isoformat()
    for name, fn in pending(day):
        db.daily_step_tried(day, name)
        if step(name, fn):
            db.daily_step_done(day, name)


def run():
    log(f"auto started: daily run on weekdays from {RUN_HOUR}:00 until {LAST_HOUR}:00 Nairobi time, "
        f"inbox every {config.POLL_MINUTES} min. Ctrl+C to stop.")
    while True:
        now = datetime.now(NAIROBI)
        if daily_due(now):
            daily_run(now)
        step("inbox", _inbox)
        _prepare_batch()
        time.sleep(config.POLL_MINUTES * 60)
