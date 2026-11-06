"""Checks that each email's domain can receive mail (MX record lookup).
This catches typos and dead domains. It can't confirm the mailbox itself exists."""
import re
import dns.resolver

import db

SYNTAX = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
_cache = {}


def domain_ok(domain):
    if domain in _cache:
        return _cache[domain]
    try:
        ok = len(dns.resolver.resolve(domain, "MX", lifetime=8)) > 0
    except Exception:
        try:  # some domains receive mail on their A record
            ok = len(dns.resolver.resolve(domain, "A", lifetime=8)) > 0
        except Exception:
            ok = False
    _cache[domain] = ok
    return ok


def run():
    leads = db.leads_by_status("new")
    good = bad = 0
    for lead in leads:
        email = lead["email"]
        if SYNTAX.match(email) and domain_ok(email.split("@")[1]):
            db.set_status(lead["id"], "verified", "verify")
            good += 1
        else:
            db.set_status(lead["id"], "invalid", "verify")
            bad += 1
            print(f"  invalid: {email}")
    print(f"Verified {good}, rejected {bad}.")


def mail_answers(domain):
    """Whether the domain's mail server answers today: True if DNS gives it an MX record (or,
    with none, an A record mail can go to), False if the domain or both records don't exist,
    None if DNS couldn't say (a timeout, no name server answered): try again later."""
    if not domain:
        return False
    try:
        return len(dns.resolver.resolve(domain, "MX", lifetime=8)) > 0
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        pass
    except Exception:
        return None
    try:
        return len(dns.resolver.resolve(domain, "A", lifetime=8)) > 0
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except Exception:
        return None
