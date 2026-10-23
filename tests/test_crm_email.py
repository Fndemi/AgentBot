"""The dashboard and CRM screens never mention the AI."""
import re

import db
from test_crm_access import app, team  # noqa: F401  (fixtures)

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
