"""Runs LeadBot unattended. Leave `python main.py auto` running.

Weekdays at 9am Nairobi time: discover, verify, personalise, prepare (check company names,
names.py), then send (up to DAILY_LIMIT). The rest of the time: check the inbox every
POLL_MINUTES, and check one small batch of company names after each inbox check.
"""
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import db

NAIROBI = timezone(timedelta(hours=3))  # Kenya has no daylight saving
RUN_HOUR = 9
# Remembers the last daily run so a restart doesn't repeat it
MARKER = Path(__file__).parent / ".auto_last_run"


def log(msg):
    print(f"{datetime.now(NAIROBI):%Y-%m-%d %H:%M} {msg}", flush=True)


def daily_due(now):
    if now.weekday() >= 5 or now.hour != RUN_HOUR:
        return False
    done = MARKER.read_text().strip() if MARKER.exists() else ""
    return done != now.date().isoformat()


def step(name, fn):
    try:
        log(f"{name}: {fn()}")
    except Exception as e:
        log(f"{name}: FAILED - {e}")


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


def daily_run(now):
    MARKER.write_text(now.date().isoformat())
    step("discover", _discover)
    step("verify", _verify)
    step("personalise", _personalise)
    step("prepare", _prepare)
    step("send", _send)


def run():
    log(f"auto started: daily run on weekdays at {RUN_HOUR}:00 Nairobi time, "
        f"inbox every {config.POLL_MINUTES} min. Ctrl+C to stop.")
    while True:
        now = datetime.now(NAIROBI)
        if daily_due(now):
            daily_run(now)
        step("inbox", _inbox)
        _prepare_batch()
        time.sleep(config.POLL_MINUTES * 60)
