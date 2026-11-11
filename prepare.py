"""Gets waiting leads ready for their first email, away from the send, so a send run never
spends its time reading websites or asking the AI:

  1. need score (targeting.py), for every waiting lead in an approved industry, in batches
  2. for the leads the next sends would pick, best first (sender.batch), until TARGET_FACTOR
     x DAILY_LIMIT of them are ready: the niche check (nichefit.py), the contact on the site
     (contacts.py), with USE_AI_OPENER the personal opening (personalise.py), and for a lead
     found more than contacts.RECHECK_AFTER_DAYS ago, whether its address is still on its
     website (contacts.recheck_addresses)

sender.plan() only picks leads that are ready (sender.prepared). The bot runs this after 5pm
on weekdays and one small step between inbox checks (auto.py).

  python main.py prepare            run until enough leads are ready (or an hour has passed)
"""
import time

import config
import contacts
import nichefit
import personalise
import sender
import targeting

TARGET_FACTOR = 2      # aim for this many days of first emails ready
SCORE_BATCH = 20       # leads scored per step (their sites are read in parallel)
LEAD_BATCH = 5         # leads niche-checked, contact-checked and personalised per step
RUN_SECONDS = 3600     # a run stops after this long; the steps between inbox checks go on


def target():
    return TARGET_FACTOR * config.DAILY_LIMIT


def ready():
    """The waiting leads ready for their first email, in sending order."""
    return [l for l in sender.approved_waiting(quiet=True) if sender.prepared(l)]


def step(skip=None, score_batch=SCORE_BATCH, lead_batch=LEAD_BATCH):
    """One step: score up to score_batch unscored waiting leads; when none is left, get up to
    lead_batch of the leads the next sends would pick ready. Leads that couldn't be got ready
    (a site that can't be read today, an AI failure) are added to `skip`, so a run doesn't
    try them again. Returns how many leads it worked on: 0 when there is nothing to do or
    enough leads are ready."""
    skip = set() if skip is None else skip
    todo = [l for l in targeting.unscored() if l["id"] not in skip][:score_batch]
    if todo:
        targeting.score_leads(todo, quiet=True)
        return len(todo)
    pool = [l for l in sender.approved_waiting(quiet=True) if l["id"] not in skip]
    wanted = sender.batch(pool, target())
    todo = [l for l in wanted if not sender.prepared(l)][:lead_batch]
    if not todo:
        return 0
    skip |= nichefit.check(todo)
    ids = {l["id"] for l in todo} - skip
    # read again: a lead that fits no niche has dropped out, one that moved niche is scored next
    checked = [l for l in sender.approved_waiting(quiet=True) if l["id"] in ids and not nichefit.needs_check(l)]
    contacts.run(checked)
    personalise.run(checked)
    # a lead found long ago: is its address still on its website? (read again after contacts.run,
    # which may have changed the address)
    contacts.recheck_addresses([l for l in sender.approved_waiting(quiet=True) if l["id"] in ids])
    still = {l["id"] for l in sender.approved_waiting(quiet=True) if l["id"] in ids and not sender.prepared(l)
             and targeting.scored(l)}
    skip |= still   # e.g. the AI couldn't write its opening: tried again next run
    return len(todo)


def run(seconds=RUN_SECONDS):
    """Steps until enough leads are ready, nothing is left to do, or `seconds` have passed."""
    lists = list_websites()
    skip, worked, start = set(), 0, time.monotonic()
    while time.monotonic() - start < seconds:
        n = step(skip)
        if not n:
            break
        worked += n
    return (f"worked on {worked} leads; {len(ready())} ready for first emails (aiming for {target()})"
            + (f"; {lists}" if lists else ""))


def list_websites():
    """Organisations from regulators' lists still waiting: today's batch of website lookups
    and sites read again (imports.daily). An error here never stops the rest of the job."""
    import imports
    try:
        n = imports.daily()
    except Exception as e:
        return f"list websites: FAILED ({e})"
    if not (n["searched"] or n["retried"]):
        return ""
    return f"list websites: searched {n['searched']}, read again {n['retried']}, added {n['added']}"
