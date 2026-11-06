"""Who at a lead the first email goes to, read from the lead's own website before it is sent.
Free sources only: the site's own pages, its sitemap, its JSON-LD and PDFs on its own domain.
An address is never guessed or put together from a name.

Every person found with a title in TITLES is kept in lead_contacts (save_contacts), each with
a confidence:
  high    their address is published on the site's own domain in the same block of the page
          as their name and title (a team member's card)
  medium  an address on the site's own domain that plainly is theirs (first and last name, or
          initial and last name: jane.wanjiru@, jwanjiru@), found elsewhere on the site, in a
          company PDF (profile, brochure) or given for them in JSON-LD Person data
  low     their name and title, but no address of theirs published: kept, never emailed
The general inboxes on the site's own domain (GENERAL_INBOXES) are kept too.

The first email goes to the highest-ranked person with high or medium confidence (TITLES order,
then high before medium), otherwise to the best general inbox, otherwise to the address the
lead already has. A named person is greeted by first name, anyone else "Hello,".

Only leads never emailed are changed, so a thread never moves to a new address. The address
the lead had before is kept in source_email.

  python main.py contacts [--recent N]   how many leads got a named contact, by title and confidence
"""
import io
import json
import re
import time
import unicodedata
from datetime import date
from urllib.parse import unquote, urljoin, urlparse

import db
import ranking
import scraper

ROLE_ORDER = scraper.ROLE_ORDER  # scraper.pick_best's order of role inboxes
# Decision-makers, best first: (title_rank, group, pattern). Founder, CEO and MD share rank 1.
TITLES = (
    (1, "Founder/CEO/Managing Director",
     r"\b(?:co-?)?founder\b|\bCEO\b|chief executive(?: officer)?|managing director|\bMD\b"),
    (2, "CTO/Head of Technology",
     r"\bCTO\b|chief technology officer|chief technical officer|technology director|technical director"
     r"|\b(?:head|director|vp|vice president) of (?:technology|engineering|tech)\b"),
    (3, "Product Manager/Head of Product",
     r"\bproduct (?:manager|lead|director|owner)\b|\b(?:head|director|vp|vice president) of product\b"
     r"|chief product officer|\bCPO\b"),
    (4, "Operations/Commercial Lead",
     r"\b(?:head|director|vp|vice president) of (?:operations|commercial)\b|chief (?:operating|commercial) officer"
     r"|\bCOO\b|\b(?:operations|commercial) (?:director|manager|lead)\b"),
)
TITLE_RE = re.compile("|".join(f"(?:{p})" for _, _, p in TITLES), re.I)
# General inboxes, best first; they rank after every person (title_rank 5 onwards)
GENERAL_INBOXES = ("sales", "partnerships", "business", "hello", "contact", "info")
GENERAL_RANK = {name: len(TITLES) + 1 + i for i, name in enumerate(GENERAL_INBOXES)}
# Other inboxes that are nobody's own address (support@, enquiries@...)
GENERAL_WORDS = ("info", "enquir", "inquir", "office", "admin", "support", "help", "contact", "hello",
                 "mail", "customer", "care", "team", "service", "sales", "partnerships", "business")
CONFIDENCES = ("high", "medium", "low")
# Pages that may name the team: the address or the link text says so
PEOPLE_PAGE = re.compile(r"team|about|leader|management|people|who-we-are|founder|board|our-story", re.I)
CONTACT_PAGE = re.compile(r"contact", re.I)
# Never read for people: posts and listings ("/blog/property-management-tips" is not a team page)
SKIP_PAGE = re.compile(r"/(?:blog|news|posts?|articles?|category|categories|tags?|insights|press|media|events"
                       r"|careers|jobs|vacancies|products?|shop|store|faqs?|help|docs|wp-content|feed)(?:/|$)", re.I)
PATHS = ("", "/contact", "/contact-us", "/about", "/about-us", "/team", "/our-team", "/leadership",
         "/management", "/people", "/who-we-are", "/about-us/team", "/about/team")
MAX_PAGES = 15        # pages and PDFs read per site
MAX_HTML = 12         # of which web pages at most, leaving room for PDFs
MAX_TRIES = 30        # addresses asked for per site: a missing page costs a try, not a page
MAX_SITEMAPS = 3      # sitemaps read from a sitemap index
MAX_PDF_BYTES = 8_000_000
PDF_PAGES = 20        # pages of a PDF read
PDF_HINT = re.compile(r"profile|brochure|company|about|team|leadership|management|board", re.I)
MAX_BLOCK = 600   # characters: a team member's card, not the whole page
HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "eng", "hon", "cpa", "rev"}
NOT_NAME_WORDS = {"our", "the", "team", "meet", "about", "chief", "officer", "head", "director", "board",
                  "founder", "co-founder", "managing", "executive", "operations", "product", "marketing",
                  "finance", "partnerships", "limited", "ltd", "kenya", "nairobi", "contact", "us", "email",
                  "phone", "read", "more", "view", "profile", "linkedin", "management", "leadership", "and",
                  "of", "vice", "president", "technology", "company", "group", "plc", "africa", "home",
                  "ceo", "cto", "coo", "cfo", "cmo", "cpo", "md", "vp", "manager", "commercial", "lead"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NAME_RE = re.compile(r"\b([A-Z][a-zA-Z'’-]+\.?(?:\s+[A-Z][a-zA-Z'’-]+\.?){1,3})\b")


def fetch(url):
    """One page as parsed HTML, or None (targeting.browser_fetch: as a normal browser)."""
    import targeting
    return targeting.browser_fetch(url)


def fetch_raw(url):
    """A file's bytes (a sitemap, a PDF), or None. Never raises: a site that fails is skipped."""
    try:
        r = scraper.get(url, timeout=20)
    except Exception:
        return None
    if r is None or len(r.content) > MAX_PDF_BYTES:
        return None
    return r.content


def _site(website):
    return website if website.startswith("http") else "https://" + website


def _decode_cf(hexed):
    """An address hidden by Cloudflare's email protection, as the page shows it to readers."""
    try:
        key = int(hexed[:2], 16)
        return "".join(chr(int(hexed[i:i + 2], 16) ^ key) for i in range(2, len(hexed), 2))
    except ValueError:
        return ""


def page_emails(soup):
    """Every address on a page: mailto links, Cloudflare-protected ones and plain text."""
    found = [a["href"][7:].split("?")[0] for a in soup.select('a[href^="mailto:"]')]
    found += [_decode_cf(el["data-cfemail"]) for el in soup.select("[data-cfemail]")]
    found += EMAIL_RE.findall(soup.get_text(" "))
    out = []
    for e in found:  # mailto links can carry spaces, %20 or a trailing dot
        e = unquote(e).strip().strip(".").strip().lower()
        if EMAIL_RE.fullmatch(e) and e not in out:
            out.append(e)
    return out


def _host(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def _links(soup, base, host):
    """Links on a page to the same site, in page order: (url, link text). A page's address
    is taken without its query (?intent=demo) or #part, so it is read once."""
    out = []
    for a in soup.select("a[href]"):
        url = urljoin(base, a["href"]).split("#")[0]
        if not url.lower().split("?")[0].endswith(".pdf"):
            url = url.split("?")[0]
        if url.startswith("http") and _host(url) == host:
            out.append((url, a.get_text(" ")))
    return out


def _people_or_contact_page(url, text=""):
    """Whether a page sounds like a team, about, leadership or contact page, from its address
    (its last part) or its link text, and is not a post or a listing (SKIP_PAGE)."""
    path = urlparse(url).path
    if SKIP_PAGE.search(path):
        return False
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return bool(PEOPLE_PAGE.search(last) or CONTACT_PAGE.search(last) or (text and PEOPLE_PAGE.search(text)))


def sitemap_urls(base, host):
    """Page and PDF addresses on the site's own domain from its sitemap (and up to
    MAX_SITEMAPS sitemaps a sitemap index lists). [] if it has none."""
    data = fetch_raw(urljoin(base, "/sitemap.xml"))
    if not data:
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", data.decode("utf-8", "ignore"))
    nested = [u for u in locs if u.lower().endswith(".xml")][:MAX_SITEMAPS]
    for url in nested:
        more = fetch_raw(url)
        if more:
            locs += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", more.decode("utf-8", "ignore"))
    return [u for u in dict.fromkeys(locs) if not u.lower().endswith(".xml") and _host(u) == host]


def pdf_text(data):
    """The text of a PDF's first PDF_PAGES pages, or "" if it can't be read."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages[:PDF_PAGES])
    except Exception:
        return ""


def crawl(website, pdfs=True):
    """{"pages": [(url, parsed page)], "pdfs": [(url, text)]} from the lead's own site, at most
    MAX_PAGES in all: the homepage, then the pages it, the other pages and the sitemap link to
    that sound like a team, about, leadership or contact page, then the usual addresses of
    those, then PDFs on the site's own domain (a company profile or brochure first).
    Nothing if the homepage can't be read."""
    base = _site(website)
    host = _host(base)
    home = fetch(base)
    if home is None and base.startswith("https://"):
        # many small sites have a broken certificate but load over plain http
        base = "http://" + base.removeprefix("https://")
        home = fetch(base)
    if home is None:
        return {"pages": [], "pdfs": []}
    pages, seen, tries = [(base, home)], {base.rstrip("/")}, 1
    texts = {" ".join(home.get_text(" ").split())}  # some sites answer every address with the same page
    linked, pdf_links = [], []

    def note_links(soup, url):
        for link, text in _links(soup, url, host):
            if link.lower().split("?")[0].endswith(".pdf"):
                pdf_links.append(link)
            elif _people_or_contact_page(link, text):
                linked.append(link)

    note_links(home, base)
    mapped = sitemap_urls(base, host)
    tries += 1
    pdf_links += [u for u in mapped if u.lower().split("?")[0].endswith(".pdf")]
    mapped = [u for u in mapped if not u.lower().split("?")[0].endswith(".pdf") and _people_or_contact_page(u)]
    guesses = [urljoin(base, p) for p in PATHS[1:]]
    while len(pages) < MAX_HTML and tries < MAX_TRIES:
        url = next((u for u in linked + mapped + guesses if u.rstrip("/") not in seen), None)
        if url is None:
            break
        seen.add(url.rstrip("/"))
        tries += 1
        soup = fetch(url)
        text = " ".join(soup.get_text(" ").split()) if soup is not None else None
        if soup is not None and text not in texts:
            texts.add(text)
            pages.append((url, soup))
            note_links(soup, url)
        time.sleep(0.5)  # be polite
    found_pdfs = []
    if pdfs:
        ordered = sorted(dict.fromkeys(pdf_links), key=lambda u: not PDF_HINT.search(urlparse(u).path))
        for url in ordered:
            if len(pages) + len(found_pdfs) >= MAX_PAGES or tries >= MAX_TRIES:
                break
            tries += 1
            data = fetch_raw(url)
            text = pdf_text(data) if data and data[:5] == b"%PDF-" else ""
            if text.strip():
                found_pdfs.append((url, text))
    return {"pages": pages, "pdfs": found_pdfs}


def read_site(website):
    """[(page address, parsed page)]: the site's pages crawl reads, without its PDFs.
    [] if the homepage can't be read."""
    return crawl(website, pdfs=False)["pages"]


def _ascii(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def is_role(local):
    """Whether an address's local part is an inbox, not a person (sales, info, ceo...)."""
    import targeting
    return local.startswith(targeting.ROLE_WORDS) or local in ROLE_ORDER or local in GENERAL_INBOXES


def email_type(email, name=None):
    """"named" (plainly this person's), "general" (an inbox anyone may read: sales@, info@,
    support@...), "role" (a post's own inbox: ceo@, md@, operations@...) or None (another
    person's address)."""
    local = email.split("@")[0].lower()
    if name and belongs(email, name, strict=False):
        return "named"
    if local in GENERAL_INBOXES or local.startswith(GENERAL_WORDS):
        return "general"
    return "role" if is_role(local) else None


def name_words(name):
    words = [w.strip(".") for w in name.split()]
    return [w for w in words if w.lower() not in HONORIFICS]


def first_name(name):
    return name_words(name)[0]


def looks_like_name(text, company=""):
    words = name_words(text)
    if not 2 <= len(words) <= 4 or any(ch.isdigit() for ch in text) or TITLE_RE.search(text):
        return False
    company_words = {w.lower() for w in re.findall(r"[A-Za-z]+", company or "")}
    return all(w[:1].isupper() and w.lower() not in NOT_NAME_WORDS and w.lower() not in company_words
               for w in words)


def belongs(email, name, strict):
    """Whether this address is plainly the named person's, from its local part. strict (an
    address found away from their name on the page): first and last name, or an initial and
    the last name, must both be in it. Otherwise their first or last name is enough."""
    local = re.sub(r"[^a-z]", "", _ascii(email.split("@")[0]))
    words = [re.sub(r"[^a-z]", "", _ascii(w)) for w in name_words(name)]
    words = [w for w in words if w]
    if len(words) < 2 or not local:
        return False
    first, last = words[0], words[-1]
    full = (first in local and last in local) or local in (first[0] + last, last + first[0], first + last[0])
    if strict:
        return full
    return full or local == first or (len(last) >= 3 and local == last)


def _blocks(el):
    """el and its ancestors, smallest first, while they are no bigger than one person's card."""
    while el is not None and getattr(el, "name", None) not in (None, "[document]", "body", "html"):
        text = " ".join(el.get_text(" ").split())
        if len(text) > MAX_BLOCK:
            return
        yield el, text
        el = el.parent


def people(url, soup, company=""):
    """[(name, title, page, addresses in their block)] for each decision-maker named on a page."""
    out = []
    for el in soup.find_all(string=TITLE_RE):
        tag = el.parent
        own = " ".join(el.split())
        if len(own) > 120:  # a title is a short line, not a sentence in a paragraph
            continue
        title = own if len(own) <= 60 else TITLE_RE.search(own).group(0)
        for block, text in _blocks(tag):
            names = [n for n in (" ".join(t.get_text(" ").split()) for t in
                                 block.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "strong", "b", "p", "span", "a", "div"]))
                     if looks_like_name(n, company)]
            names += [m for m in NAME_RE.findall(own.replace(TITLE_RE.search(own).group(0), " "))
                      if looks_like_name(m, company)]
            if names:
                name = names[0]
                title = title.replace(name, "").strip(" ,–—-|:")
                out.append((name, title or TITLE_RE.search(own).group(0), url, page_emails(block)))
                break
    return out


def pdf_people(url, text, company=""):
    """[(name, title, pdf address)] for each decision-maker a PDF names: a title on a short
    line, with the name on the same line or the line before or after it."""
    out = []
    lines = [" ".join(line.split()) for line in text.splitlines()]
    for i, line in enumerate(lines):
        m = TITLE_RE.search(line)
        if not m or len(line) > 120:
            continue
        rest = (line[:m.start()] + " " + line[m.end():]).strip(" ,–—-|:")
        names = [n for n in NAME_RE.findall(rest) if looks_like_name(n, company)]
        names += [n for n in (lines[i - 1] if i else "", lines[i + 1] if i + 1 < len(lines) else "")
                  if looks_like_name(n, company)]
        if names:
            out.append((names[0], m.group(0), url))
    return out


# JSON-LD keys whose values are people, and the title each implies ("" = their own jobTitle)
PEOPLE_KEYS = {"founder": "Founder", "founders": "Founder", "employee": "", "employees": "", "member": ""}


def ld_people(url, soup):
    """[(name, title, page, email or None)] for each person in the page's JSON-LD: Person
    objects anywhere, and an organisation's founder(s), employees and members."""
    out = []

    def walk(obj, slot=None):
        if isinstance(obj, list):
            for item in obj:
                walk(item, slot)
            return
        if not isinstance(obj, dict):
            return
        kinds = obj.get("@type") if isinstance(obj.get("@type"), list) else [obj.get("@type")]
        if obj.get("name") and ("Person" in kinds or (slot is not None and not any(kinds))):
            email = str(obj.get("email") or "").removeprefix("mailto:").strip().lower() or None
            out.append((" ".join(str(obj["name"]).split()), str(obj.get("jobTitle") or slot or ""), url, email))
        for key, value in obj.items():
            walk(value, PEOPLE_KEYS.get(key))

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text() or ""))
        except ValueError:
            continue
    return out


def title_rank(title):
    """1-4 for a title in TITLES (best first), None for any other."""
    return next((rank for rank, _, p in TITLES if re.search(p, title or "", re.I)), None)


def title_group(rank):
    return next((group for r, group, _ in TITLES if r == rank), "other")


def find(website, company=""):
    """What the site says about who to write to:
    {"emails": [addresses on its own domain, as found],
     "role": the best general inbox (GENERAL_INBOXES order) or None,
     "contact": {"name", "title", "email", "url"} of the person to write to, or None,
     "people": every person found: {"name", "title", "title_rank", "email", "email_type",
               "url", "confidence"}, best first,
     "inboxes": [(general inbox, page it is on)] in GENERAL_INBOXES order}."""
    domain = ranking.urlparse_domain(website)
    site = crawl(website)
    own = lambda e: e.endswith("@" + domain) and not scraper.placeholder(e)
    emails, where = [], {}
    for url, soup in site["pages"]:
        for e in page_emails(soup):
            if own(e) and e not in emails:
                emails.append(e)
                where[e] = url
    for url, text in site["pdfs"]:
        for e in EMAIL_RE.findall(text):
            e = e.lower().strip(".")
            if own(e) and e not in emails:
                emails.append(e)
                where[e] = url

    def plainly_theirs(name, extra=()):
        return next((e for e in list(extra) + emails if own(e) and belongs(e, name, strict=True)), None)

    found = []
    home = _site(website).rstrip("/")
    for url, soup in site["pages"]:
        if url.rstrip("/") != home and PEOPLE_PAGE.search(
                urlparse(url).path + " " + (soup.title.get_text(" ") if soup.title else "")):
            for name, title, page, near in people(url, soup, company):
                near = [e for e in near if own(e) and email_type(e, name) in ("named", "role")]
                if near:
                    found.append((name, title, page, near[0], "high"))
                else:
                    found.append((name, title, page, plainly_theirs(name), None))
        for name, title, page, email in ld_people(url, soup):
            if title_rank(title) and looks_like_name(name, company):
                found.append((name, title, page, plainly_theirs(name, [email] if email else []), None))
    for url, text in site["pdfs"]:
        for name, title, page in pdf_people(url, text, company):
            found.append((name, title, page, plainly_theirs(name), None))

    best = {}
    for order, (name, title, page, email, confidence) in enumerate(found):
        confidence = confidence or ("medium" if email else "low")
        rank = title_rank(title)
        if not rank:
            continue
        person = {"name": " ".join(name.split()), "title": title, "title_rank": rank,
                  "email": email if confidence != "low" else None,
                  "email_type": email_type(email, name) if email and confidence != "low" else None,
                  "url": page, "confidence": confidence, "order": order}
        key = " ".join(w.lower() for w in name_words(name))
        old = best.get(key)
        if old is None or (CONFIDENCES.index(confidence), rank) < (CONFIDENCES.index(old["confidence"]), old["title_rank"]):
            best[key] = person
    ranked = sorted(best.values(), key=lambda p: (p["title_rank"], CONFIDENCES.index(p["confidence"]), p["order"]))
    for p in ranked:
        del p["order"]
    to = next((p for p in ranked if p["confidence"] in ("high", "medium")), None)
    inboxes = [(f"{g}@{domain}", where[f"{g}@{domain}"]) for g in GENERAL_INBOXES if f"{g}@{domain}" in where]
    return {"read": bool(site["pages"]), "emails": emails, "where": where, "role": inboxes[0][0] if inboxes else None,
            "contact": {k: to[k] for k in ("name", "title", "email", "url")} if to else None,
            "people": ranked, "inboxes": inboxes}


def choose(lead, result):
    """(address to use, why) for a lead, from find()'s result: the best person with high or
    medium confidence, else the best general inbox, else the address it has."""
    if result["contact"]:
        return result["contact"]["email"], "named contact"
    if result["role"]:
        return result["role"], "general inbox"
    return lead["email"], "kept (no named contact or general inbox on the site)"


def save_contacts(c, lead_id, result):
    """Keep every person and general inbox find() found for the lead, in place of any kept before."""
    c.execute("DELETE FROM lead_contacts WHERE lead_id=?", (lead_id,))
    rows = [(p["name"], p["title"], p["title_rank"], p["email"], p["email_type"], p["url"], p["confidence"])
            for p in result.get("people", [])]
    rows += [(None, None, GENERAL_RANK[email.split("@")[0]], email, "general", page, "high")
             for email, page in result.get("inboxes", [])]
    for row in rows:
        c.execute("INSERT INTO lead_contacts (lead_id, name, title, title_rank, email, email_type, source_url,"
                  " confidence) VALUES (?,?,?,?,?,?,?,?)", (lead_id, *row))


def run(leads, again=False, outcomes=None):
    """Read the websites of leads never emailed and not read yet: save the named contact, if
    any, and switch the lead to the address choose() picks. Returns how many sites were read.
    A site that can't be read leaves the lead as it is, and it isn't tried again.
    again=True (reread): read them even if they were read before; a site that can't be read
    then leaves the lead exactly as it was, contact and all, to be tried at the next reread.
    A lead from an imported list (from_list) not settled yet is read whatever else, and its
    address settled by settle_listed; `outcomes` (a Counter), if given, counts what happened."""
    read = 0
    emailed = db.emailed_lead_ids()
    for lead in leads:
        lead = dict(lead)
        listed = needs_list_check(lead)
        if lead["id"] in emailed or (lead.get("contact_checked_at") and not again and not listed):
            continue
        if listed and not lead.get("website"):
            outcome = settle_listed(lead, None)
            if outcomes is not None:
                outcomes[outcome] += 1
            continue
        if not lead.get("website"):
            continue
        read += 1
        try:
            result = find(lead["website"], lead.get("company") or "")
        except Exception as e:  # one broken site never stops the day's sending
            print(f"  {lead['company']}: couldn't read {lead['website']} for contacts ({e})")
            result = {"read": False, "emails": [], "role": None, "contact": None, "people": [], "inboxes": []}
        if listed:
            outcome = settle_listed(lead, result)
            if outcomes is not None:
                outcomes[outcome] += 1
            continue
        if again and not result.get("read", True):
            print(f"  {lead['company']}: couldn't read {lead['website']}; left as it was")
            continue
        new, why = choose(lead, result)
        contact = result["contact"]
        with db.conn() as c:
            save_contacts(c, lead["id"], result)
            if result.get("read", True):  # read under today's rules: a reread doesn't do it again
                db.log_event(c, lead["id"], "contacts_read", people=len(result.get("people", [])),
                             inboxes=len(result.get("inboxes", [])))
            if new != lead["email"] and c.execute(
                    "SELECT 1 FROM leads WHERE email=? AND id<>? UNION SELECT 1 FROM suppression WHERE email=?",
                    (new, lead["id"], new)).fetchone():
                print(f"  {lead['company']}: kept {lead['email']} ({new} is another lead's or opted out)")
                new, contact = lead["email"], contact if contact and contact["email"] == lead["email"] else None
            c.execute("UPDATE leads SET contact_name=?, contact_title=?, contact_email=?, contact_url=?,"
                      " contact_checked_at=CURRENT_TIMESTAMP WHERE id=?",
                      (contact and contact["name"], contact and contact["title"], contact and contact["email"],
                       contact and contact["url"], lead["id"]))
            if new != lead["email"]:
                c.execute("UPDATE leads SET source_email=COALESCE(source_email, email), email=? WHERE id=?",
                          (new, lead["id"]))
                db.log_event(c, lead["id"], "email_changed", old=lead["email"], new=new, why=why, by="contacts")
                print(f"  {lead['company']}: {lead['email']} -> {new} ({why})")
    return read


def to_reread():
    """Waiting leads (verified, never emailed: ranking.waiting, in sending order) with a website
    that haven't been read under today's rules (no contacts_read event), so their contacts
    are found the way find() finds them now."""
    import ranking as rank
    with db.conn() as c:
        done = {r[0] for r in c.execute("SELECT DISTINCT lead_id FROM lead_events WHERE event_type='contacts_read'")}
    return [l for l in rank.waiting() if l["website"] and l["id"] not in done]


def reread(limit=None):
    """Read again the websites of the waiting leads to_reread lists (the first `limit` of
    them), saving everyone found and switching each to the address choose() picks. Can be
    stopped and run again: leads already read are skipped. Returns how many were read."""
    todo = to_reread()
    total = len(todo)
    todo = todo[:limit] if limit else todo
    print(f"{total} waiting leads to read again" + (f"; reading {len(todo)}" if limit and limit < total else ""))
    done = 0
    for i, lead in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {lead['company']} ({lead['website']})", flush=True)
        done += run([lead], again=True)
    left = len(to_reread())
    print(f"Read {done}. {left} still to read" + (" (sites that couldn't be read are tried again next time)." if left else "."))
    return done


# --- Is an old lead's address still on its website? ---
# A lead found more than RECHECK_AFTER_DAYS ago whose address came from its own website has the
# site read again before its first email (prepare.py does it; sender.prepared waits for it).
# Still shown: the check counts for RECHECK_VALID_DAYS. Gone: the lead is excluded, never
# emailed. A site that can't be read leaves the lead waiting, to be read again next time.
RECHECK_AFTER_DAYS = 60
RECHECK_VALID_DAYS = 7


def _day(text):
    try:
        return date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


def address_from_site(lead):
    """Whether the lead's address was read off its website: found by discovery (source
    'search'), or put in place of the address it came with (source_email). An address from a
    regulator's directory or a list was never said to be on the site."""
    lead = dict(lead)
    if lead.get("address_source"):  # settled for an imported lead (settle_listed)
        return lead["address_source"] == "site"
    return lead.get("source") == "search" or bool(lead.get("source_email"))


def needs_address_check(lead, today=None):
    """Whether the lead's website must be read again before its first email."""
    lead = dict(lead)
    today = today or date.today()
    if not lead.get("website") or not lead.get("email") or not address_from_site(lead):
        return False
    found = _day(lead.get("discovered_at") or lead.get("created_at"))
    if not found or (today - found).days <= RECHECK_AFTER_DAYS:
        return False
    checked = _day(lead.get("address_checked_at"))
    return not checked or (today - checked).days >= RECHECK_VALID_DAYS


def published_page(lead):
    """(readable, page): whether the lead's website could be read at all, and the page that
    still shows its address (any way scraper.page_emails finds one), or None."""
    lead = dict(lead)
    pages = read_site(lead["website"])
    if not pages:
        return False, None
    urls = {u.rstrip("/") for u, _ in pages}
    for extra in (lead.get("contact_url"), lead.get("address_page")):
        if extra and extra.rstrip("/") not in urls and urlparse(extra).netloc.removeprefix("www.") == \
                urlparse(_site(lead["website"])).netloc.removeprefix("www."):
            soup = fetch(extra)
            if soup is not None:
                pages.append((extra, soup))
    email = lead["email"].lower().strip()
    for url, soup in pages:
        if email in {e.lower().strip().strip(".") for e in scraper.page_emails(soup)}:
            return True, url
    return True, None


def recheck_addresses(leads, today=None):
    """Read again the website of each lead that needs_address_check. Returns
    {"published": n, "gone": n, "unreadable": n}."""
    today = today or date.today()
    counts = {"published": 0, "gone": 0, "unreadable": 0}
    for lead in leads:
        lead = dict(lead)
        if not needs_address_check(lead, today):
            continue
        try:
            readable, page = published_page(lead)
        except Exception as e:  # one broken site never stops the others
            print(f"  {lead['company']}: couldn't read {lead['website']} to recheck its address ({e})")
            readable, page = False, None
        if not readable:
            counts["unreadable"] += 1
            continue
        if page:
            with db.conn() as c:
                c.execute("UPDATE leads SET address_checked_at=CURRENT_TIMESTAMP, address_page=? WHERE id=?",
                          (page, lead["id"]))
                db.log_event(c, lead["id"], "address_rechecked", page=page)
            counts["published"] += 1
        else:
            db.exclude(lead["id"], f"its address is no longer published on {lead['website']} "
                                   f"(checked {today.isoformat()})", by="prepare")
            print(f"  {lead['company']}: {lead['email']} is no longer on {lead['website']}; not emailed")
            counts["gone"] += 1
    return counts


# --- python main.py contacts [--recent N] ---

def summary(recent=None):
    """How many leads whose website was read got a named contact, by title and confidence.
    recent: only the N leads read most recently. Test leads (source 'manual') are left out."""
    with db.conn() as c:
        leads = c.execute(f"SELECT id, email, contact_name, contact_email FROM leads"
                          f" WHERE contact_checked_at IS NOT NULL AND {db.real()}"
                          " ORDER BY contact_checked_at DESC, id DESC" + (" LIMIT ?" if recent else ""),
                          (recent,) if recent else ()).fetchall()
        ids = {l["id"] for l in leads}
        found = [r for r in c.execute("SELECT lead_id, title_rank, confidence FROM lead_contacts"
                                      " WHERE name IS NOT NULL") if r["lead_id"] in ids]
    n = len(leads)
    lines = [f"Leads whose website was read for contacts: {n}" + (f" (the {recent} read most recently)" if recent else "")]
    if not n:
        return lines
    pct = lambda k: f"{k} ({100 * k / n:.0f}%)"
    named = {r["lead_id"] for r in found if r["confidence"] in ("high", "medium")}
    only_low = {r["lead_id"] for r in found} - named
    to_person = sum(1 for l in leads if l["contact_name"] and l["contact_email"] == l["email"])
    to_general = sum(1 for l in leads if not (l["contact_name"] and l["contact_email"] == l["email"])
                     and email_type(l["email"] or "") == "general")
    lines += [f"With a named contact to write to (high or medium confidence): {pct(len(named))}",
              f"Named on the site but no address of theirs published (low only): {pct(len(only_low))}",
              f"First email goes to: a named person {to_person}, a general inbox {to_general}, "
              f"the address it came with {n - to_person - to_general}",
              "", f"{'BY TITLE':32}  {'LEADS':>5}  {'HIGH':>4}  {'MEDIUM':>6}  {'LOW':>4}"]
    for rank, group, _ in TITLES:
        cell = lambda conf: len({r["lead_id"] for r in found if r["title_rank"] == rank
                                 and (conf is None or r["confidence"] == conf)})
        lines.append(f"  {group:30}  {cell(None):>5}  {cell('high'):>4}  {cell('medium'):>6}  {cell('low'):>4}")
    lines.append("(a lead counts once in each column it has someone for)")
    return lines


def report(recent=None):
    print("\n".join(summary(recent)))


# --- Leads from an imported list (CBK directories, a CSV): an address published today ---
# The list's address is not trusted. Before the first email the lender's own website is read
# and an address it publishes today is used: a named person only at high confidence, otherwise
# the best general inbox (ROLE_ORDER first, then any other inbox anyone may read). If the site
# publishes none, the list's address is kept only if it is on the site's own domain and its mail
# server answers; otherwise the lead is excluded as "no current address". A site that can't be
# read is tried again at MAX_LIST_TRIES checks before the list's address is judged on its own;
# a DNS lookup that can't say (a timeout) is tried again too. Nothing is ever guessed.
MAX_LIST_TRIES = 3
NO_CURRENT_ADDRESS = "no current address"


def from_list(lead):
    """Whether the lead's address came from an imported list (a regulator's directory, a CSV)."""
    lead = dict(lead)
    return bool(lead.get("imported_at")) or lead.get("source") == "csv"


def needs_list_check(lead):
    """Whether an imported lead's address hasn't been settled against its website yet."""
    lead = dict(lead)
    return from_list(lead) and not lead.get("address_source")


def list_address(lead):
    """The address the list gave (kept in source_email once an address replaced it)."""
    lead = dict(lead)
    return (lead.get("source_email") or lead.get("email") or "").lower().strip()


def site_candidates(result):
    """[(address, why, page, person or None)] the site publishes today, best first: people at
    high confidence (title order), then ROLE_ORDER inboxes, then any other inbox (not a
    person's own address)."""
    out = [(p["email"], f"named contact ({p['title']})", p["url"], p)
           for p in result.get("people", []) if p["confidence"] == "high" and p["email"]]
    emails = result.get("emails", [])
    pages = {**dict(result.get("inboxes", [])), **result.get("where", {})}
    for role in ROLE_ORDER:
        out += [(e, f"{role}@ published on the site", pages.get(e), None) for e in emails if e.split("@")[0] == role]
    # then any other inbox it publishes (enquiries@, loans@...): never a person's own address
    out += [(e, "an inbox published on the site", pages.get(e), None) for e in sorted(emails)
            if e.split("@")[0] not in ROLE_ORDER and email_type(e) in ("general", "role")]
    seen, unique = set(), []
    for c in out:
        if c[0] not in seen:
            seen.add(c[0])
            unique.append(c)
    return unique


def settle_listed(lead, result):
    """Settle an imported lead's address from find()'s result (None: it has no website).
    Returns "changed" (now an address the site publishes, or back to the list's), "kept"
    (the same address, found on the site or kept by the list rule), "skipped" (no current
    address: excluded, never emailed) or "waiting" (site or DNS didn't answer; tried again)."""
    import verify
    lead = dict(lead)
    lead_id, before = lead["id"], (lead.get("email") or "").lower()
    if result is not None and not result.get("read", True):
        tries = (lead.get("address_tries") or 0) + 1
        with db.conn() as c:
            c.execute("UPDATE leads SET address_tries=? WHERE id=?", (tries, lead_id))
        if tries < MAX_LIST_TRIES:
            return "waiting"
    if result is not None and result.get("read", True):
        with db.conn() as c:
            save_contacts(c, lead_id, result)
        for email, why, page, person in site_candidates(result):
            with db.conn() as c:
                taken = c.execute("SELECT 1 FROM leads WHERE email=? AND id<>? UNION SELECT 1 FROM suppression"
                                  " WHERE email=?", (email, lead_id, email)).fetchone()
            if taken:
                continue
            answers = verify.mail_answers(db.email_domain(email))
            if answers is None:
                return "waiting"
            if answers:
                _use(lead, email, "site", why, page, person)
                return "kept" if email == before else "changed"
    # The site publishes no address that can be used: judge the list's own
    listed, site = list_address(lead), db.email_domain(lead.get("website") or "")
    domain = db.email_domain(listed)
    if not site:
        return _no_address(lead, "no website to check the list's address against")
    if not domain or not (domain == site or domain.endswith("." + site)):
        return _no_address(lead, f"the site publishes no address and the list's ({listed}) isn't on {site}")
    answers = verify.mail_answers(domain)
    if answers is None:
        return "waiting"
    if not answers:
        return _no_address(lead, f"the site publishes no address and {domain}'s mail server doesn't answer")
    _use(lead, listed, "list", "the list's address: on the site's domain, and its mail server answers", None, None)
    return "kept" if listed == before else "changed"


def _use(lead, email, source, why, page, person):
    """Write the settled address on the lead, with the named contact if it is one."""
    with db.conn() as c:
        c.execute("UPDATE leads SET contact_name=?, contact_title=?, contact_email=?, contact_url=?,"
                  " contact_checked_at=CURRENT_TIMESTAMP, address_source=?, address_page=?,"
                  " address_checked_at=CASE WHEN ? = 'site' THEN CURRENT_TIMESTAMP ELSE address_checked_at END"
                  " WHERE id=?",
                  (person and person["name"], person and person["title"], person and person["email"],
                   person and person["url"], source, page, source, lead["id"]))
        if email != (lead.get("email") or "").lower():
            c.execute("UPDATE leads SET source_email=COALESCE(source_email, email), email=? WHERE id=?",
                      (email, lead["id"]))
            db.log_event(c, lead["id"], "email_changed", old=lead.get("email"), new=email, why=why, by="list check")
            print(f"  {lead.get('company')}: {lead.get('email')} -> {email} ({why})")
        db.log_event(c, lead["id"], "list_address_settled", address=email, source=source, why=why)
    if lead.get("status") in ("new", "invalid"):  # its address is a working one now
        db.set_status(lead["id"], "verified", "list check")


def _no_address(lead, why):
    db.exclude(lead["id"], f"{NO_CURRENT_ADDRESS}: {why}", by="list check")
    print(f"  {lead.get('company')}: {NO_CURRENT_ADDRESS} ({why}); not emailed")
    return "skipped"


def listed_to_check():
    """Leads from an imported list never emailed whose address isn't settled yet, best need
    score first: verified, new or invalid (a dead list address may have a live one on the site),
    not suppressed, not left to a CRM user or a campaign."""
    emailed = db.emailed_lead_ids()
    suppressed = db.suppressed_emails()
    with db.conn() as c:
        rows = c.execute("SELECT * FROM leads WHERE status IN ('new', 'verified', 'invalid')"
                         " AND crm_emailed_at IS NULL AND campaign IS NULL AND address_source IS NULL"
                         " ORDER BY COALESCE(score, -1) DESC, id").fetchall()
    return [r for r in rows if from_list(r) and r["id"] not in emailed
            and (r["email"] or "").lower() not in suppressed]


def check_listed(limit=None):
    """Settle the address of every imported lead listed_to_check (the first `limit`), reading
    each one's website. Prints and returns {"changed", "kept", "skipped", "waiting": n}."""
    from collections import Counter
    todo = listed_to_check()
    total = len(todo)
    todo = todo[:limit] if limit else todo
    print(f"{total} leads from imported lists not emailed yet and not checked" +
          (f"; checking {len(todo)}" if limit and limit < total else ""))
    outcomes = Counter()
    for i, lead in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {lead['company']} ({lead['website'] or 'no website'})", flush=True)
        run([lead], outcomes=outcomes)
    counts = {k: outcomes[k] for k in ("changed", "kept", "skipped", "waiting")}
    print(f"Changed {counts['changed']}, kept {counts['kept']}, skipped {counts['skipped']} "
          f"({NO_CURRENT_ADDRESS}), waiting {counts['waiting']} (site or DNS didn't answer; tried again later).")
    return counts
