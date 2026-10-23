"""Emails sent from the CRM: how they look, new emails, attachments and who can download
them. Nothing is sent: the CRM's mail sending is replaced by a list that keeps each email."""
import base64
import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

import config
import db
from test_crm_access import add_lead, add_user, app, handled, signed_in, team  # noqa: F401  (fixtures)

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def b64(data):
    return base64.b64encode(data).decode()


def office(kind, macros=False):
    """A minimal .docx or .xlsx (or one with macros) as bytes."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", '<Types><Override ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/></Types>'
                   if macros else "<Types/>")
        z.writestr("word/document.xml" if kind == "docx" else "xl/workbook.xml", "<x/>")
    return out.getvalue()


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


def send_files(client, lead_id, files):
    return client.post(f"/api/crm/leads/{lead_id}/email", json={
        "subject": "Files", "body": "Attached.", "attachments": [{"name": n, "data": b64(d)} for n, d in files]})


def test_attachment_limits_and_real_file_types(team, outbox):
    asha, (lead_id, _) = team["asha"], team["leads"]["free"]
    refused = {
        "6 files": [(f"f{i}.pdf", PDF) for i in range(6)],
        "over 10 MB": [("a.pdf", PDF + b"0" * (6 * 1024 * 1024)), ("b.pdf", PDF + b"0" * (5 * 1024 * 1024))],
        "an .exe": [("setup.exe", b"MZ\x90\x00")],
        "no extension": [("README", b"hello")],
        "text named .pdf": [("invoice.pdf", b"not really a pdf")],
        "a png named .jpg": [("photo.jpg", PNG)],
        "a program named .csv": [("data.csv", b"MZ\x90\x00\x03\x00\x00\x00")],
        "a docx with macros": [("letter.docx", office("docx", macros=True))],
        "an xlsx named .docx": [("letter.docx", office("xlsx"))],
        "an empty file": [("empty.pdf", b"")],
    }
    for why, files in refused.items():
        r = send_files(asha, lead_id, files)
        assert r.status_code == 400, why
        assert r.json()["detail"], why
    assert outbox == []
    assert "at most 5 files" in send_files(asha, lead_id, refused["6 files"]).json()["detail"]
    assert "10 MB" in send_files(asha, lead_id, refused["over 10 MB"]).json()["detail"]
    assert "isn't really a .pdf" in send_files(asha, lead_id, refused["text named .pdf"]).json()["detail"]
    assert "Only PDF, Word" in send_files(asha, lead_id, refused["an .exe"]).json()["detail"]

    ok = [("..\\..\\Price list?.PDF", PDF), ("logo.png", PNG), ("rates.csv", "name,price\nSMS,0.80\n".encode()),
          ("Proposal.docx", office("docx")), ("sheet.xlsx", office("xlsx"))]
    r = send_files(asha, lead_id, ok)
    assert r.status_code == 200, r.text
    [msg] = outbox
    parts = {p.get_filename(): p.get_content_type() for p in msg.iter_attachments()}
    assert parts == {"Price list.pdf": "application/pdf", "logo.png": "image/png", "rates.csv": "text/csv",
                     "Proposal.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     "sheet.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    thread = asha.get(f"/api/crm/leads/{lead_id}").json()
    [out] = [m for m in thread["thread"] if m["kind"] == "out"]
    assert [(a["name"], a["size"]) for a in out["attachments"]] == [(n, len(d)) for n, d in
                                                                    zip(parts, [d for _, d in ok])]
    assert thread["can_download"] is True


def test_only_the_leads_owner_and_admins_can_download_its_attachments(team, outbox):
    lead_id, _ = team["leads"]["free"]
    assert send_files(team["asha"], lead_id, [("quote.pdf", PDF)]).status_code == 200
    with db.conn() as c:
        att = c.execute("SELECT id FROM crm_attachments").fetchone()["id"]
    url = f"/api/crm/attachments/{att}"

    r = team["asha"].get(url)
    assert r.status_code == 200 and r.content == PDF and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith('attachment; filename="quote.pdf"')
    assert r.headers["x-content-type-options"] == "nosniff"
    assert team["admin"].get(url).content == PDF

    ben = signed_in(team["admin"].app, "ben@talksasa.co.ke")
    r = ben.get(url)
    assert r.status_code == 404 and PDF not in r.content     # Asha's lead: Ben can't tell it exists
    team["admin"].post(f"/api/crm/leads/{lead_id}/release")
    r = ben.get(url)
    assert r.status_code == 403 and PDF not in r.content     # unclaimed: still only admins
    detail = ben.get(f"/api/crm/leads/{lead_id}").json()
    assert detail["can_download"] is False
    assert TestClient(team["admin"].app).get(url).status_code == 401   # not signed in


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
