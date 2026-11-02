"""Company names: the name a lead is greeted by.

A name from an official list (CBK, SASRA or any other directory importer in targeting.py)
is never replaced, nor is one from a list you gave (csv, manual). For a lead found on the
web, the name is picked from what its website calls itself:

  schema.org     the name of an Organization (or kind of one) in the page's JSON-LD
  og:site_name   the og:site_name meta tag
  footer         the footer's copyright line ("© 2026 Zidi Capital Ltd")
  logo           the logo's alt or title text
  title          the page <title>, only if it has at most 4 words and matches the address
  headline       the first <h1>, the same way
  domain         the brand in the address ("cragroup.co.ke" -> "CRA Group")

A candidate is dropped if its short form has more than MAX_WORDS words, reads like a slogan
or a description, or (except the domain's) doesn't match the address. The one most like the
domain brand is picked. When the site's candidates disagree (neither contains the other),
the AI (GROQ_MODEL) chooses one of them from the page text; it can't add one. With no
candidate from the site, the domain brand is used if it is clear; if not, there is no name:
the greeting is "Hello," and subjects leave the name out.

Every lead keeps its full name (company), a short display name without legal endings
(display_name: "Zidi Capital Ltd" -> "Zidi Capital", used in greetings and subjects) and
where the name came from (name_source).

  python main.py names                         before/after for waiting leads and today's sends
  python main.py names --sent 2026-11-02       ... and that day's sends
  python main.py names --apply                 save the names of waiting leads (sent ones never)
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
MAX_PAGE_WORDS = 4     # a title or headline is a candidate only this short
SITE_WORKERS = 8
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


def _brand_words(domain):
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
    return [w for w in words if w]


def _word(w):
    return w.upper() if len(w) <= 3 and w not in ("pay", "app", "hub") or not w.isalpha() and len(w) <= 4 \
        else w.capitalize()


def domain_brand(domain):
    """The brand in a domain as a name, in title case: "cragroup.co.ke" -> "CRA Group",
    "noblesaccosociety.co.ke" -> "Noble Sacco Society", "pay-hero.co.ke" -> "Pay Hero"."""
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
    """Whether a name belongs to this address: it spells the address, one of its own words
    (not a generic one like "sacco") is in it, or its initials start it."""
    label = _squash(domain_label(domain))
    whole = _squash(name)
    if len(label) >= 4 and (whole == label or label in whole):   # "Fintech Group", "Payroll.ke"
        return True
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in STOP_WORDS]
    if any(len(w) >= 3 and w in label for w in words):
        return True
    initials = "".join(w[0] for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in ("the", "of", "and"))
    return len(initials) >= 3 and label.startswith(initials)


def problem(name, domain, source):
    """Why a candidate can't be a name, or None."""
    short = display(name)
    if not short or not re.search(r"[A-Za-z]", short):
        return "empty"
    if len(short.split()) > MAX_WORDS:
        return f"more than {MAX_WORDS} words"
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


def _footer_names(soup):
    for el in soup.select("footer") or [soup]:
        for m in re.finditer(r"(?:©|\(c\)|copyright)\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?)?\s*([^©\n|]{2,80})",
                             el.get_text("\n"), re.I):
            yield m.group(1)


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
    """[Name] the page and address give, each usable (see problem), best source first; the
    domain brand last, when it is clear and the site doesn't already give it. The domain
    brand is spelled as on the page when the page writes it."""
    found = []
    if soup is not None:
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        h1 = soup.find("h1")
        raw = ([("schema.org", n) for n in _jsonld_names(soup)]
               + [("og:site_name", m.get("content", "")) for m in soup.select('meta[property="og:site_name"]')]
               + [("footer", n) for n in _footer_names(soup)]
               + [("logo", n) for n in _logo_texts(soup)]
               + [(src, t) for src, t in (("title", title), ("headline", h1.get_text(" ", strip=True) if h1 else ""))
                  if t and len(t.split()) <= MAX_PAGE_WORDS])
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


def home_of(website):
    return website if website.startswith("http") else f"https://{website}"


def domain_of(website):
    return urlparse(home_of(website)).netloc.lower().split(":")[0].removeprefix("www.") if website else ""


def name_for(lead, soup):
    """The Name this lead should have."""
    lead = dict(lead)
    if official(lead):
        return Name(lead["company"], display(lead["company"]), lead.get("source") or "list")
    name = pick(soup, domain_of(lead.get("website")))
    old = clean(lead.get("company"))
    if name.source == "domain" and old and _squash(old) == _squash(name.full):
        return Name(old, display(old), "domain")      # its spelling: "SeamlessIT", not "Seamlessit"
    return name


def review(leads):
    """[(lead, Name)] for each lead, reading the websites of those found on the web."""
    leads = [dict(l) for l in leads]
    def soup_of(l):
        return scraper.fetch(home_of(l["website"])) if l.get("website") and not official(l) else None
    with ThreadPoolExecutor(SITE_WORKERS) as pool:
        soups = list(pool.map(soup_of, leads))
    return [(lead, name_for(lead, soup)) for lead, soup in zip(leads, soups)]


def sent_on(day):
    """The leads emailed on that day (YYYY-MM-DD)."""
    with db.conn() as c:
        return c.execute("SELECT l.* FROM leads l WHERE l.id IN (SELECT lead_id FROM sent WHERE sent_on=?)"
                         " ORDER BY l.id", (day,)).fetchall()


def _cut(text, n):
    text = text or "-"
    return text if len(text) <= n else text[:n - 1] + "…"


def table(rows, title):
    print(f"\n{title}: {len(rows)}")
    if not rows:
        return
    print(f"  {'#':>5}  {'BEFORE (greeted as)':36}  {'AFTER: FULL NAME':36}  {'SHORT NAME':26}  SOURCE")
    for lead, name in rows:
        before = lead["company"]
        changed = "*" if name.display != before else " "
        print(f" {changed}{lead['id']:>5}  {_cut(before, 36):36}  {_cut(name.full or '(no name: Hello,)', 36):36}  "
              f"{_cut(name.display, 26):26}  {name.source}")


def run(apply=False, day=None):
    day = day or date.today().isoformat()
    waiting = ranking.waiting()
    sent = sent_on(day)
    print(f"Reading the websites of {len(waiting)} waiting leads and {len(sent)} sent on {day}...")
    waiting_rows, sent_rows = review(waiting), review(sent)
    table(waiting_rows, "Waiting leads (* = the greeting changes)")
    table(sent_rows, f"Sent on {day} (report only: never changed)")
    if apply:
        for lead, name in waiting_rows:
            db.save_name(lead["id"], name.full, name.display, name.source, keep_company=official(lead))
        print(f"\nSaved the names of {len(waiting_rows)} waiting leads. Sent leads were not changed.")
    else:
        print("\nNothing was changed. Run with --apply to save the waiting leads' names.")
    return waiting_rows, sent_rows
