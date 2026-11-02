"""Finds new companies with a web search (websearch.py: Tavily, or DuckDuckGo) and adds them as leads.

  python main.py discover                  up to DISCOVER_PER_DAY new leads, shared across niches
  python main.py discover sacco --max 5    one category, stop after 5 new leads
  python main.py discover-log [days]       what discovery did per niche each day (default 7)

Only niches approved and not paused in uses.yaml are searched (a category named on the
command line is searched whatever uses.yaml says). DISCOVER_PER_DAY is shared evenly across
them, and they take turns, one search each, in PRIORITY order. A niche that has its share
stops; once every niche has its share or is out of searches, the ones with searches left go
on until the day's total is reached. Each niche's LIST_QUERIES come first: searches for
pages that list many companies (accelerator and incubator portfolios), whose linked company
sites are visited. Then its QUERIES, each run with a random town from TOWNS in place of {town}.

Searches stop for the day when today's share of the month's Tavily calls is used
(websearch.daily_allowance). What each niche did is kept in discovery_log by day: searches,
results, leads added and why the other sites visited were dropped.
A lead's name is the organisation's own name on its site, never its page title
(quality.org_name). Directories, marketplaces, events, associations, communities,
accelerators, media and government sites are never added (quality.excluded_reason), but the
company sites they link to are visited.
Every lead found is also appended to discovered.csv so you can review it in Excel.
"""
import csv
import json
import random
import re
import time
from collections import Counter, namedtuple
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config
import db
import personalise
import quality
import rotation
import scraper
import websearch

TOWNS = ["Nairobi", "Mombasa", "Kisumu", "Nakuru", "Eldoret", "Thika", "Nyeri", "Machakos"]

# Edit these freely. 3-5 queries per category; {town} is filled in from TOWNS.
QUERIES = {
    "sacco": [
        "SACCO society {town} Kenya contact us",
        "savings and credit cooperative {town} email",
        "licensed deposit taking SACCO {town}",
        "teachers OR matatu SACCO {town} Kenya contacts",
    ],
    "microfinance": [
        "microfinance institution {town} Kenya contact us",
        "micro lender loans {town} email",
        "credit company business loans {town} Kenya",
    ],
    "school": [
        "private primary school {town} Kenya contact us",
        "secondary school {town} admissions email",
        "academy school {town} Kenya contacts",
        "international school {town} Kenya",
    ],
    "college": [
        "college {town} Kenya admissions contact us",
        "technical training institute {town} email",
        "medical training college {town} Kenya",
    ],
    "church": [
        "church {town} Kenya contact us",
        "ministries church {town} email office",
        "cathedral OR parish {town} Kenya contacts",
    ],
    "chama": [
        "investment group chama {town} Kenya contact",
        "self help group investment club {town}",
        "chama investment company {town} Kenya email",
    ],
    "health": [
        "private hospital {town} Kenya contact us",
        "medical clinic {town} email appointments",
        "dental clinic {town} Kenya contacts",
        "pharmacy chemist {town} Kenya contact",
    ],
    "transport": [
        "bus company {town} Kenya booking contact",
        "shuttle services {town} email",
        "matatu OR coach company {town} Kenya contacts",
    ],
    "retail": [
        "supermarket {town} Kenya contact us",
        "electronics shop {town} email",
        "hardware store {town} Kenya contacts",
    ],
    "insurance": [
        "insurance agency {town} Kenya contact us",
        "insurance brokers {town} email",
        "insurance company {town} Kenya contacts",
    ],
    "real_estate": [
        "property management company {town} Kenya contact us",
        "real estate agents {town} email",
        "apartments to let agency {town} Kenya",
    ],
    "utility": [
        "water and sewerage company {town} Kenya contact us",
        "internet service provider {town} email",
        "solar energy company {town} Kenya contacts",
    ],
    "hospitality": [
        "hotel {town} Kenya reservations contact",
        "restaurant {town} email bookings",
        "lodge OR guest house {town} Kenya contact us",
    ],
    "gym_salon": [
        "gym fitness centre {town} Kenya contact",
        "salon and spa {town} email bookings",
        "barbershop OR beauty parlour {town} Kenya contacts",
    ],
    "driving_school": [
        "driving school {town} Kenya contact us",
        "NTSA approved driving school {town} email",
        "driving lessons {town} Kenya fees",
    ],
    "automotive": [
        "car dealer {town} Kenya contact us",
        "garage auto repair {town} email",
        "motor spares shop {town} Kenya contacts",
    ],
    "agriculture": [
        "agrovet {town} Kenya contact us",
        "farmers cooperative society {town} email",
        "agribusiness company {town} Kenya contacts",
    ],
    "events": [
        "event planners {town} Kenya contact us",
        "wedding planner {town} email",
        "event venue gardens {town} Kenya bookings",
    ],
    "security": [
        "security company {town} Kenya contact us",
        "security guards services {town} email",
        "alarm and CCTV company {town} Kenya contacts",
    ],
    "professional": [
        "law firm advocates {town} Kenya contact us",
        "audit accounting firm {town} email",
        "consulting firm {town} Kenya contacts",
    ],
    "ngo": [
        "NGO {town} Kenya contact us",
        "community based organisation {town} email",
        "foundation charity {town} Kenya contacts",
    ],
    "logistics": [
        "courier company {town} Kenya contact us",
        "logistics clearing and forwarding {town} email",
        "parcel delivery services {town} Kenya contacts",
    ],
    # Tech companies: they send OTPs, payment alerts and order updates all day
    "tech_startup": [
        "tech startup Nairobi Kenya contact us",
        "Kenyan startup launched 2024",
        "startup Kenya seed funding",
        "software startup {town} Kenya contact",
        "mobile app startup Kenya sign up",
    ],
    "fintech": [
        "fintech startup Nairobi",
        "fintech company Kenya contact us",
        "mobile loans app Kenya contact",
        "payments startup Kenya API",
        "digital wallet OR savings app Kenya contact us",
    ],
    "saas": [
        "SaaS company Nairobi",
        "cloud software company Kenya contact us",
        "payroll OR HR software Kenya contact us",
        "POS OR inventory software Kenya contact",
        "school management system software Kenya contact",
    ],
    "ecommerce": [
        "e-commerce startup Kenya contact",
        "online shop Kenya delivery contact us",
        "online marketplace Kenya sellers contact",
        "buy online {town} Kenya store contact us",
    ],
    "delivery_app": [
        "delivery app Nairobi contact",
        "food delivery startup Kenya",
        "on-demand delivery service Kenya app contact us",
        "grocery OR errand delivery app {town} Kenya",
    ],
    "edtech": [
        "edtech Kenya contact us",
        "e-learning platform Kenya contact",
        "online learning startup Nairobi",
        "edtech startup Kenya schools parents app",
    ],
    "healthtech": [
        "healthtech startup Kenya",
        "telemedicine Kenya contact us",
        "digital health platform Nairobi contact",
        "online pharmacy Kenya delivery contact us",
    ],
    "agritech": [
        "agritech startup Kenya",
        "digital platform for farmers Kenya contact us",
        "farm inputs marketplace Kenya startup",
        "agri-fintech smallholder farmers Kenya app contact",
    ],
    "proptech": [
        "proptech Kenya contact us",
        "property listing platform Kenya contact",
        "rent collection software Kenya landlords",
        "real estate technology startup Nairobi",
    ],
    "insurtech": [
        "insurtech Kenya contact us",
        "digital insurance platform Kenya",
        "buy insurance online Kenya startup contact",
        "micro-insurance startup Kenya mobile",
    ],
    "logistics_tech": [
        "logistics technology startup Kenya",
        "fleet management software Kenya contact us",
        "truck booking platform Kenya contact",
        "last mile delivery platform Nairobi API",
    ],
    "events_ticketing": [
        "event ticketing platform Kenya contact",
        "buy event tickets online Kenya",
        "online ticketing company Nairobi contact us",
        "event registration software Kenya",
    ],
    "digital_marketing_agency": [
        "digital marketing agency Nairobi contact us",
        "digital marketing agency {town} Kenya email",
        "social media marketing company Kenya contact",
        "SEO agency Nairobi contact us",
    ],
    "payment_provider": [
        "payment gateway Kenya",
        "payment service provider Kenya",
        "money remittance company Kenya",
    ],
    "payg_energy": [
        "pay as you go solar Kenya",
        "solar home system company Kenya",
        "pay as you go clean cooking Kenya",
    ],
    "software_agency": [
        "software development company Nairobi",
        "web and mobile app developers Kenya",
        "system integrator Kenya",
    ],
    "software_vendor": [
        "school management system Kenya",
        "hospital management system Kenya",
        "SACCO management software Kenya",
        "POS system Kenya",
        "ERP software Kenya",
        "property management software Kenya",
    ],
    "coworking": [
        "coworking space Nairobi contact",
        "shared office space {town} Kenya contact us",
        "co-working hub Kenya membership",
        "serviced offices Nairobi contact us",
    ],
}

# Searches for pages that list many companies. Each result page is opened and the
# company sites it links to are visited and added under that category.
LIST_QUERIES = {
    "tech_startup": [
        "accelerator portfolio startups Kenya",
        "incubator Nairobi portfolio companies",
        "startup accelerator Kenya cohort startups",
        "Kenya startup hub member companies portfolio",
        "venture capital Kenya portfolio companies Nairobi",
    ],
}
LIST_QUERIES_PER_RUN = 2    # picked at random from the category's list
LIST_PAGES_PER_QUERY = 4    # result pages opened per list query
LINKS_PER_LIST = 25         # company links followed per page
# A site with a page like this is an accelerator, incubator or investor: its
# portfolio companies are visited and the site itself is not added
PORTFOLIO_LINK = re.compile(r"portfolio|our[- ]startups|our[- ]companies|alumni|cohort|investments|"
                            r"/startups|/companies|/ventures", re.I)
PORTFOLIO_PAGES_PER_SITE = 2

# Search order when no category is given
PRIORITY = [
    "sacco", "microfinance",
    "tech_startup", "fintech", "saas", "ecommerce", "delivery_app", "edtech", "healthtech",
    "payment_provider", "payg_energy", "software_agency", "software_vendor",
    "agritech", "proptech", "insurtech", "logistics_tech", "events_ticketing",
    "digital_marketing_agency", "coworking",
    "school", "college", "church", "chama", "health", "transport",
    "retail", "insurance", "real_estate", "utility", "hospitality", "gym_salon",
    "driving_school", "automotive", "agriculture", "events", "security",
    "professional", "ngo", "logistics",
]

SKIP_WORDS = ("facebook", "linkedin", "wikipedia", "yellowpages", "twitter",
              "instagram", "youtube", "tiktok",
              # more social, app stores and link shorteners
              "whatsapp", "telegram", "pinterest", "threads.net", "medium.com",
              "google.", "linktr.ee", "github.com",
              # directories, listings and job boards
              "businesslist", "kenyabizlist", "africabz", "crunchbase", "tracxn", "f6s.com",
              "wellfound", "angel.co", "glassdoor", "brightermonday", "startuplist", "zoominfo",
              "pitchbook", "rocketreach", "dnb.com", "cybo.com",
              # news sites
              "tuko.co.ke", "nation.africa", "standardmedia", "businessdailyafrica", "kenyans.co.ke",
              "techweez", "techcabal", "disruptafrica", "disrupt-africa", "techcrunch", "capitalfm",
              "techinsider", "startupmap", "failory", "dealroom", "nairobionline")
# Short addresses that must match exactly ("t.me" is also inside "smart.media")
SKIP_EXACT = ("x.com", "t.me", "wa.me", "bit.ly", "apple.com")
# Banks without "bank" in their address, and internet providers
BANK_NAMES = ("kcbgroup", "equitygroup", "absa.", "ncbagroup", "stanbic", "dtbafrica", "stanchart", "imbank")
ISP_NAMES = ("safaricom", "airtel", "telkom", "zuku", "faiba", "liquid.tech", "liquidtelecom",
             "wananchi", "poainternet", "mawingu", "starlink", "vilcom")
ISP_TITLE = re.compile(r"internet service provider|\bISP\b|fibre internet|fiber internet|"
                       r"home internet|broadband", re.I)
# Other bulk SMS companies are competitors, not leads. A site is one if its title, description
# or main headings sell SMS (RIVAL_HEADLINE), if its address is a known one or a name ending in
# "sms" ("advantasms", "bongasms"; not "tourisms"), or if the email it gives is on such a site: a product
# site whose address is an SMS company's belongs to that company.
RIVAL_HEADLINE = re.compile(r"bulk\s*sms|sms\s+gateway|sms\s+(?:service\s+)?provider|sms\s+marketing", re.I)
# Sites that hide their headings from a plain fetch, and our own
RIVAL_DOMAINS = ("africastalking", "advantasms", "celcomafrica", "onfonmedia", "bongasms", "bongatech",
                 "mobitechtechnologies", "movesms", "blessedtexts", "sematime", "tiaraconnect",
                 "hostpinnacle", "infobip", "twilio", "vonage", "smsleopard", "textsms", "talksasa")
FREE_MAIL = ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "icloud.com", "me.com", "ymail.com")
# Law firms come up in searches for other niches ("payment service provider Kenya"). Only the
# professional category looks for them on purpose. Read from the page title and the address.
LAW_FIRM_TITLE = re.compile(r"\badvocates?\b|\blaw\s+(?:firm|offices?|chambers)\b|\bLLP\b|"
                            r"\battorneys?\b|\bsolicitors?\b|\blegal\s+(?:advisory|practice|consultants?)\b", re.I)
LAW_FIRM_DOMAIN = re.compile(r"advocates|lawfirm|lawyers|attorneys|(?:^|[.-])llp(?:$|[.-])")
RESULTS_PER_QUERY = 10
CSV_PATH = Path(__file__).parent / "discovered.csv"
CSV_COLUMNS = ["company", "website", "industry", "email", "query"]
LOG_SCHEMA = """CREATE TABLE IF NOT EXISTS discovery_log (
    day TEXT NOT NULL,
    niche TEXT NOT NULL,
    searches INTEGER NOT NULL DEFAULT 0,
    results INTEGER NOT NULL DEFAULT 0,
    added INTEGER NOT NULL DEFAULT 0,
    -- JSON {reason: sites}: why the sites visited weren't added
    dropped TEXT,
    PRIMARY KEY (day, niche)
)"""


class Tally:
    """One niche's discovery in one run: searches made, results they gave, leads added and
    why the other sites visited were dropped."""
    def __init__(self):
        self.searches = self.results = self.added = 0
        self.dropped = Counter()

    def __str__(self):
        drops = ", ".join(f"{n} {why}" for why, n in self.dropped.most_common())
        return (f"{self.searches} searches, {self.results} results, {self.added} added"
                + (f"; dropped: {drops}" if drops else ""))


def dropped(tally, why):
    if tally:
        tally.dropped[why] += 1
    return None


def domain_of(url):
    if "//" not in url:
        url = "//" + url
    return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")


def skip_domain(domain):
    if not domain or any(w in domain for w in SKIP_WORDS):
        return True
    if any(domain == d or domain.endswith("." + d) for d in SKIP_EXACT):
        return True
    if any(w in domain for w in BANK_NAMES + ISP_NAMES):
        return True
    # Banks are out, microfinance banks are in
    return "bank" in domain and "microfinance" not in domain


def skip_title(title, category):
    """A reason to skip a site going by its page title, or None."""
    # The utility category searches for internet providers on purpose
    if category != "utility" and ISP_TITLE.search(title or ""):
        return "internet provider"
    return None


def law_firm_reason(domain, title, category):
    """A reason to skip a law firm found for any category but professional, or None."""
    if category == "professional":
        return None
    m = LAW_FIRM_TITLE.search(title or "") or LAW_FIRM_DOMAIN.search(domain or "")
    return f'a law firm ("{m.group(0)}")' if m else None


def rival_reason(domain, headline):
    """Why this site is another bulk SMS company, or None. `headline` is its title,
    description and main headings (look_at)."""
    labels = re.split(r"[.-]", domain or "")
    if any(r in (domain or "") for r in RIVAL_DOMAINS) or any(
            l.endswith("sms") and not l.endswith("isms") for l in labels):
        return f"another bulk SMS company ({domain})"
    m = RIVAL_HEADLINE.search(headline or "")
    return f'another bulk SMS company ("{m.group(0)}" on {domain})' if m else None


def known_domains():
    with db.conn() as c:
        rows = c.execute("SELECT website FROM leads WHERE website IS NOT NULL").fetchall()
    return {domain_of(r["website"]) for r in rows if r["website"]}


# A homepage as look_at reads it. headline: the title, descriptions and main headings;
# name: the organisation's own name (quality.org_name); intro: the title and descriptions
Page = namedtuple("Page", "title kenya headline name intro")
# A site that is never a lead but lists companies (quality.excluded_reason)
Listing = namedtuple("Listing", "home why")


def look_at(url):
    """Fetch the homepage. Returns a Page, or None if unreachable."""
    try:
        r = requests.get(url, headers=scraper.HEADERS, timeout=10)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    soup = BeautifulSoup(r.content, "html.parser")  # bytes: lets bs4 detect the encoding
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    intro = quality.page_intro(soup)
    headline = " | ".join([intro] + [h.get_text(" ", strip=True) for h in soup.select("h1, h2")])
    return Page(title, "kenya" in soup.get_text(" ").lower(), headline,
                quality.org_name(soup, domain_of(url)), intro)


def email_rival(email, domain):
    """Why the site's email address belongs to another bulk SMS company, or None. Only an
    address on another business's domain is looked at (its homepage is read once)."""
    mail_domain = email.rsplit("@", 1)[-1].lower()
    if mail_domain in FREE_MAIL or mail_domain == domain or mail_domain.endswith("." + domain) \
            or domain.endswith("." + mail_domain):
        return None
    page = look_at(f"https://{mail_domain}")
    return rival_reason(mail_domain, page[2] if page else "")


def search(query):
    return websearch.search(query, RESULTS_PER_QUERY)


def save_csv(row):
    is_new = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if is_new:
            w.writeheader()
        w.writerow(row)


def consider(url, category, query, seen, tally=None):
    """Visit one company site and add it as a lead if it qualifies.
    Returns the new lead's row, None, or a Listing for a directory, event, association...
    (whose companies visit() goes on to). Counts the outcome in `tally`."""
    domain = domain_of(url)
    if domain in seen:
        return dropped(tally, "already known")
    if skip_domain(domain):
        return dropped(tally, "social, directory, news or bank site")
    seen.add(domain)
    home = f"{urlparse(url).scheme or 'https'}://{urlparse(url).netloc}"
    page = look_at(home)
    if not page:
        return dropped(tally, "site unreachable")
    title, mentions_kenya, headline, org, intro = page
    if not (domain.endswith(".ke") or mentions_kenya):
        return dropped(tally, "not in Kenya")
    why = quality.excluded_reason(domain, intro or title)
    if why:
        print(f"    {domain}: skipped ({why}); visiting the companies it lists")
        dropped(tally, why)
        return Listing(home, why)
    for why, reason in (("internet provider", lambda: skip_title(title, category)),
                        ("law firm", lambda: law_firm_reason(domain, title, category)),
                        ("bulk SMS company", lambda: rival_reason(domain, headline))):
        if (reason := reason()):
            print(f"    {domain}: skipped ({reason})")
            return dropped(tally, why)
    email = scraper.pick_best(scraper.scrape_site(home))
    if not email:
        print(f"    {domain}: no email found")
        return dropped(tally, "no email found")
    reason = email_rival(email, domain)
    if reason:
        print(f"    {domain}: skipped ({email}: {reason})")
        return dropped(tally, "bulk SMS company's email")
    if db.is_suppressed(email):
        print(f"    {domain}: {email} is on the suppression list")
        return dropped(tally, "on the suppression list")
    name = org or quality.domain_brand(domain)
    if not db.add_lead(name, domain, category, email, "search", query, url):
        print(f"    {domain}: {email} already a lead")
        return dropped(tally, "email already a lead")
    row = {"company": name, "website": domain, "industry": category,
           "email": email, "query": query}
    save_csv(row)
    print(f"    + {name}  <{email}>")
    if tally:
        tally.added += 1
    return row


def visit(url, category, query, seen, tally, limit):
    """consider() one site and return the leads added (at most `limit`). A site that is never
    a lead but lists companies (a directory, event, association, accelerator...) has the
    companies it links to visited instead; their own links are not followed."""
    got = consider(url, category, query, seen, tally)
    if not isinstance(got, Listing):
        return [got] if got else []
    added = []
    for target in listed_companies(got.home, seen):
        row = consider(target, category, f"listed on {got.home}", seen, tally)
        if isinstance(row, dict):
            added.append(row)
            if len(added) >= limit:
                break
    return added


def listed_companies(home, seen):
    """The company sites a listing site links to, from its homepage and portfolio pages."""
    out = []
    for page in [home] + portfolio_pages(home):
        out += [u for u in listed_sites(page) if domain_of(u) not in seen and u not in out]
    return out


def listed_sites(page_url):
    """The outside websites a list page links to, e.g. an accelerator's portfolio."""
    soup = scraper.fetch(page_url)
    if soup is None:
        return []
    own, got, out = domain_of(page_url), set(), []
    for a in soup.select('a[href^="http"]'):
        d = domain_of(a["href"])
        if not d or d in got or d == own or d.endswith("." + own) or own.endswith("." + d) or skip_domain(d):
            continue
        got.add(d)
        out.append(a["href"])
    return out[:LINKS_PER_LIST]


def portfolio_pages(home):
    """Links on a site's homepage to its own portfolio or startups pages."""
    soup = scraper.fetch(home)
    if soup is None:
        return []
    own, out = domain_of(home), []
    for a in soup.select("a[href]"):
        url = urljoin(home, a["href"]).split("#")[0]
        if domain_of(url) != own or url.rstrip("/") == home.rstrip("/") or url in out:
            continue
        if PORTFOLIO_LINK.search(a["href"]) or PORTFOLIO_LINK.search(a.get_text(" ", strip=True)):
            out.append(url)
    return out[:PORTFOLIO_PAGES_PER_SITE]


def from_list_pages(results, category, limit, seen, tally):
    """Visit the companies the list pages in these search results link to."""
    added, pages = [], set()
    for res in results[:LIST_PAGES_PER_QUERY]:
        page_url = res.get("href") or ""
        if not page_url or page_url in pages:
            continue
        pages.add(page_url)
        sites = [u for u in listed_sites(page_url) if domain_of(u) not in seen]
        print(f"   list page: {page_url} ({len(sites)} sites to visit)")
        for url in sites:
            home = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
            hub_pages = portfolio_pages(home)
            if not hub_pages:  # an ordinary company
                targets = [(url, page_url)]
            else:              # an accelerator or investor: go to the companies it lists
                seen.add(domain_of(url))
                targets = [(u, hp) for hp in hub_pages for u in listed_sites(hp) if domain_of(u) not in seen]
                print(f"    {domain_of(url)}: has a portfolio page, visiting {len(targets)} companies listed there")
            for target, source in targets:
                row = consider(target, category, f"listed on {source}", seen, tally)
                if isinstance(row, dict):
                    added.append(row)
                    if len(added) >= limit:
                        return added
    return added


def searches(category):
    """One niche's searches in the order they are made, as (kind, query): its list searches
    first, since one of those finds many companies, then its ordinary ones."""
    lists = LIST_QUERIES.get(category, [])
    return ([("list", q) for q in random.sample(lists, min(LIST_QUERIES_PER_RUN, len(lists)))]
            + [("search", t.format(town=random.choice(TOWNS))) for t in QUERIES[category]])


def run_search(kind, query, category, limit, seen, tally):
    """Make one search and visit what it finds. Returns the leads added (at most `limit`).
    Raises websearch.DailyAllowanceUsed when today's searches are used."""
    print(f"  {'list search' if kind == 'list' else 'search'}: {query}")
    results = search(query)
    tally.searches += 1
    tally.results += len(results)
    time.sleep(random.uniform(2, 4))
    if kind == "list":
        return from_list_pages(results, category, limit, seen, tally)
    added = []
    for res in results:
        added += visit(res.get("href") or "", category, query, seen, tally, limit - len(added))
        if len(added) >= limit:
            break
    return added


def niches(on=None):
    """The niches searched when none is named: those in PRIORITY that are approved in
    uses.yaml and not paused on the day `on` (today in Nairobi)."""
    on = on or rotation.today()
    return [n for n in PRIORITY
            if personalise.approved({"industry": n}) and not personalise.paused({"industry": n}, on)]


def shares(names, total):
    """{niche: leads it may add before the others have theirs}: total split evenly, the
    remainder going one each to the first niches."""
    base, extra = divmod(total, len(names))
    return {n: base + (i < extra) for i, n in enumerate(names)}


def run(category=None, max_new=None):
    max_new = config.DISCOVER_PER_DAY if max_new is None else max_new
    if category and category not in QUERIES:
        print(f"Unknown category '{category}'. Choose from: {', '.join(PRIORITY)}")
        return []
    names = [category] if category else niches()
    if not names:
        print("No niche is approved and unpaused in uses.yaml: nothing to search.")
        return []
    share = shares(names, max_new)
    plans = {n: searches(n) for n in names}
    tallies = {n: Tally() for n in names}
    seen = known_domains()
    added = []
    try:
        while len(added) < max_new:
            left = [n for n in names if plans[n]]
            if not left:
                break
            # Niches short of their share take turns; once none is, the day's total is open to all
            short = [n for n in left if tallies[n].added < share[n]]
            for n in short or left:
                room = max_new - len(added)
                if room <= 0:
                    break
                kind, query = plans[n].pop(0)
                print(f"[{n}]")
                added += run_search(kind, query, n, min(room, share[n] - tallies[n].added) if short else room,
                                    seen, tallies[n])
    except websearch.DailyAllowanceUsed as e:
        print(f"Stopped for today: {e}.")
    finally:
        save_log(tallies)
    for n, t in tallies.items():
        if t.searches:
            print(f"  {n}: {t}")
    print(f"Added {len(added)} new leads. Review them in {CSV_PATH.name}.")
    return added


def save_log(tallies, day=None):
    """Add this run's tallies to discovery_log for the day (UTC, as websearch counts searches)."""
    day = day or websearch.today()
    with db.conn() as c:
        c.execute(LOG_SCHEMA)
        for niche, t in tallies.items():
            if not t.searches:
                continue
            row = c.execute("SELECT * FROM discovery_log WHERE day=? AND niche=?", (day, niche)).fetchone()
            drops = Counter(json.loads(row["dropped"] or "{}") if row else {}) + t.dropped
            c.execute("INSERT INTO discovery_log (day, niche, searches, results, added, dropped)"
                      " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (day, niche) DO UPDATE SET"
                      " searches = discovery_log.searches + excluded.searches,"
                      " results = discovery_log.results + excluded.results,"
                      " added = discovery_log.added + excluded.added, dropped = excluded.dropped",
                      (day, niche, t.searches, t.results, t.added, json.dumps(dict(drops))))


def log_rows(days=7):
    """discovery_log rows for the last `days` days, newest day first, as dicts."""
    since = (websearch.now().date() - timedelta(days=days - 1)).isoformat()
    with db.conn() as c:
        c.execute(LOG_SCHEMA)
        rows = c.execute("SELECT * FROM discovery_log WHERE day >= ? ORDER BY day DESC, niche",
                         (since,)).fetchall()
    return [dict(r, dropped=json.loads(r["dropped"] or "{}")) for r in rows]


def show_log(days=7):
    rows = log_rows(days)
    if not rows:
        print(f"No discovery in the last {days} days.")
        return
    print(f"{'DAY':10}  {'NICHE':20}  {'SEARCHES':>8}  {'RESULTS':>7}  {'ADDED':>5}  DROPPED")
    for r in rows:
        drops = ", ".join(f"{n} {why}" for why, n in sorted(r["dropped"].items(), key=lambda x: -x[1]))
        print(f"{r['day']:10}  {r['niche']:20}  {r['searches']:>8}  {r['results']:>7}  {r['added']:>5}  {drops}")
