"""Lead quality: the company name an email uses, and the sites that are never emailed.

Names: a page title or tagline is never a name. The site's own organisation name is used,
taken from, in order, og:site_name, the logo's alt text and the footer's copyright line. A
name is only taken if it has at most MAX_NAME_WORDS words, doesn't read like a slogan or a
description, and matches the website's address. Otherwise the brand in the domain is used
("cragroup.co.ke" -> "CRA Group").

Excluded: directories, marketplaces, events (summits, conferences, forums), associations,
chambers, communities, accelerators and investors, media and news sites, and government
(.go.ke) bodies. Discovery doesn't add them, but visits the companies they list. Existing
leads like these get the status 'excluded' and no more emails (excluded_reason says why).

  python main.py clean-leads            show the leads that would be renamed or excluded
  python main.py clean-leads --apply    rename and exclude them
"""
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import db
import scraper

MAX_NAME_WORDS = 5
SITE_WORKERS = 8  # websites read at the same time
LEGAL = re.compile(r"[\s,]+(?:ltd\.?|limited|plc|inc\.?|llc|llp|co\.?)$", re.I)
# Words a domain label is split on from the end, so "noblesaccosociety" -> "Noble Sacco Society".
# Longer words first, so "technologies" is tried before "tech".
BRAND_WORDS = sorted((
    "group", "sacco", "society", "africa", "kenya", "technologies", "technology", "tech", "solutions",
    "systems", "capital", "credit", "finance", "financial", "microfinance", "bank", "ventures", "labs",
    "digital", "school", "schools", "academy", "college", "church", "ministries", "pay", "payments",
    "energy", "solar", "power", "software", "agency", "health", "care", "medical", "clinic", "pharma",
    "loans", "lending", "cash", "money", "mobile", "online", "logistics", "express", "shop", "store",
    "media", "studio", "studios", "consulting", "global", "international", "holdings", "investments",
    "cooperative", "insurance", "realty", "homes", "foods", "farm", "farms", "hub", "works", "app",
    "apps", "cloud", "data", "learning", "education", "hr", "payroll",
    "fintech", "edtech", "healthtech", "agritech", "proptech", "insurtech", "hospital", "university",
    "pharmacy", "brokers", "shuttle", "travel", "tours"), key=len, reverse=True)
# Two-part endings; any other domain loses only its last part ("powerup.works" -> "powerup")
SUFFIXES = (".co.ke", ".or.ke", ".ac.ke", ".go.ke", ".ne.ke", ".sc.ke", ".me.ke", ".info.ke",
            ".co.uk", ".co.za", ".co.tz", ".co.ug")
# A name with any of these reads like a slogan or a description, not an organisation's name
SLOGAN = re.compile(
    r"[!?:|•–—]|\s-\s|\.\s|\b(?:we|our|your|you|us|best|leading|top|no\.?\s?1|number one|welcome|home|homepage|"
    r"official|website|page|get|make|empower\w*|transform\w*|trusted|affordable|quality|innovative|"
    r"reliable|simple|smart|easy|fast|better|grow|powering|helping|built|designed|solutions? for|"
    r"services? in|company in|provider (?:of|in)|in kenya|in nairobi|for (?:you|your|business|businesses))\b", re.I)
# Words that don't make a name this site's: "Sacco Societies Regulatory Authority" in a
# SACCO's footer shares only "sacco" with its address
STOP_WORDS = {"the", "and", "of", "ltd", "limited", "company", "co", "plc", "inc"} | set(BRAND_WORDS)

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


# --- Names ---
def domain_label(domain):
    domain = (domain or "").lower().removeprefix("www.")
    suffix = next((x for x in SUFFIXES if domain.endswith(x) and len(domain) > len(x)), None)
    domain = domain[:-len(suffix)] if suffix else domain.rsplit(".", 1)[0]
    return domain.rsplit(".", 1)[-1]


def _word(w):
    return w.upper() if len(w) <= 3 and w not in ("pay", "app", "hub") or not w.isalpha() and len(w) <= 4 \
        else w.capitalize()


def domain_brand(domain):
    """The brand in a domain as a name: "cragroup.co.ke" -> "CRA Group",
    "noblesaccosociety.co.ke" -> "Noble Sacco Society", "pay-hero.co.ke" -> "Pay Hero"."""
    words = []
    for part in re.split(r"[-_]+", domain_label(domain)):
        tail = []
        while part:
            w = next((w for w in BRAND_WORDS if part.endswith(w) and len(part) > len(w)), None)
            if not w:
                break
            tail.insert(0, w)
            part = part[:-len(w)]
        words += ([part] if part else []) + tail
    return " ".join(_word(w) for w in words if w)[:120] or domain


def clean_name(name):
    name = " ".join((name or "").replace(" ", " ").split())
    name = re.sub(r"(?i)^(?:copyright\s*)?(?:©|\(c\))?\s*(?:\d{4}\s*(?:[-–]\s*\d{4})?)?\s*", "", name)
    name = re.sub(r"(?i)\s*[-–|]?\s*\blogo\b\s*$|^\s*logo\b\s*(?:of\s+)?", "", name)
    name = re.sub(r"(?i)[.,\s]*all rights reserved.*$", "", name).strip(" .,-–|")
    while LEGAL.search(name):
        name = LEGAL.sub("", name).strip(" .,")
    if len(name.split()) > 1 and name.isupper():   # "SOVEREIGN GRACE CHURCH" -> "Sovereign Grace Church"
        name = " ".join(w if len(w) <= 3 else w.capitalize() for w in name.split())
    return name


def matches_domain(name, domain):
    """Whether a name belongs to this address: one of its words (or its initials) is in
    the domain's label."""
    label = re.sub(r"[^a-z0-9]", "", domain_label(domain))
    whole = re.sub(r"[^a-z0-9]", "", name.lower())
    if len(label) >= 4 and (whole == label or label in whole):   # "Fintech Group", "Payroll.ke"
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in STOP_WORDS]
    if any(len(w) >= 3 and w in label for w in words):
        return True
    initials = "".join(w[0] for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in ("the", "of", "and"))
    return len(initials) >= 3 and label.startswith(initials)


def reads_like_title(name):
    """Whether a lead's name is a page title or tagline rather than a name."""
    name = clean_name(name)
    return not name or len(name.split()) > MAX_NAME_WORDS or bool(SLOGAN.search(name))


def good_name(name, domain):
    """The name cleaned, if it can be used as the company's name; otherwise None."""
    name = clean_name(name)
    if not name or len(name.split()) > MAX_NAME_WORDS or SLOGAN.search(name):
        return None
    if not re.search(r"[A-Za-z]", name) or name == name.lower() or not matches_domain(name, domain):
        return None   # "maseno-uni", "citam": an address, not how they write their name
    return name


def name_candidates(soup):
    """The names a page gives itself, best first: og:site_name, the logo's alt text, the
    footer's copyright line."""
    found = [m.get("content", "") for m in soup.select('meta[property="og:site_name"]')]
    for img in soup.select("img[alt]"):
        where = " ".join([img.get("src", ""), " ".join(img.get("class", [])), img.get("id", "")]
                         + [" ".join(p.get("class", [])) + " " + (p.get("id") or "") for p in img.parents if p.name])
        if "logo" in where.lower():
            found.append(img["alt"])
    for el in soup.select("footer") or [soup]:
        for m in re.finditer(r"(?:©|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*([^©\n|]{2,80})",
                             el.get_text("\n"), re.I):
            found.append(m.group(1))
    return found


def org_name(soup, domain):
    """The organisation's name for a site: the first good name it gives itself
    (name_candidates), else the brand in its domain."""
    for name in (name_candidates(soup) if soup is not None else []):
        if (good := good_name(name, domain)):
            return good
    return domain_brand(domain)


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
    """[(lead, new_name or None, excluded_reason or None)] for the leads with a change.
    Only names that read like a title or tagline are replaced, and only
    for leads found by web search (other names come from a directory or a list you gave).
    Every lead is checked for exclusion by its address and its name, and by its site's title
    and description unless it was imported from a regulator's directory (it is what the
    directory says)."""
    leads = [dict(l) for l in leads]
    with ThreadPoolExecutor(SITE_WORKERS) as pool:
        soups = list(pool.map(lambda l: scraper.fetch(home_of(l["website"])) if l["website"] else None, leads))
    out = []
    for lead, soup in zip(leads, soups):
        domain = domain_of(lead["website"])
        text = lead["company"] if lead["imported_at"] else " | ".join(
            t for t in (page_intro(soup), lead["company"]) if t)
        why = excluded_reason(domain, text)
        name = None
        if lead["source"] == "search" and lead["website"] and reads_like_title(lead["company"]):
            name = org_name(soup, domain)
            name = None if name == lead["company"] else name
        if name or why:
            out.append((lead, name, why))
    return out


def run(apply=False):
    with db.conn() as c:
        leads = c.execute(f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(STILL_EMAILED))})"
                          " AND crm_emailed_at IS NULL ORDER BY id", STILL_EMAILED).fetchall()
    print(f"Reading the websites of {len(leads)} leads still being emailed...")
    changes = review(leads)
    excluded = [(l, why) for l, _, why in changes if why]
    renamed = [(l, name) for l, name, why in changes if name and not why]
    print(f"\nWould exclude {len(excluded)}:" if not apply else f"\nExcluded {len(excluded)}:")
    for l, why in excluded:
        print(f"  #{l['id']:<5} {l['status']:8} {(l['industry'] or ''):16} {l['company']}  ({l['website'] or l['email']}): {why}")
    print(f"\nWould rename {len(renamed)}:" if not apply else f"\nRenamed {len(renamed)}:")
    for l, name in renamed:
        print(f"  #{l['id']:<5} {l['status']:8} {l['company']!r} -> {name!r}")
    if apply:
        for l, why in excluded:
            db.exclude(l["id"], why, "clean-leads")
        for l, name in renamed:
            db.rename(l["id"], name, "clean-leads")
    else:
        print("\nNothing was changed. Run with --apply to make these changes.")
    return excluded, renamed
