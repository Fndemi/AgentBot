"""Company names: the name a lead is greeted by.

A name from an official list (CBK, SASRA or any other directory importer in targeting.py)
is never replaced, nor is one from a list you gave (csv, manual). For a lead found on the
web, the name is picked from what its website calls itself:

  og:site_name   the og:site_name meta tag
  logo           the logo's alt or title text
  copyright      the footer's copyright line ("© 2025 Gara Consultancy Ltd")
  footer         other short footer headings or bold text
  schema.org     the name of an Organization (or kind of one) in the page's JSON-LD
  domain         the brand in the address, split into words from WORDS
                 ("garaconsultancy.co.ke" -> "Gara Consultancy")

The page <title> and headings are never used. A candidate is dropped if it reads like a
search phrase or SEO title (SEARCH_PHRASE: "in Nairobi", "Kenya", "best", "top", "services",
"near me", a "|" or " - "...), has more than MAX_WORDS words, reads like a slogan, or (except
the domain's) shares no word with the address. The one most like the domain brand is
picked; when the site's candidates disagree, the AI (GROQ_MODEL) chooses one of them from
the page text and can't add one. With no candidate from the site, the lead's current name is
kept if it passes the same checks, else the domain brand if it is clear; if not, there is
no name: the greeting is "Hello," and subjects leave the name out.

Names are checked at discovery, by the bot's prepare step in small batches (prepare), and
again just before every email, first or follow-up (ensure). A follow-up keeps its thread's
subject; only its text uses the corrected name.

Every lead keeps its full name (company), a short display name without legal endings
(display_name: "Zidi Capital Ltd" -> "Zidi Capital", used in greetings and subjects), where
the name came from (name_source) and the rules it was checked under (name_version).

  python main.py names                      before/after: waiting leads, the 30 emailed last
  python main.py names --recent 50          ... the 50 emailed last
  python main.py names --sent 2026-11-02    ... and that day's emails
  python main.py names --apply              check and save one batch (the bot does this itself)
"""
import difflib
import json
import re
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from urllib.parse import urlparse

import classifier
import db
import ranking
import scraper

MAX_WORDS = 5          # in the short form
SITE_WORKERS = 8
BATCH = 20             # web leads whose names prepare() checks at a time (well under 2 minutes)
# Raised when the rules change, so every lead's name is checked again (prepare, ensure)
NAME_VERSION = 2
MAX_TEXT = 3000        # page text the AI reads
NONE = "none"
Name = namedtuple("Name", "full display source")

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
# More words a domain is split into ("garaconsultancy" -> "gara consultancy"), with BRAND_WORDS
WORDS = set(BRAND_WORDS) | {
    "consultancy", "consultants", "consultant", "consult", "advisory", "partners", "associates",
    "marketing", "ecommerce", "commerce", "design", "designs", "creative", "creatives", "web", "net",
    "network", "networks", "link", "connect", "smart", "green", "life", "plus", "prime", "royal",
    "united", "star", "first", "home", "house", "estate", "estates", "properties", "property",
    "builders", "construction", "engineering", "motors", "auto", "autos", "trading", "traders",
    "supplies", "suppliers", "distributors", "enterprises", "industries", "company", "services",
    "service", "events", "foundation", "trust", "centre", "center", "clinics", "dental", "hospitals",
    "pharmaceuticals", "diagnostics", "wellness", "fitness", "beauty", "salon", "spa", "hotel",
    "hotels", "resort", "safaris", "safari", "logistic", "courier", "cargo", "freight", "transport",
    "energies", "electric", "electrical", "water", "agri", "agro", "farmers", "dairy", "foods",
    "credit", "loan", "wallet", "pesa", "cash", "bima", "biashara", "jamii", "kazi", "soko", "duka",
    "nairobi", "mombasa", "kisumu", "nakuru", "eldoret", "thika", "nyeri", "machakos", "east",
    "academy", "institute", "training", "schools", "kids", "junior", "high", "international",
    "church", "chapel", "ministry", "fellowship", "cathedral", "parish", "mission", "gospel",
    "systems", "labs", "lab", "studio", "solution", "tech", "technologies", "innovations", "innovation",
    "ventures", "capital", "investment", "investments", "holdings", "group", "africa", "kenya"}
SHORT_WORDS = {"pay", "app", "hub", "net", "web", "sms", "lab", "ict", "hr", "it"}  # under 3 letters or kept lower
# Two-part endings; any other domain loses only its last part ("powerup.works" -> "powerup")
SUFFIXES = (".co.ke", ".or.ke", ".ac.ke", ".go.ke", ".ne.ke", ".sc.ke", ".me.ke", ".info.ke",
            ".co.uk", ".co.za", ".co.tz", ".co.ug")
# A name with any of these reads like a slogan or a description, not an organisation's name
SLOGAN = re.compile(
    r"[!?:|•–—]|\s-\s|\.\s|\b(?:we|our|your|you|us|best|leading|top|no\.?\s?1|number one|welcome|home|homepage|"
    r"official|website|page|get|make|empower\w*|transform\w*|trusted|affordable|quality|innovative|"
    r"reliable|simple|smart|easy|fast|better|grow|powering|helping|built|designed|solutions? for|"
    r"services? in|company in|provider (?:of|in)|in kenya|in nairobi|for (?:you|your|business|businesses)|"
    r"management systems?)\b", re.I)
# A name that reads like a search phrase or an SEO page title ("Ecommerce Agency in Nairobi
# Kenya"), not an organisation's name. The domain-built name may say Kenya or Nairobi.
SEARCH_PHRASE = re.compile(r"\bin nairobi\b|\bin kenya\b|\bagency in\b|\bcompany in\b|\bnear me\b|"
                           r"\bbest\b|\btop\b|\bservices\b|\||\s[-–—]\s")
PLACE_WORDS = re.compile(r"\b(?:kenya|nairobi)\b", re.I)
# Words that don't make a name this site's: "Sacco Societies Regulatory Authority" in a
# SACCO's footer shares only "sacco" with its address
STOP_WORDS = {"the", "and", "of", "ltd", "limited", "company", "co", "plc", "inc"} | set(BRAND_WORDS)
# Legal endings left out of the short name; "Kenya" and "(K)" go with them ("Acme Kenya Limited")
LEGAL_END = re.compile(r"[\s,]+(?:ltd\.?|limited|plc|llc|inc\.?|society)$", re.I)
COUNTRY_END = re.compile(r"[\s,]+(?:kenya|ke|\(k\)|\(kenya\)|k|co\.?|company|&)$", re.I)
# "Inventure Mobile Limited (Trading as Tala)": the short name is the trading name
TRADING_AS = re.compile(r"\(?\s*\b(?:trading\s+(?:as|under)|t/a)\s+([^()]+?)\)?\s*$", re.I)
# schema.org types that are an organisation
ORG_TYPE = re.compile(r"Organization$|^(?:Corporation|LocalBusiness|FinancialService|BankOrCreditUnion|"
                      r"School|CollegeOrUniversity|EducationalOrganization|Church|PlaceOfWorship|"
                      r"MedicalBusiness|MedicalClinic|Hospital|Pharmacy|InsuranceAgency|NGO|Store|"
                      r"ProfessionalService|SoftwareApplication)$")


# --- The domain brand ---
def domain_label(domain):
    domain = (domain or "").lower().removeprefix("www.")
    suffix = next((x for x in SUFFIXES if domain.endswith(x) and len(domain) > len(x)), None)
    domain = domain[:-len(suffix)] if suffix else domain.rsplit(".", 1)[0]
    return domain.rsplit(".", 1)[-1]


def segment(part):
    """A run-together label split into words from WORDS: as few letters left over as
    possible, then as few pieces. A left-over piece has at least 3 letters (or is the whole
    label), so a brand is never chopped into stray letters ("sendy" stays "sendy")."""
    n = len(part)
    best = [None] * (n + 1)   # best[i]: (letters not in WORDS, pieces, words) for part[:i]
    best[0] = (0, 0, [])
    for i in range(1, n + 1):
        for j in range(i):
            if best[j] is None:
                continue
            piece = part[j:i]
            if piece in WORDS and (len(piece) >= 3 or piece in SHORT_WORDS):
                unknown = 0
            elif len(piece) >= 3 or (j == 0 and i == n):
                unknown = len(piece)
            else:
                continue
            cand = (best[j][0] + unknown, best[j][1] + 1, best[j][2] + [piece])
            if best[i] is None or cand[:2] < best[i][:2]:
                best[i] = cand
    return best[n][2] if best[n] else [part]


def _brand_words(domain):
    words = []
    for part in re.split(r"[-_]+", domain_label(domain)):
        words += segment(part) if part else []
    return [w for w in words if w]


def _word(w):
    return w.upper() if (len(w) <= 3 and w not in SHORT_WORDS - {"sms", "ict", "hr", "it"}) \
        or not w.isalpha() and len(w) <= 4 else w.capitalize()


def domain_brand(domain):
    """The brand in a domain as a name, in title case: "cragroup.co.ke" -> "CRA Group",
    "garaconsultancy.co.ke" -> "Gara Consultancy", "pay-hero.co.ke" -> "Pay Hero"."""
    return " ".join(_word(w) for w in _brand_words(domain))[:120] or domain


def brand_is_clear(domain):
    """Whether the domain brand reads as a name: letters, no run-together words longer than
    12 letters ("algadhirclinicnakuru"), at most two digits, at most MAX_WORDS words."""
    words = _brand_words(domain)
    label = domain_label(domain)
    return (bool(words) and len(re.sub(r"[^a-z]", "", label)) >= 2 and sum(c.isdigit() for c in label) <= 2
            and all(len(w) <= 12 for w in words) and len(words) <= MAX_WORDS)


# --- Cleaning, the short name and checks ---
def clean(name):
    """A candidate without copyright marks, years, "logo" and "All rights reserved"."""
    name = " ".join((name or "").replace(" ", " ").split())
    name = re.sub(r"(?i)^(?:copyright\s*)?(?:©|\(c\))?\s*(?:\d{4}\s*(?:[-–]\s*\d{4})?)?\s*", "", name)
    name = re.sub(r"(?i)\s*[-–|]?\s*\blogo\b\s*$|^\s*logo\b\s*(?:of\s+)?", "", name)
    name = re.sub(r"(?i)[.,\s]*all rights reserved.*$", "", name).strip(" .,-–|")
    name = re.sub(r"[\s,]+(?:19|20)\d{2}$", "", name)          # "C3 Nairobi 2026"
    if len(name.split()) > 1 and name.isupper():   # "SOVEREIGN GRACE CHURCH" -> "Sovereign Grace Church"
        name = " ".join(w if len(w) <= 3 else w.capitalize() for w in name.split())
    return name


def display(name):
    """The short name: no legal endings. "Noble Sacco Society Ltd" -> "Noble Sacco",
    "Acme Kenya Limited" -> "Acme", "Avenews Ke Ltd" -> "Avenews"."""
    full = clean(name)
    if (m := TRADING_AS.search(full)) and display(m.group(1)):
        return display(m.group(1))
    short = full
    while LEGAL_END.search(short):
        short = LEGAL_END.sub("", short).strip(" .,")
        while COUNTRY_END.search(short):
            short = COUNTRY_END.sub("", short).strip(" .,")
    return short or full or None


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def matches_domain(name, domain):
    """Whether a name shares a word with its address: it spells the address, or one of its
    own words (not a generic one like "sacco") is in it."""
    label = _squash(domain_label(domain))
    whole = _squash(name)
    if len(label) >= 4 and (whole == label or label in whole):   # "Fintech Group", "Payroll.ke"
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in STOP_WORDS]
    return any(len(w) >= 3 and w in label for w in words)


def problem(name, domain, source):
    """Why a candidate can't be a name, or None."""
    full, short = clean(name), display(name)
    if not short or not re.search(r"[A-Za-z]", short):
        return "empty"
    if len(short.split()) > MAX_WORDS:
        return f"more than {MAX_WORDS} words"
    if SEARCH_PHRASE.search(full.lower()) or (source != "domain" and PLACE_WORDS.search(short)):
        return "reads like a search phrase or page title"
    if SLOGAN.search(short):
        return "reads like a slogan or description"
    if short == short.lower():
        return "written like an address"    # "maseno-uni", "citam"
    if source != "domain" and not matches_domain(short, domain):
        return "doesn't match the address"
    return None


def similarity(name, domain):
    return difflib.SequenceMatcher(None, _squash(display(name)), _squash(domain_label(domain))).ratio()


# --- Candidates from a page ---
def _jsonld_names(soup):
    def walk(node):
        if isinstance(node, list):
            for n in node:
                yield from walk(n)
        elif isinstance(node, dict):
            kinds = node.get("@type") or []
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if any(isinstance(k, str) and ORG_TYPE.search(k) for k in kinds) and isinstance(node.get("name"), str):
                yield node["name"]
            for key in ("@graph", "publisher", "author", "provider", "brand", "parentOrganization"):
                if key in node:
                    yield from walk(node[key])
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.string or script.get_text() or ""))
        except ValueError:
            continue


def _logo_texts(soup):
    for img in soup.select("img[alt], img[title]"):
        where = " ".join([img.get("src", ""), " ".join(img.get("class", [])), img.get("id", "")]
                         + [" ".join(p.get("class", [])) + " " + (p.get("id") or "") for p in img.parents if p.name])
        if "logo" in where.lower():
            yield from (t for t in (img.get("alt"), img.get("title")) if t)


def _copyright_names(soup):
    for el in soup.select("footer") or [soup]:
        for m in re.finditer(r"(?:©|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*([^©\n|]{2,80})",
                             el.get_text("\n"), re.I):
            yield m.group(1)


def _footer_texts(soup):
    """Short headings and bold text in the footer, where a site often repeats its name."""
    for el in soup.select("footer"):
        for tag in el.select("h1, h2, h3, h4, h5, strong, b, .footer-title, .widget-title"):
            text = tag.get_text(" ", strip=True)
            if text and len(text.split()) <= MAX_WORDS:
                yield text


def spelled_on_page(domain, text):
    """The domain's label as the page writes it, if it does in mixed case: "seamlessit.co.ke"
    -> "SeamlessIT", "paywave-express.co.ke" -> "Paywave Express". Else None."""
    label = _squash(domain_label(domain))
    tokens = re.findall(r"[A-Za-z0-9]+", text or "")
    for i in range(len(tokens)):
        for n in range(1, 5):
            words = tokens[i:i + n]
            if len(words) < n or len("".join(words)) > len(label):
                break
            spelled = " ".join(words)
            if _squash(spelled) == label and spelled != spelled.lower() and spelled != spelled.upper():
                return spelled
    return None


def candidates(soup, domain):
    """[Name] the page and address give, each usable (see problem), in this order:
    og:site_name, the logo's alt or title text, the copyright line, other footer text, the
    schema.org Organization name; the domain brand last, when it is clear and the site
    doesn't already give it (spelled as on the page when the page writes it). The page
    <title> and headings are never used: they are often an SEO phrase."""
    found = []
    if soup is not None:
        raw = ([("og:site_name", m.get("content", "")) for m in soup.select('meta[property="og:site_name"]')]
               + [("logo", n) for n in _logo_texts(soup)]
               + [("copyright", n) for n in _copyright_names(soup)]
               + [("footer", n) for n in _footer_texts(soup)]
               + [("schema.org", n) for n in _jsonld_names(soup)])
        for source, text in raw:
            full = clean(text)
            if not problem(full, domain, source) and display(full) not in [c.display for c in found]:
                found.append(Name(full, display(full), source))
    if brand_is_clear(domain):
        brand = (soup is not None and spelled_on_page(domain, soup.get_text(" "))) or domain_brand(domain)
        if not problem(brand, domain, "domain") and _squash(brand) not in [_squash(c.display) for c in found]:
            found.append(Name(brand, display(brand), "domain"))
    return found


def agree(names):
    """Whether these names are the same one written more or less fully ("Zidi", "Zidi Capital")."""
    keys = [_squash(n.display) for n in names]
    return all(a in b or b in a for a in keys for b in keys)


CHOOSE_PROMPT = """These are the names a Kenyan organisation's website ({website}) gives for
itself. Using the page text, choose the one that is the organisation's own name. You must
choose one of the numbered names exactly as given; never write a different name.

Names:
{names}

Page text:
\"\"\"{text}\"\"\"

Answer in JSON: {{"choice": <the number of the name>}}"""


def ask_ai(names, domain, text):
    """The name the AI (GROQ_MODEL) chooses from `names`, or None if it can't (it fails, or
    answers with anything but one of their numbers)."""
    listed = "\n".join(f"{i}. {n.full}" for i, n in enumerate(names, 1))
    try:
        raw = classifier.ask_with_retry(CHOOSE_PROMPT.format(website=domain, names=listed, text=(text or "")[:MAX_TEXT]))
        choice = json.loads(raw).get("choice")
        return names[int(choice) - 1] if str(choice).strip().isdigit() and 1 <= int(choice) <= len(names) else None
    except Exception:
        return None


def pick(soup, domain, text=None, use_ai=True):
    """The Name for a website: of the names the site gives itself, the one most like the
    domain brand; when they disagree, the AI chooses one of them. The domain brand only when
    the site gives none, and Name(None, None, "none") when it isn't clear either."""
    found = candidates(soup, domain)
    site = [n for n in found if n.source != "domain"]
    if not site:
        return found[0] if found else Name(None, None, NONE)
    if use_ai and len(site) > 1 and not agree(site):
        text = text if text is not None else (" ".join(soup.get_text(" ").split()) if soup is not None else "")
        chosen = ask_ai(site, domain, text)
        if chosen:
            return chosen._replace(source=f"{chosen.source}, chosen by AI")
    return max(site, key=lambda n: similarity(n.full, domain))   # max keeps the first of equals


# --- Leads ---
def official(lead):
    """Whether the lead's name came from an official list (an importer) or a list you gave
    (csv, manual): anything but a web search. Such a name is never replaced."""
    lead = dict(lead)
    return bool(lead.get("imported_at")) or lead.get("source") != "search"


def short_name(lead):
    """The name a greeting or subject uses, or None for "Hello," and no name."""
    lead = dict(lead)
    if lead.get("name_source") == NONE:
        return None
    return lead.get("display_name") or display(lead.get("company")) or None


# Variant D (rotation.PLAIN_VARIANTS) names the company four times, so only a name beyond
# doubt gets it: checked under today's rules, not built from the address, not picked by the AI
# from names that disagreed, no search phrase, page title or slogan, at most CLEAR_MAX_WORDS
CLEAR_MAX_WORDS = 4


def doubt(lead):
    """Why the lead's name isn't beyond doubt (so it gets A, B or C, never D), or None."""
    lead = dict(lead)
    if lead.get("name_version") != NAME_VERSION:
        return "name not checked under today's rules"
    source = lead.get("name_source") or ""
    short = short_name(lead)
    if source == NONE or not short:
        return "no name"
    if source.startswith("domain"):
        return "name built from the address"
    if "chosen by AI" in source:
        return "the site's names disagreed (picked by the AI)"
    if len(short.split()) > CLEAR_MAX_WORDS:
        return f"name has more than {CLEAR_MAX_WORDS} words"
    full = clean(lead.get("company") or short) or short
    if SEARCH_PHRASE.search(full.lower()) or SEARCH_PHRASE.search(short.lower()):
        return "name reads like a search phrase or page title"
    if SLOGAN.search(short):
        return "name reads like a slogan or description"
    if short == short.lower():
        return "name written like an address"
    if not official(lead):
        why = problem(short, domain_of(lead.get("website")), source)
        if why:
            return f"name {why}"
    return None


def home_of(website):
    return website if website.startswith("http") else f"https://{website}"


def domain_of(website):
    return urlparse(home_of(website)).netloc.lower().split(":")[0].removeprefix("www.") if website else ""


def name_for(lead, soup):
    """The Name this lead should have: a list's name as it is; for a web lead, the name its
    site gives; if none, its current name when that passes every check; else the domain brand."""
    lead = dict(lead)
    if official(lead):
        return Name(lead["company"], display(lead["company"]), lead.get("source") or "list")
    domain = domain_of(lead.get("website"))
    name = pick(soup, domain)
    old = clean(lead.get("company"))
    if name.source in ("domain", NONE) and old and not problem(old, domain, "current"):
        return Name(old, display(old), "current name, checked")   # "SeamlessIT", "Paywave Express"
    return name


def still_good(lead):
    """Whether a lead's saved name passes today's rules without reading its site again."""
    lead = dict(lead)
    if lead.get("name_version") != NAME_VERSION:
        return False
    if official(lead) or lead.get("name_source") == NONE:
        return True
    name = lead.get("display_name") or lead.get("company")
    return not problem(
        name, domain_of(lead.get("website")), lead.get("name_source") or "current")


def fetch_home(lead):
    lead = dict(lead)
    return scraper.fetch(home_of(lead["website"])) if lead.get("website") and not official(lead) else None


def save(lead, name):
    """Save a Name on a lead (an official list's full name is never replaced); returns the
    lead as a dict with its name fields as saved."""
    lead = dict(lead)
    db.save_name(lead["id"], name.full, name.display, name.source, keep_company=official(lead),
                 version=NAME_VERSION)
    if name.full and not official(lead):
        lead["company"] = name.full
    lead.update(display_name=name.display, name_source=name.source, name_version=NAME_VERSION)
    return lead


def ensure(lead):
    """Check a lead's name just before an email to it (first email or follow-up): kept if it
    passes today's rules, otherwise worked out again from its site. Returns the lead, as a
    dict, with its name fields as they are now. A follow-up's subject stays the thread's."""
    if still_good(lead):
        return dict(lead)
    return save(lead, name_for(lead, fetch_home(lead)))


# Statuses whose leads may still get an email, or have had one: their names are checked
PREPARE_STATUSES = ("new", "verified", "sent", "replied", "meeting", "client")


def needing_check():
    with db.conn() as c:
        return c.execute(f"SELECT * FROM leads WHERE status IN ({','.join('?' * len(PREPARE_STATUSES))})"
                         " AND (name_version IS NULL OR name_version < ?) ORDER BY id",
                         (*PREPARE_STATUSES, NAME_VERSION)).fetchall()


def prepare(batch=BATCH):
    """Check and save the names of leads not checked under today's rules: every list lead at
    once (no site is read), then at most `batch` web leads, their sites read in parallel.
    Returns (checked, still to check). Run again until nothing is left; the bot does this in
    its prepare step and between inbox checks."""
    leads = needing_check()
    lists = [l for l in leads if official(l)]
    web = [l for l in leads if not official(l)][:batch]
    for lead in lists:
        save(lead, name_for(lead, None))
    for lead, name in review(web):
        save(lead, name)
    return len(lists) + len(web), len(leads) - len(lists) - len(web)


def review(leads):
    """[(lead, Name)] for each lead, reading the websites of those found on the web."""
    leads = [dict(l) for l in leads]
    with ThreadPoolExecutor(SITE_WORKERS) as pool:
        soups = list(pool.map(fetch_home, leads))
    return [(lead, name_for(lead, soup)) for lead, soup in zip(leads, soups)]


def sent_on(day):
    """The leads emailed on that day (YYYY-MM-DD)."""
    with db.conn() as c:
        return c.execute("SELECT l.* FROM leads l WHERE l.id IN (SELECT lead_id FROM sent WHERE sent_on=?)"
                         " ORDER BY l.id", (day,)).fetchall()


def recently_emailed(n=30):
    """The n leads emailed most recently, newest first."""
    with db.conn() as c:
        return c.execute("SELECT l.* FROM leads l JOIN (SELECT lead_id, MAX(id) AS last FROM sent GROUP BY lead_id) s"
                         " ON s.lead_id = l.id ORDER BY s.last DESC LIMIT ?", (n,)).fetchall()


def greeted_as(lead):
    """The name the last email to this lead greeted it by ("Hello <name> team,"), from the
    text that went out; the name emails use now if none was recorded."""
    with db.conn() as c:
        sent = c.execute("SELECT sent_body FROM sent WHERE lead_id=? AND sent_body IS NOT NULL"
                         " ORDER BY id DESC LIMIT 1", (dict(lead)["id"],)).fetchone()
    m = re.match(r"\s*Hello (.+?) team,", sent["sent_body"]) if sent else None
    if m:
        return m.group(1)
    if sent and re.match(r"\s*Hello,", sent["sent_body"]):
        return "(no name: Hello,)"
    return short_name(lead) or "(no name: Hello,)"


def _cut(text, n):
    text = text or "-"
    return text if len(text) <= n else text[:n - 1] + "…"


def table(rows, title):
    """Before (the name the last email used, or would use) and after, for each (lead, Name)."""
    print(f"\n{title}: {len(rows)}")
    if not rows:
        return
    print(f"  {'#':>5}  {'BEFORE (greeted as)':36}  {'AFTER: FULL NAME':36}  {'SHORT NAME':26}  SOURCE")
    for lead, name in rows:
        before = greeted_as(lead)
        after = name.display or "(no name: Hello,)"
        print(f" {'*' if after != before else ' '}{lead['id']:>5}  {_cut(before, 36):36}  "
              f"{_cut(name.full or '(no name: Hello,)', 36):36}  {_cut(after, 26):26}  {name.source}")


def run(apply=False, day=None, recent=30, batch=BATCH):
    """Without apply: before/after for the waiting leads and the `recent` leads emailed last
    (and those emailed on `day`), changing nothing. With apply: one batch of prepare()."""
    if apply:
        done, left = prepare(batch)
        print(f"Checked the names of {done} leads; {left} still to check"
              + (" (run again, or leave it to the bot)." if left else "."))
        return done, left
    groups = [(ranking.waiting(), "Waiting leads"), (recently_emailed(recent), f"The {recent} leads emailed last")]
    if day:
        groups.append((sent_on(day), f"Emailed on {day}"))
    print(f"Reading the websites of {sum(len(g) for g, _ in groups)} leads...")
    out = []
    for leads, title in groups:
        rows = review(leads)
        table(rows, f"{title} (* = the name in emails changes)")
        out.append(rows)
    print("\nNothing was changed. The bot saves these names itself (prepare step), or run with --apply.")
    return out
