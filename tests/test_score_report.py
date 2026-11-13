"""`python main.py score-report [days]`: every emailed lead by score band, scoring reason, niche,
lead source, variant, named contact vs general inbox and company age, with emailed, replied,
interested, meetings and the reply rate. Groups under 20 emailed are "too few to judge", and no
lead's name or address appears. Nothing is sent: rows are written straight to the database."""
import json
import sys

import pytest

import db
import main
import score_report

YEAR = 2026


@pytest.fixture(autouse=True)
def ready():
    db.init()


def lead(n, *, industry="sacco", score=55, reasons=("+40 start (sacco)",), source="search", variant="A",
         to_contact=0, est_year=None, status="sent", reply=None, meeting_at=None, emailed=True,
         sent_on="2026-11-01", company=None, email=None):
    company = company or f"Company {n}"
    email = email or f"info@company{n}.co.ke"
    with db.conn() as c:
        c.execute("INSERT INTO leads (company, website, industry, email, status, source, score, score_reasons,"
                  " scored_at, template_variant, est_year, meeting_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (company, f"https://company{n}.co.ke", industry, email, status, source, score,
                   json.dumps(list(reasons)) if score is not None else None,
                   "2026-10-01 00:00:00" if score is not None else None, variant, est_year, meeting_at))
        lead_id = c.execute("SELECT id FROM leads WHERE email=?", (email,)).fetchone()[0]
        if emailed:
            c.execute("INSERT INTO sent (lead_id, message_id, template, sent_on, to_contact) VALUES (?,?,?,?,?)",
                      (lead_id, f"<m{n}@x>", "first", sent_on, to_contact))
        if reply:
            c.execute("INSERT INTO replies (lead_id, from_email, label, imap_uid) VALUES (?,?,?,?)",
                      (lead_id, email, reply, f"uid{n}"))
    return lead_id


def section(lines, title):
    """{group name: line} for one section of the report."""
    start = next(i for i, l in enumerate(lines) if l.startswith("BY " + title.upper()))
    out = {}
    for line in lines[start + 1:]:
        if not line.startswith("  ") or line.startswith("  ("):
            break
        name = line[2:].split("  ")[0].strip()
        out[name] = line
    return out


def numbers(line):
    """(emailed, replied, interested, meetings, rate) from a report line."""
    parts = line.replace("too few to judge", "").split()
    return tuple(int(p) for p in parts[-5:-1]) + (parts[-1],)


def test_bands_counts_and_reply_rate():
    for n in range(20):  # 70+: 20 emailed, 5 replied, 2 interested, 1 meeting
        lead(n, score=75, reply="question" if n < 3 else "interested" if n < 5 else None,
             status="meeting" if n == 4 else "sent")
    lead(100, score=10)
    lead(101, score=35, reply="out_of_office")  # not a reply
    lead(102, score=69, reply="bounce")  # nor this
    lead(103, score=None)
    lead(104, score=99, emailed=False)  # never emailed: not counted
    lines = score_report.summary(this_year=YEAR)
    assert lines[0].startswith("Score report: 24 leads")
    bands = section(lines, "score band")
    assert list(bands) == ["70+", "50-69", "30-49", "0-29", "not scored"]
    assert numbers(bands["70+"]) == (20, 5, 2, 1, "25.0%")
    assert "too few to judge" not in bands["70+"]
    assert numbers(bands["50-69"]) == (1, 0, 0, 0, "0.0%")
    assert numbers(bands["30-49"]) == (1, 0, 0, 0, "0.0%")
    assert all("too few to judge" in bands[b] for b in ("50-69", "30-49", "0-29", "not scored"))


def test_meeting_counts_once_and_stays_after_lost():
    lead(1, status="client")
    lead(2, status="lost", meeting_at="2026-11-02 10:00:00")
    lead(3, status="replied", reply="interested")
    bands = section(score_report.summary(this_year=YEAR), "score band")
    assert numbers(bands["50-69"]) == (3, 1, 3, 2, "33.3%")


def test_scoring_reasons_lose_points_and_identifying_detail():
    lead(1, score=80, reasons=["+50 start (fintech)", "+15 own website domain (company1.co.ke)",
                               '+15 site mentions an app or Play Store ("Google Play")',
                               "-20 email is free mail (gmail.com)"], reply="interested")
    lead(2, score=65, reasons=["+50 start (fintech)", '+15 site mentions an app or Play Store ("app")'])
    lead(3, score=None)  # not scored: under no reason
    named = section(score_report.summary(this_year=YEAR), "scoring reason")
    assert set(named) == {"start (base points for the niche)", "own website domain", "site mentions an app or Play Store", "email is free mail"}
    assert numbers(named["site mentions an app or Play Store"]) == (2, 1, 1, 0, "50.0%")
    assert numbers(named["own website domain"])[:2] == (1, 1)
    assert score_report.reason_name("couldn't check website") == "couldn't check website"


def test_niche_source_variant_contact_and_age():
    lead(1, industry="fintech", source="search", variant="D", to_contact=1, est_year=2024, reply="question")
    lead(2, industry="sacco", source="sasra", variant=None, to_contact=0, est_year=2015)
    lead(3, industry=None, source="CBK DCP directory Sep 2026", to_contact=None, est_year=2020)
    lead(4, industry="sacco", source="manual")  # a test lead: left out
    with db.conn() as c:
        c.execute("UPDATE leads SET campaign='Group A' WHERE email='info@company3.co.ke'")
    lines = score_report.summary(this_year=YEAR)
    assert lines[0].startswith("Score report: 3 leads")
    assert set(section(lines, "niche")) == {"fintech", "sacco", "no niche"}
    assert set(section(lines, "lead source")) == {"search", "sasra", "Group A"}
    assert set(section(lines, "variant")) == {"A", "D", "none (before the rotation)"}
    contact = section(lines, "named contact vs general inbox")
    assert numbers(contact["named contact"])[:2] == (1, 1)
    assert set(contact) == {"named contact", "general inbox", "not recorded"}
    assert list(section(lines, "company age")) == ["0-3 years", "4-6 years", "11+ years"]


def test_no_names_or_addresses_in_output():
    for n in range(25):
        lead(n, company=f"Secret Lender {n}", email=f"jane.doe{n}@secretlender{n}.co.ke",
             reasons=["+50 start (fintech)", f"+15 own website domain (secretlender{n}.co.ke)",
                      '+10 site mentions M-Pesa or paybill ("paybill 123456")'], reply="interested")
    text = "\n".join(score_report.summary(this_year=YEAR)).lower()
    assert "secret" not in text and "jane" not in text and "@" not in text and "123456" not in text
    assert ".co.ke" not in text


def test_days_limits_to_recently_first_emailed():
    lead(1, sent_on="2020-01-01")
    lead(2, sent_on="2999-01-01")
    assert score_report.summary(days=30, this_year=YEAR)[0].startswith("Score report: 1 leads (first emailed in the last 30")
    assert score_report.summary(this_year=YEAR)[0].startswith("Score report: 2 leads (all emailed")


def test_empty():
    assert score_report.summary() == ["Score report: 0 leads (all emailed leads)"]


def test_command(monkeypatch, capsys):
    lead(1)
    monkeypatch.setattr(sys, "argv", ["main.py", "score-report"])
    main.main()
    assert "BY SCORE BAND" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["main.py", "score-report", "x"])
    with pytest.raises(SystemExit):
        main.main()
