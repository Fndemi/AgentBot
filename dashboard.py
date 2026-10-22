"""Simple dashboard. Run: streamlit run dashboard.py"""
import pandas as pd
import streamlit as st

import db

st.set_page_config(page_title="LeadBot", layout="wide")
st.title("LeadBot — bulk SMS outreach")
db.init()

with db.conn() as c:
    # rows as dicts: pandas reads them the same from SQLite and PostgreSQL
    leads = pd.DataFrame([dict(r) for r in c.execute(f"SELECT * FROM leads WHERE {db.real()}")])
    replies = pd.DataFrame([dict(r) for r in c.execute(
        "SELECT r.received_at, l.company, r.from_email, r.label, r.summary, r.body "
        f"FROM replies r LEFT JOIN leads l ON l.id = r.lead_id WHERE {db.real('l')} ORDER BY r.id DESC")])

cols = st.columns(5)
for col, status in zip(cols, ["verified", "sent", "replied", "bounced", "unsubscribed"]):
    col.metric(status.title(), int((leads["status"] == status).sum()) if len(leads) else 0)

st.subheader("Hot replies")
hot = replies[replies["label"].isin(["interested", "question", "needs_review"])]
st.dataframe(hot, use_container_width=True, hide_index=True)

st.subheader("All replies")
st.dataframe(replies, use_container_width=True, hide_index=True)

st.subheader("Leads")
status = st.selectbox("Filter by status", ["all"] + sorted(leads["status"].unique().tolist()) if len(leads) else ["all"])
st.dataframe(leads if status == "all" else leads[leads["status"] == status],
             use_container_width=True, hide_index=True)
