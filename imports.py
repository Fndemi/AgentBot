"""Leads from regulators' published lists (SASRA, CBK, WASREB, IRA, EARB, CMA). Nothing here
sends email.

  python main.py import-list SOURCE FILE_OR_URL --niche NICHE [--dry-run]
      one list: a PDF or a web page with a table, saved or online
  python main.py import-all [--dry-run]
      the latest official version of every list in SOURCES, downloaded from its regulator

From each organisation only its name is taken, and its website if the list gives one; never
the list's email. An organisation already known (a lead with the same company name or domain,
or one waiting here from an earlier import) or on a do-not-contact domain is skipped.

Website: when the list gives none, one Tavily search finds it, while at least
TAVILY_DISCOVERY_RESERVE of this month's TAVILY_MONTHLY_CAP calls stay unused for discovery.
Beyond that the organisation waits as "website pending": the prepare job (prepare.py) looks
up WEBSITES_PER_DAY of them a day, carrying into the next month until all are done.

Address: the website is read (contacts.find) and an address it publishes today is used: a
named person at high confidence, otherwise the best general inbox, only if it is no other
lead's, not opted out, and its mail server answers (contacts.published_address). Only then
is the organisation added as a lead, verified, in its niche, with source = the SOURCE key
(sasra, cbk_psp...), so bounces, stats and the CRM report results per source.
Organisations not added yet wait in list_entries; a site that can't be read is tried again
by the prepare job, up to contacts.MAX_LIST_TRIES times.

--dry-run reads and parses the list and counts what is already known, but searches nothing,
reads no website and saves nothing.
"""
import io
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

import config
import contacts
import db
import ranking
import targeting
import websearch

WEBSITES_PER_DAY = 20    # pending website lookups the prepare job does a day

SCHEMA = """
CREATE TABLE IF NOT EXISTS list_entries (
    id INTEGER PRIMARY KEY,
    -- the SOURCE key (sasra, cbk_psp...), the list's title, its date and where it was read
    source TEXT NOT NULL,
    list_name TEXT,
    list_date TEXT,
    list_url TEXT,
    niche TEXT,
    name TEXT NOT NULL,
    website TEXT,
    -- 'list' (the list gives it) or 'search' (found by a Tavily search)
    website_from TEXT,
    -- pending: no website yet, waiting for a search; no_website: the search found none;
    -- unreadable: the site couldn't be read (tried again); no_email: the site publishes no
    -- usable address; added: now lead lead_id
    state TEXT NOT NULL,
    tries INTEGER DEFAULT 0,
    searched_at TEXT,
    checked_at TEXT,
    lead_id INTEGER,
    note TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# Search results that are never an organisation's own website
NOT_OWN_SITE = ("go.ke", ".gov", "cma.or.ke", "estateagentsboard", "kenyalaw", "the-star.co.ke",
                "thekenyatimes", "peopledaily", "eastleighvoice", "mondaq", "the-star", "allafrica",
                "kenyacompanies", "kenyaplex", "softkenya", "victormatara", "paybillke", "opencorporates",
                "infoisinfo", "kenyayote", "mapcarta", "waze.com", "tripadvisor")
# Words in a name that say what it is, not who: left out when matching it to a website
GENERIC_WORDS = targeting.LEGAL_WORDS | {
    "sacco", "society", "societies", "cooperative", "co-operative", "savings", "credit", "and", "of",
    "water", "sanitation", "sewerage", "services", "service", "insurance", "assurance", "brokers",
    "broker", "group", "holdings", "international", "east", "africa", "african", "estate", "estates",
    "agency", "agencies", "real", "properties", "property", "consultants", "management", "microfinance",
    "bank", "finance", "financial", "capital", "investment", "investments", "for", "a", "in", "provider"}


class Unavailable(Exception):
    """The list can't be read automatically (a bot check, a broken certificate, a site down)."""


# --- Reading a list -------------------------------------------------------------------------

@dataclass
class Doc:
    data: bytes
    url: str          # where it was read from: a URL or a file path
    modified: str = None  # the page's own last-modified date, if it says
    dated: str = None     # the list's own date, when its parser found it

    @property
    def pdf(self):
        return self.data.startswith(b"%PDF")

    def text(self):
        if self.pdf:
            return targeting.pdf_text(self.data)
        return BeautifulSoup(self.data, "html.parser").get_text("\n")


def fetch(src):
    """The list at a URL or a file path, as a Doc. Raises Unavailable if a site can't give it."""
    if not re.match(r"https?://", src):
        with open(src, "rb") as f:
            data = f.read()
        return Doc(data, src, None if data.startswith(b"%PDF") else modified_date(data))
    try:
        r = requests.get(src, timeout=120, headers=targeting.BROWSER_HEADERS)
    except requests.exceptions.SSLError as e:
        why = "its security certificate has expired" if "expired" in str(e) else "its security certificate fails"
        raise Unavailable(f"{src}: {why}")
    except requests.RequestException as e:
        raise Unavailable(f"{src}: {type(e).__name__}")
    if r.status_code >= 400:
        raise Unavailable(f"{src}: HTTP {r.status_code}")
    if re.search(rb"<title>\s*Bot Verification|grecaptcha|cf-challenge", r.content[:5000], re.I):
        raise Unavailable(f"{src}: the site asks for a bot check (reCAPTCHA); "
                          f"download the list in a browser and run import-list with the file")
    return Doc(r.content, src, modified_date(r.content, r.headers.get("Last-Modified")))


def modified_date(html, header=None):
    """A web page's own date: article:modified_time (WordPress), else the Last-Modified header."""
    m = re.search(rb'article:modified_time"\s+content="(\d{4}-\d{2}-\d{2})', html or b"")
    if m:
        return m.group(1).decode()
    if header:
        try:
            return datetime.strptime(header[5:16], "%d %b %Y").date().isoformat()
        except ValueError:
            pass
    return None


DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
MONTH = "(" + "|".join(targeting.MONTHS) + "|" + "|".join(m[:3] for m in targeting.MONTHS) + r")\.?"


def as_date(day, month, year):
    month = next(i for i, m in enumerate(targeting.MONTHS, 1) if m.lower().startswith(month.lower()[:3]))
    return date(int(year), month, int(day)).isoformat()


def list_date(doc, text=None):
    """The date the list itself gives ("Dated the 25th March, 2026", "as at 30th September 2026"),
    else a month and year in its file name, else the page's own date. None if nothing says."""
    if doc.dated:
        return doc.dated
    if not doc.pdf and doc.modified:
        return doc.modified
    text = text if text is not None else doc.text()
    # spaces may be missing in a PDF's text ("DatedatNairobithis3rddayofFebruary2026")
    for pattern in (rf"Dated\s*(?:at\s*[A-Za-z]+?\s*)?(?:the|this)?\s*{DAY}\s*(?:day\s*of)?\s*{MONTH},?\s*(\d{{4}})",
                    rf"as\s*at\s*(?:the\s*)?{DAY}\s*(?:day\s*of)?\s*{MONTH},?\s*(\d{{4}})"):
        m = re.search(pattern, text, re.I)
        if m:
            return as_date(*m.groups())
    m = re.search(r"\b" + MONTH + r"[-_\s,]*(?:(\d{1,2})[-_\s,]+)?(\d{4})", doc.url, re.I)
    if m:
        whole = as_date(m.group(2) or 1, m.group(1), m.group(3))
        return whole if m.group(2) else whole[:7]
    return doc.modified


def clean_name(name):
    name = re.sub(r"\s+", " ", name or "").strip(" .,;:-–")
    return name if re.search(r"[A-Za-z]{2}", name) else ""


def entry(name, website=None):
    website = ranking.urlparse_domain(website) if website and "." in website else None
    return {"name": clean_name(name), "website": website}


NAME_HEADER = re.compile(r"\b(?:name|utility|company|firm|institution|insurer|reinsurer|broker|provider|"
                         r"sacco|entity|organi[sz]ation|licensee|participant)s?\b", re.I)
WEB_HEADER = re.compile(r"\b(?:website|web|url)\b", re.I)


def from_rows(rows):
    """Entries from a table's rows (lists of cell texts): the name column is the first whose
    header names it, the website column the one headed website, web or url."""
    out, name_col, web_col = [], None, None
    for row in rows:
        cells = [" ".join((c or "").split()) for c in row]
        if name_col is None:
            name_col = next((i for i, c in enumerate(cells) if NAME_HEADER.search(c) and len(c) < 60), None)
            web_col = next((i for i, c in enumerate(cells) if WEB_HEADER.search(c) and len(c) < 30), None)
            continue
        if name_col < len(cells) and not NAME_HEADER.fullmatch(cells[name_col]):
            website = cells[web_col] if web_col is not None and web_col < len(cells) else None
            e = entry(re.sub(r"^\d+\.?\s+", "", cells[name_col]), website)
            if e["name"]:
                out.append(e)
    return out


def parse_html(doc):
    """Every row of the web page's largest table that has a name column."""
    soup = BeautifulSoup(doc.data, "html.parser")
    best = []
    for table in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]
        found = from_rows(rows)
        if len(found) > len(best):
            best = found
    return best


NUMBERED = re.compile(r"^(\d{1,3})[.)]?\s+(.+?)(?:\s+(?:P\.?\s?O\.?\s*Box|Private\s+Bag|Box\s+\d|Tel\b|\+254).*)?$", re.I)


def parse_pdf(doc):
    """A PDF list: its tables if it has any with a name column, else its numbered lines
    ("12. Acme Insurance Brokers Limited P.O. Box ...")."""
    import pdfplumber
    rows = []
    with pdfplumber.open(io.BytesIO(doc.data)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                rows += table
    found = from_rows(rows)
    if found:
        return found
    out, expected = [], 1
    for line in doc.text().splitlines():
        m = NUMBERED.match(" ".join(line.split()))
        if m and int(m.group(1)) in (expected, 1):
            expected = int(m.group(1)) + 1
            e = entry(m.group(2))
            if e["name"]:
                out.append(e)
    return out


def parse_any(doc):
    return parse_pdf(doc) if doc.pdf else parse_html(doc)


def parse_sasra(doc):
    """SASRA's gazette notice of licensed and authorised SACCOs: two columns a page, each a
    table. Schedules I (deposit-taking), II (non-withdrawable deposit-taking) and III
    (credit-only) are taken; IV (merged) and V (revoked) are not."""
    import pdfplumber
    out = []
    schedule, expected = None, 1
    with pdfplumber.open(io.BytesIO(doc.data)) as pdf:
        for page in pdf.pages:
            half = page.width / 2
            for box in ((0, 0, half, page.height), (half, 0, page.width, page.height)):
                current, boundary = None, None
                for words in lines_of(page.crop(box).extract_words()):
                    text = " ".join(w["text"] for w in words)
                    m = re.match(r"SCHEDULE\s+(I{1,3}|IV|V)\s*:", text)
                    if m:   # repeated at the top of each column: the numbering goes on
                        if m.group(1) != schedule:
                            schedule, expected = m.group(1), 1
                        current, boundary = None, None
                        continue
                    if schedule not in ("I", "II", "III"):
                        continue
                    postal = next((w for w in words if w["text"] == "Postal"), None)
                    if postal:
                        boundary, current = postal["x0"] - 1, None
                        continue
                    if boundary is None:
                        boundary = next((w["x0"] - 1 for w in words if w["text"].startswith("P.O")), None)
                    if boundary is None:
                        continue
                    number = words[0]["text"]
                    # the next number (one the PDF lost may be skipped)
                    if number.isdigit() and expected <= int(number) <= expected + 1 and words[0]["x0"] < boundary:
                        current = [w["text"] for w in words[1:] if w["x0"] < boundary]
                        out.append(current)
                        expected = int(number) + 1
                    elif current is not None and words[0]["x0"] < boundary:
                        current += [w["text"] for w in words if w["x0"] < boundary]
    names = [re.sub(r"(\w)- ([a-z])", r"\1\2", " ".join(n)) for n in out]   # "Regu- lated"
    names = [re.sub(r"\bRegulated\s+Non-?\s*(?:WDT-?)?\s*", "", n) for n in names]
    return [e for e in (entry(n) for n in names) if e["name"]]


def lines_of(words, gap=3):
    """Words grouped into lines (by their top), each line left to right."""
    lines = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= gap:
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def parse_earb(doc):
    """EARB's yearly gazette: the notice of listed estate agents companies (name, then
    P.O. Box address, one a line), up to its "Dated the" line. Individual agents are not taken."""
    text = doc.text()
    start = text.find("LISTED ESTATE AGENTS COMPANIES")
    if start < 0:
        return parse_any(doc)
    end = text.find("Dated the", start)
    doc.dated = list_date(Doc(b"%PDF", doc.url), text[end:end + 60]) if end > 0 else None
    out = []
    for line in text[start:end if end > 0 else None].splitlines():
        m = re.match(r"(.+?)\s+P\.?\s?O\.?\s*Box\b", " ".join(line.split()), re.I)
        if m:
            e = entry(m.group(1))
            if e["name"]:
                out.append(e)
    return out


def cbk_parser(parse):
    """A CBK directory parser from targeting.py, keeping only the name and the website it lists."""
    def run(doc):
        return [entry(e["name"], e.get("listed_website")) for e in parse(doc.data)]
    return run


def cbk_directory(doc):
    return [entry(e["name"], e.get("listed_website")) for e in targeting.parse_directory(targeting.pdf_text(doc.data))]


# --- Where each list is ------------------------------------------------------------------------

def link_on(page, pattern, what):
    """The newest link matching `pattern` on a page (by the upload folder or year in its address)."""
    doc = fetch(page)
    links = set(re.findall(pattern, doc.data.decode("utf-8", "ignore"), re.I))
    if not links:
        raise Unavailable(f"no {what} linked from {page}")

    def newest(u):
        m = re.search(r"/uploads/(\d{4})/(\d{2})/", u) or re.search(r"(20\d\d)()", u)
        return (m.groups() if m else ("0", "0"), u)
    return max(links, key=newest).replace("&#038;", "&").replace("&amp;", "&")


def wpdm_file(page):
    """The file behind a WordPress Download Manager page (its data-downloadurl)."""
    doc = fetch(page)
    m = re.search(rb'data-downloadurl="([^"]+)"', doc.data)
    if not m:
        raise Unavailable(f"no download on {page}")
    return re.sub(r"&refresh=[^&]*", "", m.group(1).decode().replace("&#038;", "&").replace("&amp;", "&"))


def _newest_sasra_link(page_html):
    """Of SASRA's yearly list pages, the one for the latest year."""
    links = re.findall(r'href="(https://www\.sasra\.go\.ke/download/list-of-licensed[^"]*?(20\d\d)/)"', page_html, re.I)
    return max(links, key=lambda l: l[1])[0] if links else None


def sasra_url():
    page = "https://www.sasra.go.ke/licensed-dt-saccos/"
    link = _newest_sasra_link(fetch(page).data.decode("utf-8", "ignore"))
    if not link:
        raise Unavailable(f"no SACCO list linked from {page}")
    return wpdm_file(link)


def cbk_url(page, name):
    return link_on(page, r'href="(https?://[^"]*' + name + r'[^"]*\.pdf)"', name.replace("-", " ").lower())


@dataclass
class List:
    name: str                 # the list's title
    niche: str
    url: object               # the latest version's address: a string, or a function finding it
    parse: object = parse_any

    def latest(self):
        return self.url() if callable(self.url) else self.url


@dataclass
class Source:
    key: str
    title: str
    lists: list = field(default_factory=list)


CBK_SUPERVISION = "https://www.centralbank.go.ke/bank-supervision/"
CBK_NPS = "https://www.centralbank.go.ke/national-payments-system/"

SOURCES = {s.key: s for s in (
    Source("sasra", "SASRA licensed SACCOs", [
        List("SASRA licensed and authorised SACCOs", "sacco", sasra_url, parse_sasra)]),
    Source("cbk_psp", "CBK authorised payment service providers", [
        List("CBK Directory of Authorized Payment Service Providers", "payment_provider",
             lambda: cbk_url(CBK_NPS, "Directory-of-Authori[sz]ed-Payment-Service-Providers"),
             cbk_parser(targeting.parse_psp))]),
    Source("cbk_mfb", "CBK microfinance banks, credit-only lenders and money remittance providers", [
        List("CBK Directory of Licensed Microfinance Banks", "microfinance",
             lambda: cbk_url(CBK_SUPERVISION, "Directory-of-Licen[cs]ed-Microfinance-Banks"), cbk_directory),
        # CBK publishes no directory of non-deposit-taking credit providers yet: its credit-only
        # lenders are the digital credit providers
        List("CBK Directory of Digital Credit Providers (credit-only lenders)", "microfinance",
             lambda: cbk_url(CBK_SUPERVISION, "Directory-of-Digital-Credit-Providers"), cbk_directory),
        List("CBK Directory of Licensed Money Remittance Providers", "fintech",
             lambda: cbk_url(CBK_SUPERVISION, "Directory-of-Licen[cs]ed-Money-Remittance-Providers"),
             cbk_parser(targeting.parse_mrp))]),
    Source("wasreb", "WASREB register of water service providers", [
        List("WASREB Register of Licences and Tariffs", "water_utility", "https://wasreb.go.ke/my-tariffs/")]),
    Source("ira", "IRA licensed insurers and insurance brokers", [
        List("IRA licensed insurers and brokers", "insurance",
             "https://www.ira.go.ke/assets/file/c6d2ff66-licensed-entities-2026.pdf")]),
    Source("earb", "EARB registered estate agency firms", [
        List("EARB Kenya Gazette: listed estate agents companies", "proptech",
             lambda: link_on("https://estateagentsboard.or.ke/official-publication-of-the-2026-kenya-gazette-"
                             "registered-estate-agents-and-estate-agents-companies/",
                             r'href="(https://estateagentsboard\.or\.ke/wp-content/uploads/[^"]*Gazette[^"]*\.pdf)"',
                             "gazette PDF"), parse_earb)]),
    Source("cma_sandbox", "Firms admitted to CMA's regulatory sandbox", [
        List("CMA regulatory sandbox admitted firms", "fintech", "https://www.sandbox.cma.or.ke/")]),
)}


# --- Website search ----------------------------------------------------------------------------

def search_budget():
    """Tavily calls the imports may still make this month: what is left of TAVILY_MONTHLY_CAP
    beyond the TAVILY_DISCOVERY_RESERVE kept for discovery. 0 when Tavily isn't set up."""
    if websearch.why_not_tavily():
        return 0
    return max(0, config.TAVILY_MONTHLY_CAP - websearch.calls("tavily") - config.TAVILY_DISCOVERY_RESERVE)


def significant_words(name):
    words = re.findall(r"[a-z0-9]+", targeting.TRADING.split(name.lower())[0])
    return [w for w in words if w not in GENERIC_WORDS and len(w) >= 3]


def acronym(name):
    """'KWS' for "Kenya Women Sacco": its words' first letters, or a bracketed short name."""
    m = re.search(r"\(([A-Za-z]{3,12})\)", name)
    return m.group(1).lower() if m else None


def own_site(name, results):
    """The organisation's own website among search results: the first one not on a social,
    directory, news or government site whose domain has a word of its name (or its short name
    in brackets) in it. None if none does."""
    import discover
    words, short = significant_words(name), acronym(name)
    for r in results:
        domain = ranking.urlparse_domain(r.get("href") or "")
        if not domain or any(s in domain for s in discover.SKIP_WORDS + NOT_OWN_SITE) or domain in discover.SKIP_EXACT:
            continue
        bare = re.sub(r"[^a-z0-9]", "", domain.rsplit(".", 2)[0] if domain.count(".") >= 2 else domain.split(".")[0])
        if any(w in bare for w in words) or (short and short in bare):
            return domain
    return None


def find_website(name):
    """One Tavily search for the organisation's website: (domain or None). Raises Unavailable
    if Tavily couldn't answer (the call still counts)."""
    try:
        results = websearch.tavily(f'"{name}" Kenya official website', 5)
    except Exception as e:
        raise Unavailable(f"Tavily failed ({type(e).__name__})")
    return own_site(name, results)


# --- Importing ---------------------------------------------------------------------------------

class Known:
    """Names and domains already known: every lead's, every organisation waiting in
    list_entries, and each one added as this run goes on."""

    def __init__(self, c):
        self.names, self.domains = {}, {}
        for lead in c.execute("SELECT company, email, website FROM leads"):
            self.add(lead["company"], targeting.own_domains([lead["email"]], lead["website"]),
                     f"lead {lead['company']}")
        for e in c.execute("SELECT source, name, website FROM list_entries"):
            self.add(e["name"], {e["website"]} - {None}, f"{e['source']} list entry {e['name']}")
        self.blocked = db.do_not_contact_domains()

    def add(self, name, domains, what):
        for k in targeting.name_keys(name or ""):
            self.names.setdefault(k, what)
        for d in domains:
            self.domains.setdefault(d, what)

    def match(self, e):
        key = next((k for k in targeting.name_keys(e["name"]) if k in self.names), None)
        if key:
            return f"same name as {self.names[key]}"
        if e["website"] and e["website"] in self.domains:
            return f"{e['website']} is {self.domains[e['website']]}'s"
        return None

    def do_not_contact(self, website):
        return bool(website) and any(website == d or website.endswith("." + d) for d in self.blocked)


def counts():
    return {"found": 0, "known": 0, "do_not_contact": 0, "website_found": 0, "website_pending": 0,
            "no_website": 0, "unreadable": 0, "email_found": 0, "no_email": 0, "added": 0}


def import_list(key, src, niche, dry_run=False, lst=None, budget=None):
    """Import one list (a file path or URL) under SOURCE `key` into niche `niche`. Returns the
    run's counts plus "list_date", "list_name" and "url". `budget`: a one-item list holding the
    Tavily calls this run may still make (shared between the lists of import-all)."""
    source = SOURCES.get(key)
    if source is None:
        raise SystemExit(f"Unknown source {key}: one of {', '.join(SOURCES)}.")
    if niche not in personalise_niches():
        raise SystemExit(f"Unknown niche {niche}: add it to uses.yaml first.")
    doc = fetch(src)
    if lst is None:   # a file given by hand: whichever of the source's parsers reads most from it
        lst, entries = max(((l, try_parse(l.parse, doc)) for l in source.lists), key=lambda p: len(p[1]))
        if not entries:
            lst, entries = source.lists[0], try_parse(parse_any, doc)
    else:
        entries = lst.parse(doc)
    if not entries:
        raise Unavailable(f"no organisations found in {src}")
    when = list_date(doc)
    n = counts()
    n.update(list_date=when, list_name=lst.name, url=src)
    db.init()
    with db.conn() as c:
        c.executescript(SCHEMA)
        known = Known(c)
    if budget is None:
        budget = [0 if dry_run else search_budget()]
    todo = []
    for e in entries:
        n["found"] += 1
        if known.match(e):
            n["known"] += 1
            continue
        if known.do_not_contact(e["website"]):
            n["do_not_contact"] += 1
            continue
        known.add(e["name"], {e["website"]} - {None}, f"{key} list entry {e['name']}")
        todo.append(e)
    if dry_run:
        n["website_found"] = sum(1 for e in todo if e["website"])
        n["website_pending"] = len(todo) - n["website_found"]
        n["searches_allowed"] = search_budget()
        return n
    for e in todo:
        with db.conn() as c:
            entry_id = c.execute(
                "INSERT INTO list_entries (source, list_name, list_date, list_url, niche, name, website,"
                " website_from, state) VALUES (?,?,?,?,?,?,?,?,'pending') RETURNING id",
                (key, lst.name, when, src, niche, e["name"], e["website"],
                 "list" if e["website"] else None)).fetchone()[0]
        n.update({k: n[k] + v for k, v in settle(entry_id, budget).items()})
    with db.conn() as c:
        c.executescript(targeting.RUNS_SCHEMA)
        c.execute("INSERT INTO import_runs (source, source_url, found, result) VALUES (?,?,?,?)",
                  (key, src, n["found"], json.dumps(n, ensure_ascii=False)))
    return n


def try_parse(parse, doc):
    """What `parse` reads from the list, or [] if it can't read this kind of list."""
    try:
        return parse(doc)
    except Exception:
        return []


def stamp():
    """Now, as the database keeps times (UTC text); websearch's clock, so a day's searches
    are counted on the same day as its Tavily calls."""
    return websearch.now().strftime("%Y-%m-%d %H:%M:%S")


def personalise_niches():
    import personalise
    return set(personalise.USES)


def settle(entry_id, budget):
    """Take one waiting organisation as far as it can go today: find its website (if the
    budget allows a search), read it and, if it publishes a usable address, add the lead.
    Returns the counts it changed."""
    n = {}
    bump = lambda k: n.__setitem__(k, n.get(k, 0) + 1)
    with db.conn() as c:
        e = dict(c.execute("SELECT * FROM list_entries WHERE id=?", (entry_id,)).fetchone())
    if not e["website"]:
        if budget[0] <= 0:
            bump("website_pending")
            return n
        budget[0] -= 1
        try:
            website = find_website(e["name"])
        except Unavailable as err:
            print(f"  {e['name']}: {err}; left pending")
            bump("website_pending")
            return n
        state = "pending" if website else "no_website"
        with db.conn() as c:
            c.execute("UPDATE list_entries SET website=?, website_from=?, state=?, searched_at=? WHERE id=?",
                      (website, "search" if website else None, state, stamp(), entry_id))
        if not website:
            bump("no_website")
            return n
        if db.do_not_contact(website):
            _set(entry_id, "no_email", note="do-not-contact domain")
            bump("do_not_contact")
            return n
        e["website"] = website
    bump("website_found")
    try:
        result = contacts.find(e["website"], e["name"])
    except Exception as err:   # one broken site never stops an import
        print(f"  {e['name']}: couldn't read {e['website']} ({err})")
        result = {"read": False}
    if not result.get("read"):
        tries = (e["tries"] or 0) + 1
        _set(entry_id, "unreadable" if tries < contacts.MAX_LIST_TRIES else "no_email", tries=tries,
             note=None if tries < contacts.MAX_LIST_TRIES else "site couldn't be read")
        bump("unreadable")
        return n
    if contacts.site_candidates(result):
        bump("email_found")
    picked = contacts.published_address(result)
    if picked == "waiting":
        _set(entry_id, "unreadable", tries=e["tries"] or 0, note="a mail server lookup timed out")
        bump("unreadable")
        return n
    if not picked:
        _set(entry_id, "no_email", note="the site publishes no usable address")
        bump("no_email")
        return n
    add_lead(e, result, *picked)
    bump("added")
    return n


def _set(entry_id, state, tries=None, note=None):
    with db.conn() as c:
        c.execute("UPDATE list_entries SET state=?, tries=COALESCE(?, tries), note=?, checked_at=? WHERE id=?",
                  (state, tries, note, stamp(), entry_id))


def add_lead(e, result, email, why, page, person):
    """Add the organisation as a verified lead at the address its site publishes."""
    with db.conn() as c:
        lead_id = c.execute(
            "INSERT INTO leads (company, website, industry, email, status, source, source_url, discovered_at,"
            " imported_at, address_source, address_page, address_checked_at, address_tries, contact_name,"
            " contact_title, contact_email, contact_url, contact_checked_at)"
            " VALUES (?,?,?,?,'verified',?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'site',?,CURRENT_TIMESTAMP,0,"
            " ?,?,?,?,CURRENT_TIMESTAMP) RETURNING id",
            (e["name"], e["website"], e["niche"], email, e["source"], e["list_url"], page,
             person and person["name"], person and person["title"], person and person["email"],
             person and person["url"])).fetchone()[0]
        db.log_event(c, lead_id, "discovered", source=e["source"], list=e["list_name"], list_date=e["list_date"],
                     url=e["list_url"])
        contacts.save_contacts(c, lead_id, result)
        db.log_event(c, lead_id, "list_address_settled", address=email, source="site", why=why)
        c.execute("UPDATE list_entries SET state='added', lead_id=?, checked_at=?, note=NULL WHERE id=?",
                  (lead_id, stamp(), e["id"]))
    print(f"  added {e['name']}: {email} ({why})")
    return lead_id


def import_all(dry_run=False):
    """Every list in SOURCES, latest version. Returns {key: [counts of each list]}. A list
    that can't be read is reported and the others go on."""
    budget = [0 if dry_run else search_budget()]
    out = {}
    for key, source in SOURCES.items():
        out[key] = []
        for lst in source.lists:
            print(f"{key}: {lst.name}...", flush=True)
            try:
                url = lst.latest()
                out[key].append(import_list(key, url, lst.niche, dry_run, lst, budget))
            except (Unavailable, SystemExit) as e:
                print(f"  not imported: {e}")
                out[key].append({"list_name": lst.name, "error": str(e)})
    return out


# --- The prepare job's daily step --------------------------------------------------------------

def searched_today():
    with db.conn() as c:
        c.executescript(SCHEMA)
        return c.execute("SELECT COUNT(*) FROM list_entries WHERE substr(searched_at, 1, 10) = ?",
                         (websearch.today(),)).fetchone()[0]


def daily(limit=WEBSITES_PER_DAY):
    """The prepare job's step: up to `limit` website lookups a day for organisations still
    pending (oldest first, within search_budget), and up to `limit` sites that couldn't be read
    tried again. Returns {"searched", "retried", "added"}."""
    db.init()
    with db.conn() as c:
        c.executescript(SCHEMA)
    searches = min(max(0, limit - searched_today()), search_budget())
    with db.conn() as c:
        pending = [r[0] for r in c.execute(
            "SELECT id FROM list_entries WHERE state='pending' AND website IS NULL ORDER BY id LIMIT ?", (searches,))]
        retry = [r[0] for r in c.execute(
            "SELECT id FROM list_entries WHERE (state='unreadable' OR (state='pending' AND website IS NOT NULL))"
            " AND COALESCE(substr(checked_at, 1, 10), '') < ? ORDER BY id LIMIT ?", (websearch.today(), limit))]
    budget, added = [searches], 0
    for entry_id in pending + retry:
        added += settle(entry_id, budget).get("added", 0)
    return {"searched": len(pending), "retried": len(retry), "added": added}


# --- Report ------------------------------------------------------------------------------------

COLUMNS = (("list date", "list_date"), ("found", "found"), ("already known", "known"),
           ("website found", "website_found"), ("website pending", "website_pending"),
           ("email found", "email_found"), ("added", "added"))


def table(results, dry_run=False):
    """Per source: list date, found, already known, website found, website pending, email
    found, added; then why some weren't added and the lists that couldn't be read."""
    lines = [f"{'SOURCE':12} " + " ".join(f"{h.upper():>15}" for h, _ in COLUMNS)]
    notes = []
    for key, runs in results.items():
        for r in runs:
            if "error" in r:
                notes.append(f"{key} ({r['list_name']}): not imported - {r['error']}")
                lines.append(f"{key:12} " + f"{'unavailable':>15}")
                continue
            cells = [r.get("list_date") or "not dated"] + [
                "-" if dry_run and k in ("email_found", "added") else r[k] for _, k in COLUMNS[1:]]
            lines.append(f"{key:12} " + " ".join(f"{str(v):>15}" for v in cells) + f"   {r['list_name']}")
            extra = [f"{r[k]} {what}" for k, what in (("do_not_contact", "on do-not-contact"),
                                                      ("no_website", "no website found"),
                                                      ("unreadable", "site unreadable (tried again)"),
                                                      ("no_email", "no usable address on the site"))
                     if r.get(k)]
            if extra:
                notes.append(f"{key} ({r['list_name']}): " + ", ".join(extra))
    if dry_run:
        allowed = search_budget()
        notes.append(f"Dry run: nothing searched, read or saved. Website pending = no website in the list; "
                     f"{allowed} Tavily searches are allowed this month above the "
                     f"{config.TAVILY_DISCOVERY_RESERVE} kept for discovery.")
    return lines + ([""] + notes if notes else [])


def main(args):
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    if args and args[0] == "all":
        results = import_all(dry_run)
    else:
        niche = targeting.take_option(args, "--niche")
        if len(args) != 2 or not niche:
            raise SystemExit("Usage: python main.py import-list SOURCE FILE_OR_URL --niche NICHE [--dry-run]\n"
                             f"Sources: {', '.join(SOURCES)}")
        try:
            results = {args[0]: [import_list(args[0], args[1], niche, dry_run)]}
        except Unavailable as e:
            raise SystemExit(f"Not imported: {e}")
    print("\n".join(table(results, dry_run)))
    return results


if __name__ == "__main__":
    main(sys.argv[1:])
