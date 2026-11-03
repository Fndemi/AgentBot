"""Finds contact emails on company websites.

Input: a CSV of companies you already have (name, website, industry).
Get these from public directories: Kenya Yellow Pages, SASRA's licensed SACCO list,
Ministry of Education school lists, chamber of commerce member lists, etc.

The scraper visits the homepage plus common contact/about pages and pulls emails.
"""
import csv
import re
import time
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import db

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# Addresses written out to hide them from scrapers: "info [at] acme [dot] co.ke", "info(at)acme.com"
HIDDEN_EMAIL = re.compile(r"\b([a-z0-9._%+-]+)\s*[\[({]\s*at\s*[\])}]\s*([a-z0-9-]+(?:(?:\s*[\[({]\s*dot\s*[\])}]\s*|\.)"
                          r"[a-z0-9-]+)+)", re.I)
DOT = re.compile(r"\s*[\[({]\s*dot\s*[\])}]\s*", re.I)   # a plain "." has no spaces round it
CONTACT_PATHS = ["", "/contact", "/contact-us", "/contacts", "/about", "/about-us"]
ABOUT_PATHS = ["", "/about", "/about-us"]
# Sites are read as a normal browser would: many turn away a bot's user-agent
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
# A refusal or overload that often clears on a second try; a timeout isn't tried again
RETRY_STATUS = {403, 429, 500, 502, 503, 504}
RETRY_PAUSE = 2  # seconds

# Junk that looks like an email but isn't one
BAD_ENDINGS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
BAD_WORDS = ("example.com", "sentry", "wixpress", "domain.com", "yourname", "email.com")
# Prefer generic business inboxes (safer under the Data Protection Act)
PREFERRED = ("info@", "sales@", "admin@", "hello@", "contact@", "enquiries@", "office@")


def clean(emails, site_domain):
    out = set()
    for e in emails:
        e = e.lower().strip(".")
        if not EMAIL_RE.fullmatch(e):  # e.g. a mailto: link with no address in it
            continue
        if e.endswith(BAD_ENDINGS) or any(w in e for w in BAD_WORDS):
            continue
        out.add(e)
    # Keep emails on the company's own domain first; fall back to anything found
    own = {e for e in out if e.split("@")[1].endswith(site_domain)}
    return own or out


def pick_best(emails):
    for p in PREFERRED:
        for e in emails:
            if e.startswith(p):
                return e
    return sorted(emails)[0] if emails else None


def get(url, timeout=10):
    """A page's response (status 200), fetched as a browser would, or None. A refusal or an
    overload (RETRY_STATUS) or a dropped connection is tried once more after RETRY_PAUSE;
    a timeout is not, so a dead site costs one wait."""
    for attempt in (1, 2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
        except requests.Timeout:
            return None
        except requests.RequestException:
            r = None
        if r is not None and r.status_code == 200:
            return r
        if (r is not None and r.status_code not in RETRY_STATUS) or attempt == 2:
            return None
        time.sleep(RETRY_PAUSE)
    return None


def fetch(url):
    """One page as parsed HTML, or None if it can't be fetched."""
    r = get(url)
    return BeautifulSoup(r.text, "html.parser") if r is not None else None


def cf_decode(code):
    """An address hidden by Cloudflare's email protection (data-cfemail, or the part of an
    /cdn-cgi/l/email-protection link after #): hex bytes, each XORed with the first."""
    try:
        data = bytes.fromhex(code.strip())
    except ValueError:
        return ""
    return bytes(b ^ data[0] for b in data[1:]).decode("utf-8", "ignore") if data else ""


def page_emails(soup):
    """Every address a page gives: mailto links (any case, URL-decoded), addresses hidden by
    Cloudflare, ones written out ("info [at] acme [dot] co.ke") and plain ones in the text."""
    found = set()
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            found.add(unquote(href[7:]).split("?")[0].strip())
        elif "/cdn-cgi/l/email-protection#" in href:
            found.add(cf_decode(href.split("#", 1)[1]))
    for el in soup.select("[data-cfemail]"):
        found.add(cf_decode(el["data-cfemail"]))
    text = soup.get_text(" ")
    found.update(EMAIL_RE.findall(text))
    found.update(f"{m.group(1)}@{DOT.sub('.', m.group(2))}" for m in HIDDEN_EMAIL.finditer(text))
    return {e for e in found if e}


def scrape_site(url):
    if not url.startswith("http"):
        url = "https://" + url
    domain = urlparse(url).netloc.replace("www.", "")
    found = set()
    for path in CONTACT_PATHS:
        soup = fetch(urljoin(url, path))
        if soup is None:
            continue
        found.update(page_emails(soup))
        time.sleep(1)  # be polite
        if found and path:
            break
    return clean(found, domain)


_pages = {}  # site -> page texts, so one run doesn't fetch the same site twice
_page_urls = {}  # site -> the address of each of those pages, in the same order
_taglines = {}  # site -> the homepage's title, descriptions and main headings


def site_pages(url):
    """The full readable text of the homepage and the about page, as a list
    (homepage first). Empty if the site can't be fetched."""
    if not url:
        return []
    if not url.startswith("http"):
        url = "https://" + url
    if url in _pages:
        return _pages[url]
    pages, urls = [], []
    for path in ABOUT_PATHS:
        soup = fetch(urljoin(url, path))
        if soup is None:
            continue
        if url not in _taglines:  # the first page that loads is the homepage
            metas = soup.select('meta[name="description"], meta[property="og:description"], '
                                'meta[property="og:title"]')
            _taglines[url] = [t for t in [soup.title.get_text(" ", strip=True) if soup.title else ""]
                              + [m.get("content", "") for m in metas]
                              + [h.get_text(" ", strip=True) for h in soup.select("h1")] if t]
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = " ".join(soup.get_text(" ").split())
        if text and text not in pages:  # some sites serve the homepage for every path
            pages.append(text)
            urls.append(urljoin(url, path))
        time.sleep(1)  # be polite
        if len(pages) == 2:
            break
    _pages[url], _page_urls[url] = pages, urls
    return pages


def site_sources(url):
    """[(page address, readable text)] for the pages site_pages read, homepage first."""
    pages = site_pages(url)
    if url and not url.startswith("http"):
        url = "https://" + url
    return list(zip(_page_urls.get(url, []), pages))


def site_taglines(url):
    """Where a site states its tagline: the homepage's title, descriptions and main headings."""
    site_pages(url)
    if url and not url.startswith("http"):
        url = "https://" + url
    return _taglines.get(url, [])


def site_text(url, limit=4000):
    """Readable text from the homepage and the about page, at most `limit`
    characters in total. Empty if the site can't be fetched."""
    pages = site_pages(url)
    if len(pages) < 2:
        return "".join(pages)[:limit]
    home, about = pages
    home = home[:max(limit // 2, limit - len(about) - 1)]
    return (home + "\n" + about)[:limit]


def run(csv_path):
    added = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for i, row in enumerate(rows, 1):
        name, site, industry = row["company"], row["website"], row.get("industry", "")
        # If the CSV already has an email, use it directly
        if row.get("email"):
            if db.add_lead(name, site, industry, row["email"], "csv"):
                added += 1
            continue
        emails = scrape_site(site)
        best = pick_best(emails)
        if best and db.is_suppressed(best):
            print(f"[{i}/{len(rows)}] {name}: {best} is on the suppression list")
            continue
        print(f"[{i}/{len(rows)}] {name}: {best or 'no email found'}")
        if best and db.add_lead(name, site, industry, best, "csv", source_url=site):
            added += 1
    print(f"Added {added} new leads.")
