"""Which leads answer: every lead that has been emailed, grouped several ways, to check the need
score (targeting.py) and the other choices against what actually happened.

  python main.py score-report [days]   all emailed leads (or those first emailed in the last N
                                       days) by score band, scoring reason, niche, lead source,
                                       variant, named contact vs general inbox and company age

For each group: leads emailed, replied, interested and meetings, and the reply rate (replied
out of emailed). A group with fewer than MIN_EMAILS leads emailed is marked "too few to judge".

Counted per lead, the way the CRM's report counts them: replied = any reply that isn't a bounce
or an out-of-office; interested = a reply labelled interested, or the lead got to a meeting;
meetings = the lead's status is or was meeting (meeting, client, or a meeting date recorded).
Test leads (source 'manual') are left out. No lead's name or address appears in the output:
scoring reasons are shown without the website, address or quoted text they named.
"""
import json
import re

import bounces
import db

MIN_EMAILS = 20
BANDS = ((70, "70+"), (50, "50-69"), (30, "30-49"), (0, "0-29"))
NOT_SCORED = "not scored"
# (most years, name): how long ago the company started (ranking.py estimates est_year)
AGES = ((3, "0-3 years"), (6, "4-6 years"), (10, "7-10 years"), (None, "11+ years"))
AGE_UNKNOWN = "not known"
POINTS = re.compile(r"^[+-]\d+\s+")
# what a reason names in brackets at its end: a domain, an address, a date, text quoted from the
# site ("app"), an industry. Taken out so reasons group together and nothing identifies a lead
DETAIL = re.compile(r"\s*\([^()]*\)\s*$")


def band(score):
    if score is None:
        return NOT_SCORED
    return next(name for low, name in BANDS if score >= low)


def age_group(est_year, this_year):
    if not est_year:
        return AGE_UNKNOWN
    age = max(0, this_year - int(est_year))
    return next(name for most, name in AGES if most is None or age <= most)


def reason_name(reason):
    """A saved scoring reason without its points or the detail in brackets:
    '+15 site mentions an app or Play Store ("app")' -> 'site mentions an app or Play Store'."""
    text = POINTS.sub("", (reason or "").strip())
    while DETAIL.search(text):
        text = DETAIL.sub("", text)
    text = text.strip()
    return "start (base points for the niche)" if text == "start" else text or None


def reasons(raw):
    """The distinct reason names in a lead's saved score_reasons (a JSON list)."""
    try:
        found = json.loads(raw or "[]")
    except ValueError:
        return []
    names = {reason_name(r) for r in found if isinstance(r, str)}
    return sorted(n for n in names if n)


def rows(days=None):
    """One row per emailed lead (test leads left out): first emailed in the last `days` days if given."""
    since = "AND first_sent >= date('now', ?)" if days else ""
    with db.conn() as c:
        return c.execute(f"""
            SELECT * FROM (
                SELECT l.id AS lead_id, l.industry, l.status, l.source, l.campaign, l.score, l.scored_at,
                       l.score_reasons, l.template_variant, l.est_year, l.meeting_at,
                       (SELECT MIN(s.sent_on) FROM sent s WHERE s.lead_id = l.id) AS first_sent,
                       (SELECT f.to_contact FROM sent f WHERE f.lead_id = l.id ORDER BY f.id LIMIT 1) AS to_contact,
                       (SELECT COUNT(*) FROM replies r WHERE r.lead_id = l.id
                          AND COALESCE(r.label, '') NOT IN ('bounce', 'out_of_office')) AS replies,
                       (SELECT COUNT(*) FROM replies r WHERE r.lead_id = l.id AND r.label = 'interested') AS interested
                FROM leads l
                WHERE {db.real('l')} AND EXISTS (SELECT 1 FROM sent s WHERE s.lead_id = l.id)
            ) t WHERE 1=1 {since} ORDER BY lead_id""", ((f"-{int(days)} days",) if days else ())).fetchall()


def outcome(r):
    meeting = r["status"] in ("meeting", "client") or bool(r["meeting_at"])
    return {"emailed": 1, "replied": int(r["replies"] > 0),
            "interested": int(r["interested"] > 0 or meeting), "meetings": int(meeting)}


def contact_kind(to_contact):
    if to_contact is None:
        return "not recorded"
    return "named contact" if to_contact else "general inbox"


def groups(found, this_year):
    """{section title: {group name: counts}} for the emailed leads `found`."""
    keys = {
        "score band": lambda r: [band(r["score"] if r["scored_at"] else None)],
        "scoring reason": lambda r: reasons(r["score_reasons"]) if r["scored_at"] else [],
        "niche": lambda r: [r["industry"] or "no niche"],
        "lead source": lambda r: [bounces.lead_source(r)],
        "variant": lambda r: [r["template_variant"] or "none (before the rotation)"],
        "named contact vs general inbox": lambda r: [contact_kind(r["to_contact"])],
        "company age": lambda r: [age_group(r["est_year"], this_year)],
    }
    out = {title: {} for title in keys}
    for r in found:
        result = outcome(r)
        for title, key in keys.items():
            for name in key(r):
                counts = out[title].setdefault(name, dict.fromkeys(result, 0))
                for k, v in result.items():
                    counts[k] += v
    return out


ORDER = {"score band": [name for _, name in BANDS] + [NOT_SCORED],
         "company age": [name for _, name in AGES] + [AGE_UNKNOWN]}


def summary(days=None, this_year=None):
    """The report's lines. No lead's name or address appears in any of them."""
    from datetime import date
    found = rows(days)
    period = f"first emailed in the last {days} days" if days else "all emailed leads"
    lines = [f"Score report: {len(found)} leads ({period})"]
    if not found:
        return lines
    lines.append(f'Reply rate = replied / emailed. Under {MIN_EMAILS} emailed: "too few to judge".')
    for title, named in groups(found, this_year or date.today().year).items():
        order = ORDER.get(title)
        items = sorted(named.items(), key=lambda g: (order.index(g[0]) if order and g[0] in order else len(order or ()),
                                                     -g[1]["emailed"], g[0]))
        width = max([len(title) + 3] + [len(n) + 2 for n in named])
        lines += ["", f"{'BY ' + title.upper():{width}}  {'EMAILED':>7}  {'REPLIED':>7}  {'INTERESTED':>10}  "
                      f"{'MEETINGS':>8}  {'REPLY RATE':>10}"]
        for name, n in items:
            rate = f"{100 * n['replied'] / n['emailed']:.1f}%"
            judge = "  too few to judge" if n["emailed"] < MIN_EMAILS else ""
            lines.append(f"  {name:{width - 2}}  {n['emailed']:>7}  {n['replied']:>7}  {n['interested']:>10}  "
                         f"{n['meetings']:>8}  {rate:>10}{judge}")
        if title == "scoring reason":
            lines.append("  (a lead counts under every reason its score had; leads not scored are under none)")
    return lines


def report(days=None):
    print("\n".join(summary(days)))
