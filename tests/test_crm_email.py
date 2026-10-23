"""Emails sent from the CRM: how they look and new emails. Nothing is sent: the CRM's mail sending is replaced by a list that keeps each email."""
import re

import pytest

import config
import db
from test_crm_access import add_lead, add_user, app, handled, signed_in, team  # noqa: F401  (fixtures)

@pytest.fixture
def outbox(app, monkeypatch):
    import crm
    sent = []
    monkeypatch.setattr(crm, "_deliver", sent.append)
    monkeypatch.setattr(config, "COMPANY", "TalkSasa Communications Ltd")
    return sent


def visible_text(html):
    return re.sub(r"<[^>]+>", "", html)


def test_a_reply_is_html_with_a_plain_version_signature_and_grey_quote(team, outbox):
    lead_id, reply_id = team["leads"]["free"]
    with db.conn() as c:
        c.execute("UPDATE replies SET body = ? WHERE id = ?",
                  ("Tell me more about bulk SMS.\n\n> On Mon, Florence <florence@talksasa.co.ke> wrote:\n> Hello", reply_id))
        c.execute("UPDATE users SET phone = '+254 711 000000' WHERE id = ?", (team["ids"]["asha"],))
    r = team["asha"].post(f"/api/crm/leads/{lead_id}/reply", json={"body": "Hi,\nHappy to help.\nAsha"})
    assert r.status_code == 200
    [msg] = outbox
    plain = msg.get_body(("plain",)).get_content()
    assert plain.startswith("Hi,\nHappy to help.\nAsha\n\nAsha\nTalkSasa Communications Ltd\n+254 711 000000\n\nOn ")
    assert "info@free-school.com wrote:\n> Tell me more about bulk SMS." in plain
    page = msg.get_body(("html",)).get_content()
    text = visible_text(page)
    assert "Hi,<br>\nHappy to help.<br>\nAsha" in page                      # line breaks kept
    assert "font-family:Arial" in page and "background:#f5f5f5" in page   # readable font, grey quote
    assert "TalkSasa Communications Ltd" in text and "+254 711 000000" in text
    assert re.search(r"On \w{3}, \d+ \w{3} \d{4} at \d\d:\d\d, info@free-school.com wrote:", text)
    assert "Tell me more about bulk SMS." in text and "florence@talksasa.co.ke" in text
    assert ">" not in text and "&gt;" not in text
    # the conversation keeps the plain version
    out = [m for m in team["asha"].get(f"/api/crm/leads/{lead_id}").json()["thread"] if m["kind"] == "out"]
    assert out[0]["body"] == plain.rstrip("\n") and out[0]["attachments"] == []


def test_a_new_email_claims_the_lead_and_stops_the_bots_emails(team, outbox):
    import ranking
    lead_id, _ = add_lead("info@quiet-school.com", status="sent", label="out_of_office")
    db.record_sent(lead_id, "<first@talksasa.co.ke>", "school")
    waiting_id, _ = add_lead("info@new-school.com", status="verified", label="out_of_office")
    asha = team["asha"]
    assert asha.post(f"/api/crm/leads/{lead_id}/email", json={"subject": " ", "body": "Hi"}).status_code == 400
    assert lead_id in [l["id"] for l in db.awaiting_followup(1)]

    r = asha.post(f"/api/crm/leads/{lead_id}/email", json={"subject": "Bulk SMS pricing", "body": "Hi,\nPrices attached."})
    assert r.status_code == 200 and r.json()["to"] == "info@quiet-school.com"
    asha.post(f"/api/crm/leads/{waiting_id}/email", json={"subject": "Hello", "body": "Hi"})
    msg = outbox[0]
    assert msg["Subject"] == "Bulk SMS pricing" and msg["In-Reply-To"] is None
    assert "wrote:" not in msg.get_body(("plain",)).get_content()  # its own conversation, nothing quoted

    lead = db.find_lead_by_email("info@quiet-school.com")
    assert lead["claimed_by"] == team["ids"]["asha"] and lead["crm_emailed_at"] and lead["status"] == "sent"
    assert lead_id not in [l["id"] for l in db.awaiting_followup(1)]
    assert "CRM" in db.claim_send(lead_id, 1, 100)
    assert waiting_id not in [l["id"] for l in ranking.waiting()]
    assert "CRM" in db.claim_send(waiting_id, 0, 100)
    # someone else's lead now: other sales can't email it
    ben = signed_in(team["admin"].app, "ben@talksasa.co.ke")
    assert ben.post(f"/api/crm/leads/{lead_id}/email", json={"subject": "Hi", "body": "Hi"}).status_code == 404
    assert len(outbox) == 2


@pytest.mark.parametrize("status", ["unsubscribed", "bounced", "invalid"])
def test_no_new_email_or_reply_to_a_lead_that_cannot_be_emailed(team, outbox, status):
    lead_id, _ = add_lead("info@blocked.com", status=status, label="not_interested")
    for path, body in [(f"/api/crm/leads/{lead_id}/email", {"subject": "Hi", "body": "Hi"}),
                       (f"/api/crm/leads/{lead_id}/reply", {"body": "Hi"})]:
        for client in (team["asha"], team["admin"]):
            assert client.post(path, json=body).status_code == 409
    assert outbox == []


def test_a_suppressed_address_is_never_emailed_even_at_another_status(team, outbox):
    lead_id, _ = team["leads"]["free"]
    with db.conn() as c:
        db._suppress(c, "info@free-school.com", "unsubscribed")
    assert team["admin"].post(f"/api/crm/leads/{lead_id}/email", json={"subject": "Hi", "body": "Hi"}).status_code == 409
    assert outbox == []


def test_screens_show_no_ai_wording(team):
    lead_id, reply_id = team["leads"]["free"]
    with db.conn() as c:
        c.execute("UPDATE replies SET summary = ? WHERE id = ?",
                  ("The AI was unsure (label question, confidence 0.40). Please read it. AI summary: wants prices",
                   reply_id))
    detail = team["admin"].get(f"/api/crm/leads/{lead_id}").json()
    listed = team["admin"].get("/api/crm/leads?scope=open").json()["leads"]
    dash = team["admin"].get("/api/all").json()["replies"]
    for summary in ([m["summary"] for m in detail["thread"] if m["kind"] == "in"]
                    + [l["last_summary"] for l in listed] + [r["summary"] for r in dash]):
        assert not re.search(r"\bAI\b", summary or ""), summary
    [mine] = [m for m in detail["thread"] if m["kind"] == "in"]
    assert mine["summary"] == "Unsure of the label (question, confidence 0.40). Please read it. Summary: wants prices"
    with db.conn() as c:  # what is stored (and sent to Telegram) is left as it was
        assert "AI summary" in c.execute("SELECT summary FROM replies WHERE id = ?", (reply_id,)).fetchone()[0]
