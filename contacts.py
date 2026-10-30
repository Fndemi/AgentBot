"""Who at a lead the first email goes to, read from the lead's own website before it is sent.

1. A decision-maker: a person named on the site's team, about or leadership page with a
   title in TITLES (CEO, founder, CTO, head of product, operations, partnerships, marketing
   or finance), whose exact address is published on the same site. The address must be in
   the same block of the page as their name and title, or, anywhere else on the site, plainly
   be theirs (first and last name, or initial and last name, e.g. jane.wanjiru@, jwanjiru@).
   Their name, title, address and the page are saved on the lead, and the email greets them
   by first name.
2. Otherwise the first of ROLE_ORDER published on the site's own domain (sales@ first).
3. Otherwise the address the lead already has.

An address is never guessed or put together from a name: only addresses found on the site,
on its own domain, are ever used. Only leads never emailed are changed, so a thread never
moves to a new address. The address the lead had before is kept in source_email.
"""
import re
import time
import unicodedata
from urllib.parse import unquote, urljoin, urlparse

import db
import ranking
import scraper

ROLE_ORDER = ("sales", "partnerships", "business", "tech", "ops", "hello", "contact", "info")
# In this order of preference when a site names more than one
TITLES = (
    ("CEO", r"\bCEO\b|chief executive(?: officer)?|managing director|\bMD\b"),
    ("Founder", r"\b(?:co-?)?founder\b"),
    ("CTO", r"\bCTO\b|chief technology officer|chief technical officer"),
    ("Head of product", r"\b(?:head|director|vp|vice president) of product\b|chief product officer|\bCPO\b|product director"),
    ("Head of operations", r"\b(?:head|director|vp|vice president) of operations\b|chief operating officer|\bCOO\b|operations director"),
    ("Head of partnerships", r"\b(?:head|director|vp|vice president) of (?:partnerships|business development)\b|partnerships director"),
    ("Head of marketing", r"\b(?:head|director|vp|vice president) of marketing\b|chief marketing officer|\bCMO\b|marketing director"),
    ("Head of finance", r"\b(?:head|director|vp|vice president) of finance\b|chief financial officer|\bCFO\b|finance director"),
)
TITLE_RE = re.compile("|".join(f"(?:{p})" for _, p in TITLES), re.I)
# Pages that may name the team: the address or the link text says so
PEOPLE_PAGE = re.compile(r"team|about|leader|management|people|who-we-are|founder|board|our-story", re.I)
PATHS = ("", "/contact", "/contact-us", "/about", "/about-us", "/team", "/our-team", "/leadership",
         "/management", "/people", "/who-we-are", "/about-us/team", "/about/team")
MAX_PAGES = 12
MAX_BLOCK = 600   # characters: a team member's card, not the whole page
HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "prof", "eng", "hon", "cpa", "rev"}
NOT_NAME_WORDS = {"our", "the", "team", "meet", "about", "chief", "officer", "head", "director", "board",
                  "founder", "co-founder", "managing", "executive", "operations", "product", "marketing",
                  "finance", "partnerships", "limited", "ltd", "kenya", "nairobi", "contact", "us", "email",
                  "phone", "read", "more", "view", "profile", "linkedin", "management", "leadership", "and",
                  "of", "vice", "president", "technology", "company", "group", "plc", "africa", "home",
                  "ceo", "cto", "coo", "cfo", "cmo", "cpo", "md", "vp"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NAME_RE = re.compile(r"\b([A-Z][a-zA-Z'’-]+\.?(?:\s+[A-Z][a-zA-Z'’-]+\.?){1,3})\b")


def fetch(url):
    """One page as parsed HTML, or None (targeting.browser_fetch: as a normal browser)."""
    import targeting
    return targeting.browser_fetch(url)


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


def read_site(website):
    """[(page address, parsed page)]: the homepage, the usual contact, about and team pages,
    and pages the homepage links to that sound like them. [] if the homepage can't be read."""
    base = _site(website)
    home = fetch(base)
    if home is None:
        return []
    urls = [urljoin(base, p) for p in PATHS[1:]]
    host = urlparse(base).netloc.removeprefix("www.")
    for a in home.select("a[href]"):
        url = urljoin(base, a["href"]).split("#")[0]
        if urlparse(url).netloc.removeprefix("www.") == host and (
                PEOPLE_PAGE.search(urlparse(url).path) or PEOPLE_PAGE.search(a.get_text(" "))):
            urls.append(url)
    pages, seen = [(base, home)], {base.rstrip("/")}
    for url in urls:
        if len(pages) >= MAX_PAGES:
            break
        if url.rstrip("/") in seen:
            continue
        seen.add(url.rstrip("/"))
        soup = fetch(url)
        if soup is not None:
            pages.append((url, soup))
        time.sleep(0.5)  # be polite
    return pages


def _ascii(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def is_role(local):
    """Whether an address's local part is an inbox, not a person (sales, info, ceo...)."""
    import targeting
    return local.startswith(targeting.ROLE_WORDS) or local in ROLE_ORDER


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


def _rank(title):
    return next((i for i, (_, p) in enumerate(TITLES) if re.search(p, title, re.I)), len(TITLES))


def find(website, company=""):
    """What the site says about who to write to:
    {"emails": [addresses on its own domain], "role": the best role address or None,
     "contact": {"name", "title", "email", "url"} or None}."""
    domain = ranking.urlparse_domain(website)
    pages = read_site(website)
    emails = []
    for _, soup in pages:
        for e in page_emails(soup):
            if e.endswith("@" + domain) and e not in emails:
                emails.append(e)
    role = next((f"{r}@{domain}" for r in ROLE_ORDER if f"{r}@{domain}" in emails), None)
    found = []
    for url, soup in pages:
        if url.rstrip("/") == _site(website).rstrip("/") or not PEOPLE_PAGE.search(
                urlparse(url).path + " " + (soup.title.get_text(" ") if soup.title else "")):
            continue  # names only count on a team, about or leadership page
        for name, title, page, near in people(url, soup, company):
            near = [e for e in near if e.endswith("@" + domain) and not is_role(e.split("@")[0])]
            email = (next((e for e in near if belongs(e, name, strict=False)), None)
                     or next((e for e in emails if not is_role(e.split("@")[0]) and belongs(e, name, strict=True)), None))
            if email:
                found.append({"name": " ".join(name.split()), "title": title, "email": email, "url": page})
    found.sort(key=lambda p: _rank(p["title"]))
    return {"emails": emails, "role": role, "contact": found[0] if found else None}


def choose(lead, result):
    """(address to use, why) for a lead, from find()'s result."""
    if result["contact"]:
        return result["contact"]["email"], "named contact"
    if result["role"]:
        return result["role"], "role address"
    return lead["email"], "kept (no contact or role address on the site)"


def run(leads):
    """Read the websites of leads never emailed and not read yet: save the named contact, if
    any, and switch the lead to the address choose() picks. Returns how many sites were read.
    A site that can't be read leaves the lead as it is, and it isn't tried again."""
    read = 0
    emailed = db.emailed_lead_ids()
    for lead in leads:
        lead = dict(lead)
        if lead.get("contact_checked_at") or lead["id"] in emailed or not lead.get("website"):
            continue
        read += 1
        try:
            result = find(lead["website"], lead.get("company") or "")
        except Exception as e:  # one broken site never stops the day's sending
            print(f"  {lead['company']}: couldn't read {lead['website']} for contacts ({e})")
            result = {"emails": [], "role": None, "contact": None}
        new, why = choose(lead, result)
        contact = result["contact"]
        with db.conn() as c:
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
