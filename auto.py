"""Runs LeadBot unattended. Leave `python main.py auto` running.

Weekdays, Nairobi time:
  from 9am   send first (up to DAILY_LIMIT, to leads got ready the evening before), then
             discover, verify, personalise and check company names (names.py)
  to 5pm     top up: while fewer than DAILY_LIMIT have gone out, send again every
             POLL_MINUTES (follow-ups that fall due, leads got ready since)
  from 5pm   prepare (prepare.py): score waiting leads and get the next sends' leads ready
             (niche check, contact, opening line), aiming for 2 x DAILY_LIMIT

Each step is recorded in the database when it finishes (daily_steps), so a crash, restart or
redeploy picks the day up where it stopped; a step that fails is tried again at the next
check, at most MAX_TRIES times a day. A step that finished never runs twice in a day.

All the time: check the inbox every POLL_MINUTES, and after each check take one small step
of name checks and of lead preparation.
"""
import time
from datetime import datetime, timedelta, timezone

import config
import db

NAIROBI = timezone(timedelta(hours=3))  # Kenya has no daylight saving
RUN_HOUR = 9        # the morning steps start at 9am...
SEND_UNTIL = 17     # ...sends top up until 5pm, and a morning step not started by then waits
PREPARE_HOUR = 17   # the prepare job runs from 5pm
MAX_TRIES = 3       # a failing step is tried this many times a day at most


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


def _send():
    import sender
    sender.run()
    return f"{db.sent_today()} of {config.DAILY_LIMIT} sent today"


def _discover():
    import discover
    return f"{len(discover.run())} new leads"


def _verify():
    import verify
    verify.run()
    return f"{len(db.leads_by_status('verified'))} verified leads waiting"


def _personalise():
    import personalise
    return f"{personalise.run()} leads given a personal opening"


# The names step checks names in batches for at most this long; the rest wait for the
# batches between inbox checks (names.prepare reads at most names.BATCH sites at a time)
NAMES_SECONDS = 600


def _names():
    import names
    done = left = 0
    start = time.monotonic()
    while time.monotonic() - start < NAMES_SECONDS:
        checked, left = names.prepare()
        done += checked
        if not left:
            break
    return f"checked the names of {done} leads, {left} still to check"


def _prepare():
    import prepare
    return prepare.run()


def _inbox():
    import inbox
    return f"{inbox.check_once()} replies handled"


# The day's steps, in order, and the hours they may start in
MORNING = (("send", _send), ("discover", _discover), ("verify", _verify),
           ("personalise", _personalise), ("names", _names))
EVENING = (("prepare", _prepare),)


def steps_for(now):
    """The steps that may run at `now`: the morning ones from RUN_HOUR until SEND_UNTIL, the
    evening one from PREPARE_HOUR, on weekdays only."""
    if now.weekday() >= 5:
        return ()
    if RUN_HOUR <= now.hour < SEND_UNTIL:
        return MORNING
    return EVENING if now.hour >= PREPARE_HOUR else ()


def pending(day, steps):
    """Of `steps`, those still to do on `day` (YYYY-MM-DD): not finished, and tried fewer
    than MAX_TRIES times."""
    state = db.daily_steps(day)
    return [(name, fn) for name, fn in steps
            if not state.get(name, {}).get("done_at") and state.get(name, {}).get("tries", 0) < MAX_TRIES]


def daily_due(now):
    """Whether a step is due now (steps_for). A bot started or restarted at 11am still
    does the morning; one started at 8pm still prepares."""
    return bool(pending(now.date().isoformat(), steps_for(now)))


def daily_run(now):
    """Run each step due now and still to do today, in order. A failed step doesn't stop the
    others; it is tried again at the next check (daily_due)."""
    day = now.date().isoformat()
    for name, fn in pending(day, steps_for(now)):
        db.daily_step_tried(day, name)
        if step(name, fn):
            db.daily_step_done(day, name)


def top_up(now):
    """Send again if today's send has run, fewer than DAILY_LIMIT have gone out, it is before
    SEND_UNTIL and there is something to send. Quiet otherwise: no log line, and no repeat
    of the morning's alert while sending is paused (bounce brake) or refused (a missing
    fact in .env). Returns True if it sent."""
    if now.weekday() >= 5 or not RUN_HOUR <= now.hour < SEND_UNTIL:
        return False
    if not db.daily_steps(now.date().isoformat()).get("send", {}).get("tries"):
        return False   # the morning send comes first
    if db.sent_today() >= config.DAILY_LIMIT:
        return False
    import rotation
    import sender
    if sender.bounce_gate() or rotation.refusal() or not sender.plan(dry_run=True, quiet=True):
        return False
    return step("send (top-up)", _send)


def between_checks():
    """One small step of name checks and of lead preparation; logged only when it did
    something."""
    import names
    import prepare
    try:
        checked, left = names.prepare()
        if checked:
            log(f"names: checked {checked} leads, {left} still to check")
        worked = prepare.step(score_batch=prepare.SCORE_BATCH // 2, lead_batch=2)
        if worked:
            log(f"prepare: worked on {worked} leads")
    except Exception as e:
        log(f"between checks: FAILED - {e}")


def run():
    log(f"auto started: weekdays send from {RUN_HOUR}:00 (topped up until {SEND_UNTIL}:00), "
        f"prepare from {PREPARE_HOUR}:00 Nairobi time, inbox every {config.POLL_MINUTES} min. Ctrl+C to stop.")
    while True:
        now = datetime.now(NAIROBI)
        if daily_due(now):
            daily_run(now)
        top_up(datetime.now(NAIROBI))
        step("inbox", _inbox)
        between_checks()
        time.sleep(config.POLL_MINUTES * 60)
