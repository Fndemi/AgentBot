"""A/B/C template rotation (templates v4) and the gaps every v4 email has.

A niche in rotation has three first emails, one per technique: templates/<niche>_A.txt,
_B.txt and _C.txt, each with its own follow-up 1 (templates/followup/<niche>_<variant>.txt),
and one follow-up 2 for all three (templates/followup2/<niche>.txt). When a lead's first email
goes out it gets the variant sent least so far in its niche (random on a tie), saved on the
lead and on every sent row, and keeps it for its follow-ups. uses.yaml can switch variants
off for a niche (variants: [A, B]); a lead that already has one keeps it.

Leads emailed before the rotation have no variant: they finish with their niche's old
follow-up 1 (templates/followup/<niche>.txt) plus a meeting request, then the new follow-up 2
(see sender.followup_body).

Every v4 email asks for a 15-minute meeting on two suggested times, {slot1} and {slot2},
worked out when it is built: working days in Nairobi only, 2 to 7 working days ahead, on
different days, picked per lead so a day's batch doesn't all offer the same two times.
"""
import random
from datetime import date, datetime, timedelta, timezone

import config
import db
import personalise

VARIANTS = ("A", "B", "C")
TECHNIQUES = {"A": "Quick question", "B": "Free test", "C": "Niche angle"}
# Niches offered a visit first; the rest are offered a video call first
VISIT_NICHES = ("sacco", "school", "church")
VISIT_OPTIONS = "at your office if you're in or near Nairobi, on a video call or by phone"
CALL_OPTIONS = "on a video call, by phone, or at your office if you're in Nairobi"
BOOKING_LINE = "Or pick any time that suits you here: {url}"
# Settings the templates quote as facts: no email goes out while one of them is empty
REQUIRED = ("OFFER_FREE_SMS", "SETUP_TIME", "PRICE_FROM")

NAIROBI = timezone(timedelta(hours=3), "EAT")  # Kenya has no daylight saving
FIRST_DAY, LAST_DAY = 2, 7  # meeting slots: this many working days after the email, at least and at most
DEFAULT_TIMES = "10am,11:30am,2pm,3:30pm"
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Kenyan public holidays (Public Holidays Act): New Year, Labour Day, Madaraka, Mazingira,
# Mashujaa, Jamhuri, Christmas, Boxing Day. Good Friday and Easter Monday are worked out below.
FIXED_HOLIDAYS = ((1, 1), (5, 1), (6, 1), (10, 10), (10, 20), (12, 12), (12, 25), (12, 26))
# Idd-ul-Fitr is gazetted each year once the moon is seen: these are the expected dates.
# Correct one here if the gazette says otherwise, and add any one-off holiday the government
# declares to EXTRA_HOLIDAYS.
EID_AL_FITR = {2026: date(2026, 3, 20), 2027: date(2027, 3, 10), 2028: date(2028, 2, 27)}
EXTRA_HOLIDAYS = set()


def missing_settings():
    """The settings in REQUIRED that are empty in .env."""
    return [k for k in REQUIRED if not str(getattr(config, k, "") or "").strip()]


def refusal():
    """Why no email may go out, or None."""
    missing = missing_settings()
    if not missing:
        return None
    return (f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} empty in .env. The templates "
            "quote the free test size (OFFER_FREE_SMS), the set-up time (SETUP_TIME) and the starting "
            "price (PRICE_FROM), so nothing is sent until all three are filled in")


# --- Which variant a lead gets ---

def enabled(niche, available):
    """The variants new leads in this niche may get: those with a first email (`available`),
    less any that uses.yaml switches off (variants: [A, B])."""
    wanted = (personalise.USES.get(niche) or {}).get("variants")
    if wanted is None:
        return [v for v in VARIANTS if v in available]
    wanted = {str(v).strip().upper() for v in wanted}
    return [v for v in VARIANTS if v in available and v in wanted]


def counts(niche):
    """How many leads in this niche have had their first email from each variant."""
    with db.conn() as c:
        rows = c.execute("SELECT industry, template_variant, COUNT(*) AS n FROM leads"
                         " WHERE template_variant IS NOT NULL GROUP BY industry, template_variant").fetchall()
    out = {}
    for r in rows:
        if personalise.industry_key(r) == niche:
            out[r["template_variant"]] = out.get(r["template_variant"], 0) + r["n"]
    return out


def pick(niche, available, rng=random):
    """The variant a new lead in this niche gets: of those enabled, the one sent least so far,
    a random one of them on a tie."""
    options = enabled(niche, available)
    if not options:
        raise RuntimeError(f"uses.yaml switches off every variant for {niche} "
                           f"(variants: {personalise.USES[niche].get('variants')})")
    used = counts(niche)
    least = min(used.get(v, 0) for v in options)
    return rng.choice([v for v in options if used.get(v, 0) == least])


# --- Meeting times ---

def today():
    """Today in Nairobi."""
    return datetime.now(NAIROBI).date()


def _easter(year):
    """Easter Sunday (Gregorian), by the anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def holidays(year):
    """Kenya's public holidays in a year. One that falls on a Sunday also gives the next day
    that isn't already a holiday off."""
    easter = _easter(year)
    days = {date(year, m, d) for m, d in FIXED_HOLIDAYS} | {easter - timedelta(days=2), easter + timedelta(days=1)}
    if year in EID_AL_FITR:
        days.add(EID_AL_FITR[year])
    days |= {d for d in EXTRA_HOLIDAYS if d.year == year}
    for d in sorted(x for x in days if x.weekday() == 6):
        off = d + timedelta(days=1)
        while off in days:
            off += timedelta(days=1)
        days.add(off)
    return days


def working_day(d):
    return d.weekday() < 5 and d not in holidays(d.year)


def working_days_after(day, n):
    """The date n working days (Monday to Friday, not a public holiday) after `day`."""
    while n:
        day += timedelta(days=1)
        if working_day(day):
            n -= 1
    return day


def meeting_times():
    """MEETING_TIMES as a list, e.g. ["10am", "11:30am", "2pm", "3:30pm"]."""
    times = [t.strip() for t in (config.MEETING_TIMES or "").split(",") if t.strip()]
    return times or DEFAULT_TIMES.split(",")


def slot_text(day, time):
    return f"{DAY_NAMES[day.weekday()]} {day.day} {MONTHS[day.month - 1]} at {time}"


def slots(lead_id, step=1, on=None):
    """Two meeting times for one email, e.g. ("Tuesday 4 Nov at 10am", "Thursday 6 Nov at 2pm"):
    the earlier first, on different working days FIRST_DAY to LAST_DAY working days after `on`
    (default today in Nairobi). Picked by lead and step (1 = first email), so leads emailed the
    same day are offered different times, and building the same email twice gives the same."""
    on = on or today()
    days = [working_days_after(on, n) for n in range(FIRST_DAY, LAST_DAY + 1)]
    rng = random.Random(f"{lead_id or 0}:{step}")
    first, second = sorted(rng.sample(days, 2))
    times = meeting_times()
    return slot_text(first, rng.choice(times)), slot_text(second, rng.choice(times))


# --- The gaps ---

def meet_options(niche):
    return VISIT_OPTIONS if niche in VISIT_NICHES else CALL_OPTIONS


def booking_line():
    """"Or pick any time that suits you here: <BOOKING_URL>", or "" with no BOOKING_URL."""
    url = (config.BOOKING_URL or "").strip()
    return BOOKING_LINE.format(url=url) if url else ""


def gaps(lead, step=1, on=None):
    """{free_sms} {setup_days} {price_from} {slot1} {slot2} {booking_line} {meet_options}
    for one email to this lead; step is 1 for the first email, n + 1 for follow-up n."""
    lead = dict(lead)
    slot1, slot2 = slots(lead.get("id"), step, on)
    return dict(free_sms=config.OFFER_FREE_SMS, setup_days=config.SETUP_TIME, price_from=config.PRICE_FROM,
                slot1=slot1, slot2=slot2, booking_line=booking_line(),
                meet_options=meet_options(personalise.industry_key(lead)))
