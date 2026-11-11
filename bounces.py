"""Bounces: what kind each one is, why the server refused it, and a summary to learn from.

Every bounce is recorded in the bounces table, whether it came back as a delivery report
(inbox.py) or the mail server refused the address while sending (sender.py, campaigns.py):

  hard     no such user, domain not found, or another permanent refusal: that address is
           suppressed at once, so nothing (first email, follow-up, campaign, CRM) goes to it again
  soft     mailbox full, try later: nothing is stopped; the lead isn't tried again for
           SOFT_PAUSE_HOURS, then sending carries on as before
  unknown  the report doesn't say: treated as hard, as every bounce was before

  python main.py bounces [days]     bounces in the last N days (default 30) by email type,
                                    lead source and step, and the most common reasons

The summary never shows a lead's name or address: reasons have addresses, host names and IP
addresses taken out.
"""
import json
import re
from collections import Counter

import db

DAYS = 30
SOFT_PAUSE_HOURS = 24
TOP_REASONS = 10

HARD, SOFT, UNKNOWN = "hard", "soft", "unknown"
# A full or over-quota mailbox is soft whatever status the server gave it (often 5.2.2 or 552)
MAILBOX_FULL = re.compile(r"mailbox (?:is )?full|over ?quota|quota (?:exceeded|full)|exceeded (?:the )?(?:storage|quota)"
                          r"|insufficient (?:system )?storage|mailbox size limit", re.I)
NO_SUCH = re.compile(r"no such (?:user|mailbox|recipient|account|address)|user (?:unknown|not found)|unknown (?:user|recipient)"
                     r"|recipient (?:address rejected|not found|unknown)|(?:address|mailbox|account) (?:not found|does ?n[o']t exist)"
                     r"|does not exist|doesn'?t exist|invalid (?:recipient|mailbox|address)|mailbox unavailable"
                     r"|account (?:has been )?disabled|domain not found|host or domain name not found|name service error"
                     r"|no mx|nxdomain|unroutable|unrouteable"
                     r"|group you tried to contact .{0,80}may not exist", re.I)
# The report itself says the failure is final (Exim: "This is a permanent error")
PERMANENT = re.compile(r"permanent (?:error|failure)|permanently (?:rejected|failed)", re.I)
TRY_LATER = re.compile(r"try (?:again )?later|temporar(?:il)?y|greylist|rate limit|too many (?:connections|messages)", re.I)
ENHANCED = re.compile(r"\b([245])\.\d{1,3}\.\d{1,3}\b")
BASIC = re.compile(r"\b([245])\d\d\b")
STATUS = re.compile(r"^Status:\s*([245]\.\d{1,3}\.\d{1,3})", re.I | re.M)
DIAGNOSTIC = re.compile(r"^Diagnostic-Code:\s*(.*(?:\r?\n[ \t]+.*)*)", re.I | re.M)
# Postfix and Exim write the remote server's words after "said:" in the readable part
SAID = re.compile(r"said:\s*(.*(?:\r?\n[ \t]+.*)*)", re.I)
# Postfix without a reply from the remote server: "<a@b.c>: connect to host[ip]:25: Connection timed out"
POSTFIX_LINE = re.compile(r"^<[^>\s]+@[^>\s]+>:\s*(.+(?:\r?\n[ \t]+.*)*)", re.M)
# Exim: the failed address on its own line, the reason on the indented lines under it
EXIM_BLOCK = re.compile(r"^[ \t]+<?[^\s@<>]+@[^\s<>]+>?[ \t]*\r?\n((?:[ \t]{3,}\S.*(?:\r?\n|$))+)", re.M)
# Where the copy of our own email starts in a report: never read as the server's words
RETURNED = re.compile(r"^(?:content-type:\s*(?:message/rfc822|text/rfc822-headers)|-+ ?this is a copy of the message"
                      r"|-+ ?original message|-+ ?below this line is a copy)", re.I | re.M)
MESSAGE_ID = re.compile(r"^Message-ID:\s*(<[^>\s]+>)", re.I | re.M)


def _one_line(text):
    return " ".join((text or "").split())


def classify(reason, status=None, context=""):
    """(kind, code) for a bounce, from the server's reason, the report's Status if it gives one
    (e.g. "5.1.1") and the report's own words (`context`, never the copy of our email).
    code is the status or the first SMTP code in the reason, or ""."""
    reason = reason or ""
    words = f"{reason}\n{context or ''}"
    m = ENHANCED.search(status or "") or ENHANCED.search(reason) or BASIC.search(reason)
    code = m.group(0) if m else ""
    klass = m.group(1) if m else ""
    if MAILBOX_FULL.search(words):
        return SOFT, code
    if NO_SUCH.search(words):
        return HARD, code
    if klass == "4":
        return SOFT, code
    if klass == "5" or PERMANENT.search(words):
        return HARD, code
    if TRY_LATER.search(words):
        return SOFT, code
    return UNKNOWN, code


def reason_of(text):
    """The server's own words in a delivery report: its Diagnostic-Code, else what it "said:",
    else the first line that looks like a refusal. "" if there is none."""
    m = DIAGNOSTIC.search(text or "")
    if m:
        return _one_line(re.sub(r"^\s*smtp\s*;\s*", "", m.group(1), flags=re.I))[:300]
    for pattern in (SAID, POSTFIX_LINE, EXIM_BLOCK):
        m = pattern.search(text or "")
        if m:
            return _one_line(m.group(1))[:300]
    for line in (text or "").splitlines():
        if NO_SUCH.search(line) or MAILBOX_FULL.search(line) or ENHANCED.search(line):
            return _one_line(line)[:300]
    return ""


def report_part(raw):
    """A delivery report's own text: everything before the copy of our email it returns."""
    return RETURNED.split(raw or "", maxsplit=1)[0]


def from_report(raw):
    """(kind, code, reason) for a delivery report, from its full text (headers and parts)."""
    status = STATUS.search(raw or "")
    own = report_part(raw)
    reason = reason_of(own)
    kind, code = classify(reason, status.group(1) if status else None, own)
    return kind, code, reason


def from_refusal(code, message):
    """(kind, code, reason) for an address the mail server refused while sending."""
    text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message or "")
    reason = _one_line(f"{code} {text}")[:300]
    kind, found = classify(reason)
    return kind, found or str(code), reason


def sent_message_ids(raw):
    """Message-IDs in a delivery report: its own, and the returned email's (ours)."""
    return MESSAGE_ID.findall(raw or "")


def sent_by_ids(ids):
    """(lead_id, step) of the email of ours among these Message-IDs, or None."""
    if not ids:
        return None
    with db.conn() as c:
        row = c.execute(f"SELECT id, lead_id FROM sent WHERE message_id IN ({','.join('?' * len(ids))})"
                        " ORDER BY id DESC LIMIT 1", list(ids)).fetchone()
        if not row:
            return None
        before = c.execute("SELECT COUNT(*) FROM sent WHERE lead_id=? AND id<?", (row["lead_id"], row["id"])).fetchone()[0]
    return row["lead_id"], before


def emails_sent(lead_id, before=None):
    """How many emails a lead had been sent (by `before`, a UTC date or time, if given)."""
    if lead_id is None:
        return 0
    with db.conn() as c:
        if before:
            return c.execute("SELECT COUNT(*) FROM sent WHERE lead_id=? AND sent_on<=?", (lead_id, before[:10])).fetchone()[0]
        return c.execute("SELECT COUNT(*) FROM sent WHERE lead_id=?", (lead_id,)).fetchone()[0]


def last_step(lead_id, before=None):
    """The step of the last email sent to a lead, or None if it had none: what a delivery
    report that doesn't name the email most likely returned."""
    n = emails_sent(lead_id, before)
    return n - 1 if n else None


def record(lead_id, email, kind, code, reason, step, via, reply_id=None, event_id=None, backfilled=False, at=None):
    """Save one bounce. A hard or unknown one suppresses `email` at once: nothing is sent to that
    address again, whether or not it is still a lead's. Returns the kind. `at` (UTC
    "YYYY-MM-DD HH:MM:SS") is when it happened, for one added afterwards; default now."""
    email = (email or "").lower().strip() or None
    with db.conn() as c:
        c.execute("INSERT INTO bounces (lead_id, email, kind, code, reason, step, via, reply_id, event_id, backfilled,"
                  " created_at) VALUES (?,?,?,?,?,?,?,?,?,?, COALESCE(?, CURRENT_TIMESTAMP)) ON CONFLICT DO NOTHING",
                  (lead_id, email, kind, code or None, reason or None, step, via, reply_id, event_id, int(backfilled),
                   _utc(at)))
        if lead_id is not None and not backfilled:
            db.log_event(c, lead_id, f"{kind}_bounce", email=email, code=code or None, via=via, step=step)
    if kind != SOFT and email and not backfilled:
        db.suppress(email, "bounced")
    return kind


def _utc(at):
    """A stored time as "YYYY-MM-DD HH:MM:SS" (the form every table keeps), or None."""
    if not at:
        return None
    return str(at).replace("T", " ").removesuffix("Z")[:19]


# --- Bounces from before this table existed ---

def backfill():
    """Add each bounce stored before this table existed, once: every delivery report kept in
    replies (its kind and reason read from the text kept) and every address refused while
    sending (no reason was kept: unknown). Returns how many were added. Nothing is suppressed
    or changed: those leads were bounced and suppressed when it happened."""
    added = 0
    with db.conn() as c:
        first_new = c.execute("SELECT MIN(created_at) FROM bounces WHERE backfilled=0").fetchone()[0]
        replies = c.execute("SELECT r.id, r.lead_id, r.from_email, r.body, r.received_at, l.email FROM replies r"
                            " LEFT JOIN leads l ON l.id = r.lead_id WHERE r.label='bounce'"
                            " AND r.id NOT IN (SELECT reply_id FROM bounces WHERE reply_id IS NOT NULL)").fetchall()
        # refusals while sending were logged as the lead's 'bounced' event by the send (or a campaign)
        events = c.execute("SELECT e.id, e.lead_id, e.detail, e.created_at, l.email, l.campaign FROM lead_events e"
                           " JOIN leads l ON l.id = e.lead_id WHERE e.event_type='bounced'"
                           " AND e.id NOT IN (SELECT event_id FROM bounces WHERE event_id IS NOT NULL)"
                           + (" AND e.created_at < ?" if first_new else ""),
                           (first_new,) if first_new else ()).fetchall()
    for r in replies:
        kind, code, reason = from_report(r["body"])
        found = sent_by_ids(sent_message_ids(r["body"]))
        step = found[1] if found else last_step(r["lead_id"], r["received_at"])
        record(r["lead_id"], r["email"], kind, code, reason, step, "inbox", reply_id=r["id"], backfilled=True,
               at=r["received_at"])
        added += 1
    for e in events:
        by = (json.loads(e["detail"] or "{}") or {}).get("by")
        if by != "send" and not (e["campaign"] and by == e["campaign"]):
            continue  # set by the inbox (a report, added above), by hand or from the dashboard
        # refused while sending: the refused email was the next one after those already sent
        record(e["lead_id"], e["email"], UNKNOWN, "", "", emails_sent(e["lead_id"], e["created_at"]),
               "send", event_id=e["id"], backfilled=True, at=e["created_at"])
        added += 1
    return added


# --- The summary ---

ADDRESS = re.compile(r"<?[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}>?", re.I)
URL = re.compile(r"\bhttps?://\S+", re.I)
IP = re.compile(r"\[?\b\d{1,3}(?:\.\d{1,3}){3}\b\]?")
HOST = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
TOKEN = re.compile(r"\b(?=[a-z0-9-]*\d)(?=[a-z0-9-]*[a-z])[a-z0-9-]{12,}\b", re.I)  # queue ids, hashes
IN_REPLY = re.compile(r"\(in reply to [^)]*\)", re.I)
GROUP_NAME = re.compile(r"(group you tried to contact) \([^)]*\)", re.I)  # Google Groups names the inbox


def scrub(reason):
    """A reason with no address, host name, link or IP address in it, for grouping and showing."""
    text = ADDRESS.sub("<address>", reason or "")
    text = URL.sub("", text)
    text = IP.sub("", text)
    text = HOST.sub("<host>", text)
    text = TOKEN.sub("", IN_REPLY.sub("", GROUP_NAME.sub(r"\1", text)))
    text = _one_line(text).strip(" :;,-").lower()
    return text[:90] or "(no reason given)"


def email_type(email):
    """named person, role inbox or info@ for an address."""
    import contacts  # here: it reads websites, which this module never does
    local = (email or "").split("@")[0].lower()
    if local == "info":
        return "info@"
    return "role inbox" if local and contacts.is_role(local) else "named person"


def lead_source(row):
    """Where the lead came from: its campaign (Group A), a regulator's list by its SOURCE key
    (sasra, cbk_psp...: imports.py), CBK (the older directory imports), search, or list."""
    import imports
    if row["lead_id"] is None:
        return "not linked to a lead"
    if row["campaign"]:
        return row["campaign"]
    source = (row["source"] or "").strip()
    if source in imports.SOURCES:
        return source
    if "cbk" in source.lower():
        return "CBK"
    if source == "search":
        return "search"
    return "list"


def step_name(step):
    if step is None:
        return "not known"
    return "first email" if step == 0 else f"follow-up {step}"


def rows(days=DAYS):
    """Bounces recorded in the last `days` days (test leads, source 'manual', left out)."""
    with db.conn() as c:
        return c.execute(f"""
            SELECT b.*, l.source, l.campaign FROM bounces b LEFT JOIN leads l ON l.id = b.lead_id
            WHERE b.created_at >= datetime('now', ?) AND {db.real('l')}
            ORDER BY b.id""", (f"-{int(days)} days",)).fetchall()


def summary(days=DAYS):
    """The report's lines: totals, then tables by email type, lead source and step, then the
    most common reasons. No lead's name or address appears in any of them."""
    found = rows(days)
    kinds = Counter(r["kind"] for r in found)
    lines = [f"Bounces in the last {days} days: {len(found)} "
             f"({kinds[HARD]} hard, {kinds[SOFT]} soft, {kinds[UNKNOWN]} unknown)"]
    if not found:
        return lines
    lines.append("Hard and unknown bounces stop all email to that address at once; soft ones "
                 f"(mailbox full, try later) pause the lead for {SOFT_PAUSE_HOURS} hours.")
    for title, key in (("email type", lambda r: email_type(r["email"])), ("lead source", lead_source),
                       ("step", lambda r: step_name(r["step"]))):
        groups = {}
        for r in found:
            groups.setdefault(key(r), Counter())[r["kind"]] += 1
        lines += ["", f"{'BY ' + title.upper():24}  {'TOTAL':>5}  {'HARD':>4}  {'SOFT':>4}  {'UNKNOWN':>7}"]
        for name, n in sorted(groups.items(), key=lambda g: (-sum(g[1].values()), g[0])):
            lines.append(f"  {name:22}  {sum(n.values()):>5}  {n[HARD]:>4}  {n[SOFT]:>4}  {n[UNKNOWN]:>7}")
    reasons = Counter((r["kind"], scrub(r["reason"])) for r in found)
    lines += ["", "TOP REASONS"]
    for (kind, reason), n in reasons.most_common(TOP_REASONS):
        lines.append(f"  {n:>4}  {kind:7}  {reason}")
    return lines


def report(days=DAYS):
    added = backfill()
    if added:
        print(f"(added {added} bounces from before bounces were recorded)")
    print("\n".join(summary(days)))
