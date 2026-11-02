"""Lead quality: the sites that are never emailed. (Company names: names.py.)

Excluded: directories, marketplaces, events (summits, conferences, forums), associations,
chambers, communities, accelerators and investors, media and news sites, and government
(.go.ke) bodies. Discovery doesn't add them, but visits the companies they list. Existing
leads like these get the status 'excluded' and no more emails (excluded_reason says why).

  python main.py clean-leads            show the leads that would be excluded
  python main.py clean-leads --apply    exclude them
"""
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import db
import scraper

SITE_WORKERS = 8  # websites read at the same time

# --- What a site is, from its address, title and description ---
EXCLUDED = (
    ("government body", None),  # .go.ke, checked on the address
    ("directory or marketplace", re.compile(
        r"\b(?:directory|directories|business listings?|yellow pages|classifieds|marketplace|"
        r"find (?:a |the best )?(?:businesses|companies|suppliers|services)|list of (?:companies|businesses|startups))\b", re.I)),
    ("event", re.compile(
        r"\b(?:summit|conference|expo|exhibition|hackathon|festival|forums?|awards|trade fair|"
        r"tech week|startup week)\b", re.I)),
    ("association or chamber", re.compile(
        r"\b(?:association|chambers? of commerce|chamber|federation|alliance of|council of|union of)\b", re.I)),
    # not "a faith community" (a church) or "join our community of merchants" (a company)
    ("community", re.compile(
        r"\b(?:tech|developer|developers|startup|online)\s+community\b|\bcommunity\s+(?:platform|hub)\b", re.I)),
    ("accelerator or investor", re.compile(
        r"\b(?:accelerator|incubator|innovation hub|venture capital|venture partners|impact invest\w*|"
        r"angel network|startup fund|seed fund|portfolio companies)\b", re.I)),
    # not "latest news and insights" (a company's blog)
    ("media or news", re.compile(
        r"\b(?:latest|breaking|local|daily)\s+(?:local\s+)?news\s+(?:from|in|on|across)\b|"
        r"\bnews\s+(?:site|website|platform|portal|outlet|agency)\b|\bmagazine\b|\bnewspaper\b|"
        r"\bmedia house\b|\bpodcast\b|\b(?:tv|radio)\s+station\b", re.I)),
)


def excluded_reason(domain, text):
    """Why a site is never emailed, or None. `text` is its title and description (not its
    headings, where "News" or "Our community" mean nothing), or a lead's company name."""
    domain = (domain or "").lower()
    if domain.endswith(".go.ke") or ".go.ke/" in domain or domain.endswith(".gov"):
        return "government body"
    for why, pattern in EXCLUDED[1:]:
        if text and pattern.search(text):
            return why
    return None


def page_intro(soup):
    """A page's title and descriptions, which say what the site is."""
    if soup is None:
        return ""
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return " | ".join([title] + [m.get("content", "") for m in soup.select(
        'meta[name="description"], meta[property="og:description"]')])


# --- Existing leads ---
STILL_EMAILED = ("new", "verified", "sent")   # statuses that may still get a first email or a follow-up


def home_of(website):
    return website if website.startswith("http") else f"https://{website}"


def domain_of(website):
    return urlparse(home_of(website)).netloc.lower().split(":")[0].removeprefix("www.") if website else ""


def review(leads):
    """[(lead, excluded_reason)] for the leads that are never to be emailed. Each is checked by
    its address and its name, and by its site's title and description unless it was imported
    from a regulator's directory (it is what the directory says)."""
    leads = [dict(l) for l in leads]
    with ThreadPoolExecutor(SITE_WORKERS) as pool:
        soups = list(pool.map(lambda l: scraper.fetch(home_of(l["website"]))
                              if l["website"] and not l["imported_at"] else None, leads))
    out = []
    for lead, soup in zip(leads, soups):
        text = lead["company"] if lead["imported_at"] else " | ".join(
            t for t in (page_intro(soup), lead["company"]) if t)
        why = excluded_reason(domain_of(lead["website"]), text)
        if why:
            out.append((lead, why))
    return out


def run(apply=False):
    with db.conn() as c:
        leads = c.execute(f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(STILL_EMAILED))})"
                          " AND crm_emailed_at IS NULL ORDER BY id", STILL_EMAILED).fetchall()
    print(f"Reading the websites of {len(leads)} leads still being emailed...")
    excluded = review(leads)
    print(f"\nWould exclude {len(excluded)}:" if not apply else f"\nExcluded {len(excluded)}:")
    for l, why in excluded:
        print(f"  #{l['id']:<5} {l['status']:8} {(l['industry'] or ''):16} {l['company']}  ({l['website'] or l['email']}): {why}")
    if apply:
        for l, why in excluded:
            db.exclude(l["id"], why, "clean-leads")
    else:
        print("\nNothing was changed. Run with --apply to make these changes.")
    return excluded
