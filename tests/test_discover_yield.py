"""More good leads from each search: a niche's searches start at a different query each day and
vary their wording; sites are read as a browser, a refusal tried once more; hidden addresses
(Cloudflare, "[at]", URL-encoded mailto) are found; a site outside .ke counts as Kenyan when it
names a Kenyan town, +254, KES or M-Pesa. Searches and websites are fakes: nothing is fetched."""
from datetime import date

import pytest
import requests
from bs4 import BeautifulSoup

import discover
import scraper

MON, TUE = date(2026, 11, 2), date(2026, 11, 3)


# --- The searches change from day to day ---

def test_each_day_starts_at_a_different_query():
    first = {day: discover.searches("software_vendor", day)[0][1] for day in (MON, TUE)}
    assert first[MON] != first[TUE]
    assert len(discover.searches("software_vendor", MON)) == len(discover.QUERIES["software_vendor"])


def test_a_query_without_a_town_is_worded_differently_each_day():
    days = [date(2026, 11, d) for d in range(2, 8)]
    worded = {discover.vary("fintech startup Nairobi", d) for d in days}
    assert len(worded) >= 5
    assert all(w.startswith("fintech startup Nairobi") for w in worded)
    assert "fintech startup Nairobi Nairobi" not in worded          # never the word it already has


def test_a_query_with_a_town_still_gets_a_town(monkeypatch):
    monkeypatch.setattr(discover, "TOWNS", ["Nakuru"])
    assert discover.vary("SACCO society {town} Kenya contact us", MON) == "SACCO society Nakuru Kenya contact us"


# --- Reading sites ---

class Response:
    def __init__(self, status, content=b"<html></html>"):
        self.status_code, self.content, self.text = status, content, content.decode()


@pytest.fixture
def web(monkeypatch):
    """requests.get answers from .answers in turn; every address asked is in .asked."""
    class Web:
        answers, asked, headers = [], [], []
    monkeypatch.setattr(scraper, "RETRY_PAUSE", 0)

    def get(url, headers, timeout):
        Web.asked.append(url)
        Web.headers.append(headers)
        answer = Web.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(scraper.requests, "get", get)
    return Web


def test_sites_are_read_as_a_browser_and_a_refusal_is_tried_again(web):
    web.answers[:] = [Response(403), Response(200, b"<p>ok</p>")]
    assert scraper.fetch("https://acme.co.ke").get_text() == "ok"
    assert len(web.asked) == 2 and "Chrome" in web.headers[0]["User-Agent"]


@pytest.mark.parametrize("answer", [requests.Timeout("slow"), Response(404)])
def test_a_timeout_or_missing_page_is_not_tried_again(web, answer):
    web.answers[:] = [answer, Response(200)]
    assert scraper.fetch("https://acme.co.ke") is None and len(web.asked) == 1


def test_a_dropped_connection_is_tried_once_more(web):
    web.answers[:] = [requests.ConnectionError("reset"), requests.ConnectionError("reset")]
    assert scraper.fetch("https://acme.co.ke") is None and len(web.asked) == 2


# --- Hidden addresses ---

def cloudflare(address, key=0x5A):
    return (bytes([key]) + bytes(b ^ key for b in address.encode())).hex()


def test_hidden_addresses_are_found():
    page = BeautifulSoup(
        f'<a href="/cdn-cgi/l/email-protection#{cloudflare("info@gara.co.ke")}">[email protected]</a>'
        f'<span class="__cf_email__" data-cfemail="{cloudflare("sales@gara.co.ke")}"></span>'
        '<a href="MAILTO:%20hello@gara.co.ke?subject=Hi">Write</a>'
        "<p>Or write to support [at] gara [dot] co [dot] ke. Visit us at Westlands. ops(at)gara.co.ke</p>",
        "html.parser")
    assert scraper.page_emails(page) == {"info@gara.co.ke", "sales@gara.co.ke", "hello@gara.co.ke",
                                         "support@gara.co.ke", "ops@gara.co.ke"}


def test_broken_cloudflare_codes_are_ignored():
    assert scraper.cf_decode("zz") == "" and scraper.cf_decode("") == ""


# --- The Kenya check ---

@pytest.mark.parametrize("text, kenyan", [
    ("Offices in Westlands, Nairobi", True), ("Call +254 700 000 000", True), ("From KES 500 a month", True),
    ("Pay with M-Pesa", True), ("Serving Kenya since 2019", True), ("Offices in Lagos and Accra", False),
])
def test_kenya_check(monkeypatch, text, kenyan):
    monkeypatch.setattr(scraper, "get", lambda url: Response(200, f"<title>Acme</title><p>{text}</p>".encode()))
    assert discover.look_at("https://acme.com").kenya is kenyan
