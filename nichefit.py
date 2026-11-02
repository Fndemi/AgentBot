"""Before a lead gets its first email, the AI (GROQ_MODEL) reads its website and says which
approved niche in uses.yaml it really belongs to. A lead that fits another approved niche
moves to it (its template and score follow); one that fits none is never emailed
(niche_fit 'none'), so a tour company doesn't get the tech_startup email.

Leads imported from a regulator's directory (targeting.py) are not checked: the directory
says what they are. A lead whose website can't be read, or that the AI couldn't answer for,
is skipped today and checked again at the next send.
"""
import json

import classifier
import db
import personalise
import targeting

MAX_TEXT = 4000
NONE = "none"
# What each niche is, for the AI. A niche not listed here is described by its name.
DESCRIPTIONS = {
    "sacco": "a savings and credit cooperative (SACCO)",
    "microfinance": "a microfinance institution or bank lending to small businesses and individuals",
    "digital_lender": "a digital or mobile lender giving loans through an app or USSD",
    "tech_startup": "a technology startup building its own digital product (app or platform)",
    "fintech": "a financial technology company (payments, lending, savings or insurance products)",
    "saas": "a company selling its own software as a subscription service",
    "ecommerce": "an online shop or e-commerce platform selling goods",
    "delivery_app": "a delivery or ride app (food, parcels, groceries, transport)",
    "edtech": "an education technology company (online learning, school software)",
    "healthtech": "a health technology company (telemedicine, health apps, clinic software)",
    "payment_provider": "a payment service provider, gateway or merchant payments company",
    "payg_energy": "a pay-as-you-go solar or energy company",
    "software_agency": "a software development agency building systems for clients",
    "software_vendor": "a company selling its own business software (school, SACCO, hospital, ERP systems)",
    "hr_payroll": "an HR or payroll software or services company",
    "school": "a primary or secondary school",
    "church": "a church or ministry",
}
PROMPT = """Which kind of organisation is this Kenyan website? Pick exactly one niche from the
list, or "none" if the organisation is none of them (for example a tour company, a hotel, a
law firm, a directory, an event or a news site is "none"). Judge by what the organisation
itself does, not by who it serves.

Niches:
{niches}

Company: {company}
Website: {website}
Website text:
\"\"\"{text}\"\"\"

Answer in JSON: {{"niche": "<one niche name from the list, or none>", "reason": "<one short sentence>"}}"""


def approved_niches():
    return [k for k, v in personalise.USES.items() if k != "default" and v.get("approved")]


def needs_check(lead):
    lead = dict(lead)
    return lead.get("niche_checked_at") is None and not lead.get("imported_at")


def site_niche(lead):
    """(niche, reason) for one lead from its website: an approved niche or NONE.
    (None, why) when its website can't be read. Raises if the AI fails or answers badly."""
    lead = dict(lead)
    text = targeting.site_text(lead.get("website"))
    if not text:
        return None, "website couldn't be read"
    niches = approved_niches()
    raw = classifier.ask_with_retry(PROMPT.format(
        niches="\n".join(f"- {n}: {DESCRIPTIONS.get(n, n.replace('_', ' '))}" for n in niches),
        company=lead.get("company") or "", website=lead.get("website") or "", text=text[:MAX_TEXT]))
    answer = json.loads(raw)
    niche = str(answer.get("niche") or "").strip().lower()
    if niche != NONE and niche not in niches:
        raise ValueError(f"the AI named a niche that isn't approved: {niche!r}")
    return niche, str(answer.get("reason") or "").strip()[:300]


def check(leads):
    """Check the leads that need it. Returns the ids of those that couldn't be checked today."""
    later = set()
    for lead in leads:
        if not needs_check(lead):
            continue
        name = lead["company"] or lead["email"]
        try:
            niche, reason = site_niche(lead)
        except Exception as e:
            print(f"  niche check: {name}: the AI failed ({type(e).__name__}); skipped today")
            later.add(lead["id"])
            continue
        if niche is None:
            print(f"  niche check: {name}: {reason}; skipped today")
            later.add(lead["id"])
            continue
        moved = niche if niche not in (NONE, lead["industry"]) else None
        db.save_niche_fit(lead["id"], niche, reason, industry=moved)
        if niche == NONE:
            print(f"  niche check: {name} fits no approved niche ({reason}); it won't be emailed")
        elif moved:
            print(f"  niche check: {name} moved from {lead['industry']} to {niche} ({reason})")
    return later
