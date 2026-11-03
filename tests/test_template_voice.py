"""Every email speaks to the one person reading it, as "you": the greeting is "Hello," (or
"Hello Jane," for a named contact), meeting asks are to "you", never to a tech lead, developer
or "whoever handles" something, and {company} is named once, in the subject or the first line,
apart from the "- {company}" signature of a sample SMS. Follow-up 2, which follows every
variant's first email, names it in its first line only when the thread's subject doesn't."""
import re
from pathlib import Path

import pytest

import sender

T = Path(sender.TEMPLATE_DIR)
TEMPLATES = sorted(p for p in T.rglob("*.txt") if "legacy" not in p.parts)
THIRD_PERSON = re.compile(r"tech lead|lead developer|whoever|walk (?:them|your team)|your developers? "
                          r"(?:an|can|use|integrate|set up)|get your developer|Hello \{company\} team", re.I)
SMS_SIGNATURE = re.compile(r'- \{company\}"$')


def rel(path):
    return path.relative_to(T).as_posix()


def subject(name):
    path = T / name
    return path.read_text(encoding="utf-8").split("\n", 1)[0] if path.exists() else None


def first_line(body):
    return next(l.strip() for l in body.split("\n") if l.strip() and l.strip() not in ("{greeting}", "{opener}"))


def body_of(path):
    text = path.read_text(encoding="utf-8")
    return text.split("\n", 1)[1] if "/" not in rel(path) else text


@pytest.mark.parametrize("path", TEMPLATES, ids=rel)
def test_speaks_to_you(path):
    text = path.read_text(encoding="utf-8")
    assert not THIRD_PERSON.search(text), THIRD_PERSON.search(text).group(0)
    assert body_of(path).lstrip().startswith("{greeting}")


@pytest.mark.parametrize("path", TEMPLATES, ids=rel)
def test_company_is_named_once_in_the_subject_or_first_line(path):
    body = body_of(path)
    others = [l for l in body.split("\n") if "{company}" in l and l.strip() != first_line(body)]
    assert all(SMS_SIGNATURE.search(l.strip()) for l in others), others   # elsewhere only as an SMS signature
    if rel(path).startswith("followup2/"):
        assert "{at_company}" in first_line(body) and "{company}" not in first_line(body)
        return
    if "/" in rel(path):   # follow-up 1 replies under its own first email's subject
        subjects = [subject(path.name)]
    else:
        subjects = [path.read_text(encoding="utf-8").split("\n", 1)[0]]
    for s in subjects:
        assert s is not None
        assert s.count("{company}") + first_line(body).count("{company}") == 1, (s, first_line(body))


def test_follow_up_2_names_the_company_only_when_the_subject_doesnt():
    lead = {"company": "Zidi Capital Ltd", "display_name": "Zidi Capital"}
    assert sender.at_company(lead, "Re: When your SMS provider goes down") == " to you at Zidi Capital"
    assert sender.at_company(lead, "Free SMS test for Zidi Capital") == ""
    assert sender.at_company({"company": "x", "name_source": "none"}, "Loan reminders by SMS") == ""


def test_named_contacts_are_greeted_by_first_name():
    lead = {"company": "Zidi", "email": "jane@zidi.co.ke", "contact_name": "Jane Wanjiru",
            "contact_email": "jane@zidi.co.ke"}
    assert sender.greeting(lead) == "Hello Jane,"
    assert sender.greeting(dict(lead, contact_name=None)) == "Hello,"
