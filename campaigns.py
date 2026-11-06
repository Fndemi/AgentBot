"""Named-contact campaigns: hand-written emails to named people, sent by the bot.

  python main.py send-group campaigns/group_a.yaml --dry-run   check every address, show what would go
  python main.py send-group campaigns/group_a.yaml             send the entries not sent yet

The file is a YAML list; each entry has to, contact_name, company, website, subject, body and
followup_body. contact_name and followup_body may be empty: an entry without a followup_body is
sent normally but gets no automatic follow-up. A file whose name ends in "_hold"
("group_a_hold.yaml") keeps entries back: it is never sent by any command (a dry run may preview
it), and the bot never reads campaign files at all. The campaign is named after the file ("group_a.yaml" -> "Group A"). Entries are
sent word for word, as plain text, from GROUP_FROM_NAME <GROUP_FROM_EMAIL> through the bot's own
mail server, with a List-Unsubscribe header; the Message-ID is saved and a copy filed in Sent.
Only entries not sent before go out, at most GROUP_DAILY_LIMIT a day per campaign, counted toward
DAILY_LIMIT, so more entries can be added to the file and the same command sends only those.

Before sending, every address is checked: its domain must receive mail (MX or A record) or the
entry is skipped, and the company's website is read to see whether the address is published.
An entry whose company the bot or another campaign has already emailed (a lead with a sent email
whose address or website is on the entry's address or website domain; free-mail domains such as
gmail.com don't count) is skipped as "company already emailed by LeadBot (address, date)", shown
in the dry run too, unless the entry has allow_same_company: true.

Each entry becomes a lead (status sent, industry "named_contact", campaign "Group A"), so its
replies are linked and it shows in the CRM. The bot never sends its own templates or follow-ups
to it, nor to anyone on its domains (do_not_contact, which discovery checks too). Instead, after
GROUP_FOLLOWUP_AFTER_DAYS working days with no reply, the bot sends that entry's followup_body
once, in the same thread (send_followups, run with every send).
"""
import os
import smtplib
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from urllib.parse import urljoin

import yaml

import bounces
import config
import db
import ranking
import rotation
import scraper
import sender
import verify

INDUSTRY = "named_contact"
FIELDS = ("to", "contact_name", "company", "website", "subject", "body", "followup_body")
# allow_same_company: true sends the entry even when its company has been emailed before (company_emailed)
FLAGS = ("allow_same_company",)
# contact_name may be blank (info@...); so may followup_body: that entry gets no automatic follow-up
NEEDED = ("to", "company", "subject", "body")
# A file whose name ends like this holds entries kept back ("group_a_hold.yaml"): it is never sent,
# by any command; a dry run may preview it. To send one of its entries, move it to the campaign's file.
HOLD_SUFFIX = "_hold"
SEND_DELAY_SECONDS = 8   # between emails, so a run of GROUP_DAILY_LIMIT stays short
CHECK_WORKERS = 8


def campaign_name(path):
    """The campaign's name from its file: "campaigns/group_a.yaml" -> "Group A"."""
    return " ".join(w.capitalize() if len(w) > 1 else w.upper() for w in Path(path).stem.split("_"))


def load(path):
    """The file's entries, as dicts with every field (missing ones ""), texts as written apart
    from blank lines at their start and end."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a YAML list of entries")
    entries = []
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: entry {i} is not a set of fields")
        unknown = set(item) - set(FIELDS) - set(FLAGS)
        if unknown:
            raise ValueError(f"{path}: entry {i} has unknown fields: {', '.join(sorted(unknown))}")
        entry = {k: str(item.get(k) or "").strip("\n") for k in FIELDS}
        for flag in FLAGS:
            if not isinstance(item.get(flag, False), bool):
                raise ValueError(f"{path}: entry {i}: {flag} must be true or false")
            entry[flag] = item.get(flag, False)
        entry["to"] = entry["to"].strip().lower()
        entries.append(entry)
    return entries


def missing(entry):
    return [f for f in NEEDED if not entry[f].strip()]


# --- Checking an address ---
def published_on(address, website):
    """The page of the company's website that shows this address (any way scraper.page_emails
    finds one, Cloudflare-hidden ones included), or None."""
    if not website:
        return None
    home = website if website.startswith("http") else f"https://{website}"
    for path in scraper.CONTACT_PATHS:
        url = urljoin(home, path)
        soup = scraper.fetch(url)
        if soup is not None and address in {e.lower() for e in scraper.page_emails(soup)}:
            return url
    return None


def check(entry):
    """{"domain_ok": the address's domain has an MX or A record, "page": where the website
    publishes the address, or None}."""
    domain = db.email_domain(entry["to"])
    return {"domain_ok": bool(domain) and verify.domain_ok(domain),
            "page": published_on(entry["to"], entry["website"] or domain)}


def check_all(entries):
    with ThreadPoolExecutor(CHECK_WORKERS) as pool:
        return list(pool.map(check, entries))


# --- What has gone out ---
def lead_of(address):
    return db.find_lead_by_email(address)


def emails_sent(lead_id):
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) FROM sent WHERE lead_id=?", (lead_id,)).fetchone()[0]


def sent_today(campaign):
    """Emails of this campaign sent today (first emails and follow-ups)."""
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) FROM sent s JOIN leads l ON l.id = s.lead_id"
                         " WHERE l.campaign = ? AND s.sent_on = ?", (campaign, date.today().isoformat())).fetchone()[0]


def room(campaign):
    """Emails this campaign may still send today: GROUP_DAILY_LIMIT, within DAILY_LIMIT."""
    return max(0, min(config.GROUP_DAILY_LIMIT - sent_today(campaign), config.DAILY_LIMIT - db.sent_today()))


def company_domains(entry):
    """The entry's address and website domains, free-mail ones (gmail.com...) left out."""
    return {d for d in (db.email_domain(entry["to"]), db.email_domain(entry["website"]))
            if d and d not in ranking.FREE_MAIL}


def company_emailed(entry, campaign):
    """(address, date) of the first email the bot or another campaign sent to someone at the
    entry's company: a lead whose address or website is on one of company_domains(entry).
    This campaign's own leads don't count. None if there is no such email."""
    domains = company_domains(entry)
    if not domains:
        return None
    with db.conn() as c:
        rows = c.execute("SELECT l.email, l.website, l.campaign, MIN(s.sent_on) AS first_sent"
                         " FROM leads l JOIN sent s ON s.lead_id = l.id"
                         " GROUP BY l.id, l.email, l.website, l.campaign ORDER BY first_sent, l.id").fetchall()
    for r in rows:
        if r["campaign"] == campaign or r["email"] == entry["to"]:
            continue
        theirs = {db.email_domain(r["email"]), db.email_domain(r["website"])} - {""}
        if theirs & domains:
            return r["email"], str(r["first_sent"])
    return None


def why_not(entry, campaign):
    """Why this entry won't be sent, or None if it will (when there's room today)."""
    if missing(entry):
        return "incomplete: no " + ", ".join(missing(entry))
    if db.is_suppressed(entry["to"]):
        return "opted out or bounced before (suppression list)"
    lead = lead_of(entry["to"])
    if lead and emails_sent(lead["id"]):
        return ("already sent" if lead["campaign"] == campaign
                else f"already emailed by {'the ' + lead['campaign'] + ' campaign' if lead['campaign'] else 'LeadBot'}")
    if not entry.get("allow_same_company"):
        earlier = company_emailed(entry, campaign)
        if earlier:
            return f"company already emailed by LeadBot ({earlier[0]}, {earlier[1]})"
    return None


# --- Sending ---
def message(entry, subject, body, in_reply_to=None, references=None):
    """The email exactly as written: plain text, from GROUP_FROM_NAME <GROUP_FROM_EMAIL>."""
    sender_address = config.GROUP_FROM_EMAIL or config.FROM_EMAIL
    msg = EmailMessage()
    msg["From"] = formataddr((config.GROUP_FROM_NAME, sender_address))
    msg["To"] = entry["to"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_address.split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    msg["List-Unsubscribe"] = f"<mailto:{sender_address}?subject=unsubscribe>"
    msg.set_content(body + "\n")
    return msg


def make_lead(entry, campaign):
    """The entry as a lead, ready for its first email (status verified until it goes out):
    its campaign, the follow-up to send and its named contact. A lead the bot found but never
    emailed becomes the campaign's, so the bot's templates never go to it."""
    if not lead_of(entry["to"]):
        db.add_lead(entry["company"], entry["website"] or None, INDUSTRY, entry["to"], "campaign")
    lead = lead_of(entry["to"])
    with db.conn() as c:
        c.execute("UPDATE leads SET company=?, website=?, industry=?, campaign=?, campaign_followup=?,"
                  " contact_name=?, contact_email=?, display_name=NULL WHERE id=?",
                  (entry["company"], entry["website"] or lead["website"], INDUSTRY, campaign,
                   entry["followup_body"], entry["contact_name"] or None,
                   entry["to"] if entry["contact_name"] else None, lead["id"]))
    db.set_status(lead["id"], "verified", campaign)
    return lead_of(entry["to"])


def block_domains(entry, campaign):
    """Put the company's domains on the do-not-contact list (never a free-mail one)."""
    for domain in {db.email_domain(entry["to"]), db.email_domain(entry["website"])}:
        if domain and domain not in ranking.FREE_MAIL:
            db.add_do_not_contact(domain, f"{campaign} campaign")


def deliver(smtp, lead, msg, step, campaign):
    """Claim, send, record and file one email; returns "sent", "bounced", "limit" or a reason
    it wasn't sent. The claim (db.claim_send) rechecks the lead and DAILY_LIMIT at the last moment."""
    owner = f"{socket.gethostname()}:{os.getpid()}:{campaign}"
    refused = db.claim_send(lead["id"], step, config.DAILY_LIMIT, owner)
    if refused:
        return "limit" if refused.startswith("daily limit") else refused
    try:
        smtp.send_message(msg)
    except smtplib.SMTPRecipientsRefused as e:
        db.release_claim(lead["id"])
        code, text = next(iter(e.recipients.values()), (0, b""))
        kind, code, reason = bounces.from_refusal(code, text)
        bounces.record(lead["id"], lead["email"], kind, code, reason, step, "send")
        if kind == bounces.SOFT:  # mailbox full, try later: run the campaign again another day
            return f"not sent (soft bounce, try again later: {reason})"
        db.set_status(lead["id"], "bounced", campaign)
        return "bounced"
    except (smtplib.SMTPException, OSError) as e:
        db.release_claim(lead["id"])
        return f"not sent ({e})"
    db.record_sent(lead["id"], msg["Message-ID"], f"campaign:{campaign}", str(msg["Subject"]),
                   msg.get_content(), "campaign" if not step else "campaign_followup", None,
                   with_opener=0, to_contact=int(bool(lead["contact_name"])))
    if not step:
        db.set_status(lead["id"], "sent", campaign)
    sender.save_to_sent(msg, time.time())
    return "sent"


def report(rows):
    print(f"{'ADDRESS':34}  {'DOMAIN OK':9}  PUBLISHED")
    for entry, result in rows:
        print(f"{entry['to']:34}  {'yes' if result['domain_ok'] else 'NO':9}  "
              f"{'yes, ' + result['page'] if result['page'] else 'no'}")


def on_hold(path):
    """Whether a campaign file is kept back (its name ends in HOLD_SUFFIX): never sent."""
    return Path(path).stem.lower().endswith(HOLD_SUFFIX)


def followup_note(entry):
    if not entry["followup_body"].strip():
        return "no follow-up"
    return f"follow-up after {config.GROUP_FOLLOWUP_AFTER_DAYS} working days without a reply"


def send_group(path, dry_run=False, connect=sender.connect):
    """Check and send the file's entries not sent yet. Returns {address: what happened}.
    A file on hold (on_hold) is never sent: a dry run previews it, a real run refuses."""
    if on_hold(path) and not dry_run:
        print(f"{Path(path).name} is on hold: its entries are never sent. To send one, move it into "
              "the campaign's own file. Nothing was sent.")
        return {e["to"]: "on hold: never sent" for e in load(path)}
    if on_hold(path):
        print(f"{Path(path).name} is ON HOLD: preview only, it can never be sent.\n")
    campaign = campaign_name(path)
    entries = load(path)
    outcome = {e["to"]: why_not(e, campaign) for e in entries}
    todo = [e for e in entries if not outcome[e["to"]]]
    print(f"{campaign}: {len(entries)} entries, {len(todo)} ready to send (not sent before, nothing missing).")
    for e in entries:
        if outcome[e["to"]]:
            print(f"  {e['to']}: {outcome[e['to']]}")
    if not todo:
        return outcome
    for e in todo:
        print(f"  {e['to']}: {followup_note(e)}")
    print("\nChecking each address (mail domain, and whether the company's website shows it)...")
    results = check_all(todo)
    report(list(zip(todo, results)))
    sendable = []
    for entry, result in zip(todo, results):
        if result["domain_ok"]:
            sendable.append(entry)
        else:
            outcome[entry["to"]] = "skipped: its domain receives no mail (no MX or A record)"
    space = room(campaign)
    print(f"\nToday {campaign} may send {space} more (GROUP_DAILY_LIMIT {config.GROUP_DAILY_LIMIT}, "
          f"DAILY_LIMIT {config.DAILY_LIMIT}, {db.sent_today()} sent today in all).")
    for entry in sendable[space:]:
        outcome[entry["to"]] = "waiting: today's limit is reached; run it again tomorrow"
    sendable = sendable[:space]
    if dry_run:
        for entry in sendable:
            outcome[entry["to"]] = "would send"
            print(f"\n--- would send to {entry['to']} ({followup_note(entry)}) ---\nFrom: {config.GROUP_FROM_NAME} "
                  f"<{config.GROUP_FROM_EMAIL or config.FROM_EMAIL}>\nSubject: {entry['subject']}\n\n{entry['body']}")
        print("\n(dry run) Nothing was sent or saved.")
        return outcome
    if not sendable:
        return outcome
    owner = sender.lock_owner()
    if not sender.take_lock(owner):
        print("Another send run is in progress. Nothing was sent; try again in a few minutes.")
        return outcome
    try:
        with sender.keep_lock(owner):
            smtp = connect()
            try:
                for i, entry in enumerate(sendable):
                    lead = make_lead(entry, campaign)
                    block_domains(entry, campaign)
                    result = deliver(smtp, lead, message(entry, entry["subject"], entry["body"]), 0, campaign)
                    outcome[entry["to"]] = result
                    print(f"  {entry['to']}: {result}")
                    if result == "limit":
                        break
                    if i < len(sendable) - 1:
                        time.sleep(SEND_DELAY_SECONDS)
            finally:
                sender.close(smtp)
    finally:
        db.release_lock(sender.SEND_LOCK, owner)
    return outcome


# --- The one follow-up ---
def followups_due(today=None):
    """Campaign leads due their follow-up: still at sent (no reply), exactly one email so far,
    a follow-up text, not opted out, and GROUP_FOLLOWUP_AFTER_DAYS working days since the
    first email. Each row is the lead with the first email's message_id, subject and date."""
    today = today or rotation.today()
    with db.conn() as c:
        rows = c.execute("""
            SELECT l.*, s.message_id, s.sent_subject, s.sent_on AS first_sent_on FROM leads l
            JOIN sent s ON s.lead_id = l.id
            WHERE l.campaign IS NOT NULL AND l.status = 'sent' AND COALESCE(l.campaign_followup, '') != ''
              AND (SELECT COUNT(*) FROM sent s2 WHERE s2.lead_id = l.id) = 1
            ORDER BY s.id""").fetchall()
    suppressed = db.suppressed_emails()
    return [r for r in rows if r["email"] not in suppressed
            and rotation.working_days_after(date.fromisoformat(r["first_sent_on"]),
                                            config.GROUP_FOLLOWUP_AFTER_DAYS) <= today]


def send_followups(connect=sender.connect):
    """Send every campaign follow-up that is due, within each campaign's daily room. The bot
    runs this with every send (auto.py). Returns how many went out."""
    due = followups_due()
    if not due:
        return 0
    smtp, sent = None, 0
    try:
        for lead in due:
            if room(lead["campaign"]) <= 0:
                continue
            smtp = smtp or connect()
            subject = lead["sent_subject"] or ""
            entry = {"to": lead["email"]}
            msg = message(entry, subject if subject.lower().startswith("re:") else f"Re: {subject}",
                          lead["campaign_followup"], in_reply_to=lead["message_id"])
            result = deliver(smtp, lead, msg, 1, lead["campaign"])
            print(f"  {lead['campaign']} follow-up to {lead['email']}: {result}")
            sent += result == "sent"
            if result == "limit":
                break
            time.sleep(SEND_DELAY_SECONDS)
    finally:
        if smtp:
            sender.close(smtp)
    return sent


def main(args):
    if not args:
        sys.exit("Usage: python main.py send-group FILE [--dry-run]")
    send_group(args[0], dry_run="--dry-run" in args)
