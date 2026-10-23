"""LeadBot CRM: sign-in with two roles (admin, sales), open leads, claim, reply, notes, team.

Install (web.py, right after `app = FastAPI(...)`):
    from crm import install as install_crm
    install_crm(app)

First admin (you type the password yourself; it is never shown or stored in plain text):
    python crm.py create-admin

Other commands:
    python crm.py reset-password someone@talksasa.co.ke
    python crm.py list-users

Settings (.env, all optional):
    CRM_FROM_EMAIL   address replies go out from (default FROM_EMAIL, e.g. florence@...; later sales@...)
    CRM_SMTP_USER    / CRM_SMTP_PASS   login for that mailbox (default SMTP_USER / SMTP_PASS)
    CRM_SENT_FOLDER  IMAP folder to file a copy of each reply in (default "Sent"; blank = don't)
    CRM_DRY_RUN      true = record replies but don't actually send them (for testing)
    SESSION_HOURS    how long a sign-in lasts (default 12)
    COOKIE_SECURE    true once the dashboard is served over HTTPS
"""
import getpass
import hashlib
import hmac
import imaplib
import json
import os
import re
import secrets
import smtplib
import ssl
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import config
import db

HERE = Path(__file__).parent
COOKIE = "lb_session"


def _flag(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


SESSION_HOURS = int(os.getenv("SESSION_HOURS", "12"))
COOKIE_SECURE = _flag("COOKIE_SECURE")
CRM_DRY_RUN = _flag("CRM_DRY_RUN")
CRM_FROM_EMAIL = os.getenv("CRM_FROM_EMAIL") or config.FROM_EMAIL or config.SMTP_USER
CRM_SMTP_USER = os.getenv("CRM_SMTP_USER") or config.SMTP_USER
CRM_SMTP_PASS = os.getenv("CRM_SMTP_PASS") or config.SMTP_PASS
CRM_SENT_FOLDER = os.getenv("CRM_SENT_FOLDER", "Sent")

ROLES = ("admin", "sales")
ACTIONABLE = ("interested", "question", "needs_review")
NO_EMAIL = ("unsubscribed", "bounced", "invalid")   # never emailed again
CLOSED = ("client", "lost", "unsubscribed", "bounced", "invalid")
CRM_STATUSES = ("replied", "meeting", "client", "lost")
EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[A-Za-z]{2,}$")
MIN_PASSWORD = 10


# ---------------------------------------------------------------- database

def conn():
    c = db.conn()
    if not db.postgres():  # PostgreSQL always checks them
        c.execute("PRAGMA foreign_keys = ON")
    return c


def _cols(c, table):
    return db.columns(c, table)


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def iso(ts):
    """SQLite timestamps are UTC without a zone; mark them so browsers convert them."""
    if ts and "T" not in ts and len(ts) > 10:
        return ts.replace(" ", "T") + "Z"
    return ts


def ensure_schema():
    db.init()  # the CRM's tables refer to leads, which PostgreSQL needs to exist first
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            phone TEXT DEFAULT '',
            role TEXT NOT NULL CHECK (role IN ('admin','sales')),
            password_hash TEXT NOT NULL,
            must_change INTEGER NOT NULL DEFAULT 1,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_login TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crm_messages (
            id INTEGER PRIMARY KEY,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            user_id INTEGER REFERENCES users(id),
            to_email TEXT,
            subject TEXT,
            body TEXT,
            message_id TEXT,
            in_reply_to TEXT,
            status TEXT,            -- sent / dry_run / failed
            error TEXT,
            sent_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS crm_notes (
            id INTEGER PRIMARY KEY,
            lead_id INTEGER NOT NULL REFERENCES leads(id),
            user_id INTEGER REFERENCES users(id),
            body TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS crm_events (
            id INTEGER PRIMARY KEY,
            lead_id INTEGER,
            user_id INTEGER,
            action TEXT NOT NULL,
            detail TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_crm_messages_lead ON crm_messages(lead_id);
        CREATE INDEX IF NOT EXISTS idx_crm_notes_lead ON crm_notes(lead_id);
        CREATE INDEX IF NOT EXISTS idx_crm_events_lead ON crm_events(lead_id);
        -- one-time data changes, so each runs once and never undoes what people did since
        CREATE TABLE IF NOT EXISTS crm_migrations (
            name TEXT PRIMARY KEY,
            done_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        lc = _cols(c, "leads")
        if "claimed_by" not in lc:
            c.execute("ALTER TABLE leads ADD COLUMN claimed_by INTEGER")
        if "claimed_at" not in lc:
            c.execute("ALTER TABLE leads ADD COLUMN claimed_at TEXT")
        if "handled" not in _cols(c, "replies"):
            c.execute("ALTER TABLE replies ADD COLUMN handled INTEGER DEFAULT 0")
        # Every reply but a bounce now needs someone to read it. Replies an older version marked
        # handled by itself (not interested, auto-replies, unsubscribes) go back to waiting,
        # unless someone has answered the lead since. Once only: Mark done after this sticks.
        if not c.execute("SELECT 1 FROM crm_migrations WHERE name = 'review_all_replies'").fetchone():
            c.execute("""UPDATE replies SET handled = 0
                         WHERE COALESCE(handled, 0) = 1 AND COALESCE(label, '') != 'bounce'
                           AND NOT EXISTS (SELECT 1 FROM crm_messages m WHERE m.lead_id = replies.lead_id
                                           AND m.status != 'failed' AND m.sent_at >= replies.received_at)""")
            c.execute("INSERT INTO crm_migrations (name) VALUES ('review_all_replies')")
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now_utc(),))


# Summaries are stored as the inbox check wrote them (they also go to Telegram). On the
# dashboard and CRM screens they are shown without the words about the AI.
SCREEN_WORDING = [
    (re.compile(r"\bnot read by the AI\b"), "not labelled automatically"),
    (re.compile(r"\bThe AI could not read this reply\b"), "This reply couldn't be labelled automatically"),
    (re.compile(r"\bThe AI thinks this may be\b"), "This may be"),
    (re.compile(r"\bThe AI was unsure \(label "), "Unsure of the label ("),
    (re.compile(r"\bAI summary:"), "Summary:"),
    (re.compile(r"\b[Tt]he AI\b"), "LeadBot"),
    (re.compile(r"\bAI\b"), "automatic"),
]


def screen_text(text):
    for pattern, words in SCREEN_WORDING:
        text = pattern.sub(words, text) if text else text
    return text


def log_event(c, lead_id, user_id, action, detail=""):
    c.execute("INSERT INTO crm_events (lead_id, user_id, action, detail) VALUES (?,?,?,?)",
              (lead_id, user_id, action, detail))


# ---------------------------------------------------------------- passwords & sessions

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        test = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(dk) // 2)
        return hmac.compare_digest(test.hex(), dk)
    except (ValueError, TypeError):
        return False


def password_problem(pw: str):
    if len(pw or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if pw.strip() != pw:
        return "The password can't start or end with a space."
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(c, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?,?,?)",
              (_token_hash(token), user_id, expires))
    return token


def user_from_token(token):
    if not token:
        return None
    with conn() as c:
        row = c.execute("""SELECT u.id, u.name, u.email, u.phone, u.role, u.must_change, u.active
                           FROM sessions s JOIN users u ON u.id = s.user_id
                           WHERE s.token_hash = ? AND s.expires_at > ?""",
                        (_token_hash(token), now_utc())).fetchone()
    if not row or not row["active"]:
        return None
    u = dict(row)
    u["must_change"] = bool(u["must_change"])
    u.pop("active", None)
    return u


def set_cookie(resp, token):
    resp.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True,
                    samesite="strict", secure=COOKIE_SECURE, path="/")


# Simple brute-force brake: 5 failed tries per email (20 per network address) in 15 minutes.
_fails = defaultdict(list)


def _too_many(key):
    cutoff = time.time() - 900
    _fails[key] = [t for t in _fails[key] if t > cutoff]
    limit = 20 if key.startswith("ip:") else 5   # an office shares one address
    return len(_fails[key]) >= limit


# ---------------------------------------------------------------- guard

PUBLIC_PATHS = {"/login", "/api/auth/login", "/favicon.ico"}
WHILE_MUST_CHANGE = {"/login", "/api/auth/me", "/api/auth/password", "/api/auth/logout"}
# The dashboard (web.py, web_extra.py) and the What's working report: admins only
ADMIN_PATHS = {"/", "/api/all", "/api/sent-today", "/api/crm/report"}
ADMIN_PREFIXES = ("/api/leads/", "/api/replies/")


def admin_path(path):
    path = path.rstrip("/") or "/"
    return path in ADMIN_PATHS or (path + "/").startswith(ADMIN_PREFIXES)


def _safe_next(path):
    return path if path.startswith("/") and not path.startswith("//") else "/"


def install(app):
    """Protect every page and API route of the dashboard and add the CRM routes."""
    ensure_schema()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        # Cross-site request check for anything that changes data (cookie is SameSite=Strict as well)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "Request blocked: it came from another site."}, 403)
        if path in PUBLIC_PATHS:
            return await call_next(request)
        user = user_from_token(request.cookies.get(COOKIE))
        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Please sign in."}, 401)
            return RedirectResponse("/login?next=" + quote(_safe_next(path)), 303)
        if user["must_change"] and path not in WHILE_MUST_CHANGE:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Set a new password first."}, 403)
            return RedirectResponse("/login", 303)
        if user["role"] != "admin" and admin_path(path):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Only an admin can see this."}, 403)
            return HTMLResponse("<!doctype html><meta charset='utf-8'><title>LeadBot</title>"
                                "<p style='font-family:system-ui;margin:40px'>Only an admin can see the dashboard. "
                                "<a href='/crm'>Go to the CRM</a></p>", 403, headers={"Cache-Control": "no-store"})
        request.state.user = user
        response = await call_next(request)
        if not path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(router)


def current_user(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Please sign in.")
    return user


def admin_only(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Only an admin can do that.")
    return user


router = APIRouter()


def _page(name):
    html = (HERE / "static" / name).read_text(encoding="utf-8")
    return HTMLResponse("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
                        "</head><body>" + html + "</body></html>",
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


@router.get("/login", response_class=HTMLResponse)
def login_page():
    return _page("login.html")


@router.get("/crm", response_class=HTMLResponse)
def crm_page():
    return _page("crm.html")


# ---------------------------------------------------------------- auth API

class LoginIn(BaseModel):
    email: str
    password: str


class PasswordIn(BaseModel):
    current: str
    new: str


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    email = body.email.strip().lower()
    ip = request.client.host if request.client else "?"
    if _too_many("e:" + email) or _too_many("ip:" + ip):
        raise HTTPException(429, "Too many tries. Wait 15 minutes and try again.")
    with conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        ok = bool(row) and row["active"] and check_password(body.password, row["password_hash"])
        if not row:
            check_password(body.password, hash_password("timing-equaliser"))  # same delay either way
        if not ok:
            _fails["e:" + email].append(time.time())
            _fails["ip:" + ip].append(time.time())
            raise HTTPException(401, "That email and password don't match.")
        _fails.pop("e:" + email, None)
        token = new_session(c, row["id"])
        c.execute("UPDATE users SET last_login = ? WHERE id = ?", (now_utc(), row["id"]))
        log_event(c, None, row["id"], "login")
    resp = JSONResponse({"ok": True, "must_change": bool(row["must_change"]),
                         "user": {"name": row["name"], "role": row["role"]}})
    set_cookie(resp, token)
    return resp


@router.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        with conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/api/auth/me")
def me(user=Depends(current_user)):
    return {"user": user, "dry_run": CRM_DRY_RUN, "from_email": CRM_FROM_EMAIL}


@router.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, user=Depends(current_user)):
    problem = password_problem(body.new)
    if problem:
        raise HTTPException(400, problem)
    if body.new == body.current:
        raise HTTPException(400, "Choose a password different from the current one.")
    with conn() as c:
        row = c.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not check_password(body.current, row["password_hash"]):
            raise HTTPException(400, "Your current password isn't right.")
        c.execute("UPDATE users SET password_hash = ?, must_change = 0 WHERE id = ?",
                  (hash_password(body.new), user["id"]))
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))  # sign out other devices
        token = new_session(c, user["id"])
        log_event(c, None, user["id"], "password_changed")
    resp = JSONResponse({"ok": True})
    set_cookie(resp, token)
    return resp


# ---------------------------------------------------------------- leads

def _sees(user, lead):
    """Sales see leads nobody has claimed and their own; admins see every lead."""
    return user["role"] == "admin" or lead["claimed_by"] in (None, user["id"])


def _lead_or_404(c, lead_id, user):
    """A colleague's lead is "not found" for sales, so they can't tell it exists."""
    lead = c.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead or not _sees(user, lead):
        raise HTTPException(404, "Lead not found.")
    return lead


# What sales see of a lead; admins see every column
SALES_FIELDS = ("id", "company", "email", "phone", "industry", "website", "score", "status",
                "claimed_by", "claimed_at", "created_at")


def _can_email(c, lead):
    return lead["status"] not in NO_EMAIL and not db.is_suppressed(lead["email"] or "")


def _score_cols(c):
    """The targeting step adds score, score_reasons and source to leads; older databases don't have them."""
    lc = _cols(c, "leads")
    return {k: (k if k in lc else None) for k in ("score", "score_reasons", "source")}


def _reasons(raw):
    """score_reasons is saved as a JSON list; show it as short readable strings."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return [str(raw)]
    if isinstance(val, dict):
        val = [f"{k} {v}" for k, v in val.items()]
    if not isinstance(val, list):
        return [str(val)]
    out = []
    for x in val:
        if isinstance(x, dict):
            pts = x.get("points", x.get("score", x.get("delta")))
            why = x.get("reason") or x.get("rule") or x.get("why") or ""
            out.append((f"{'+' if isinstance(pts, (int, float)) and pts > 0 else ''}{pts} {why}").strip() if pts is not None else str(why))
        else:
            out.append(str(x))
    return [o for o in out if o][:12]


def _user_names(c):
    return {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM users")}


@router.get("/api/crm/leads")
def open_leads(scope: str = "open", user=Depends(current_user)):
    """Open = the lead has written back and the deal isn't closed yet."""
    if scope not in ("open", "mine", "closed"):
        raise HTTPException(400, "Unknown view.")
    closed = ",".join("'%s'" % s for s in CLOSED)
    actionable = ",".join("'%s'" % s for s in ACTIONABLE)
    no_email = ",".join("'%s'" % s for s in NO_EMAIL)
    waiting = "COALESCE(r.handled,0) = 0 AND COALESCE(r.label,'') != 'bounce'"   # every reply but a bounce
    me = int(user["id"])
    if scope == "closed":
        where = f"l.status IN ('client','lost')"
    else:
        where = (f"((l.status NOT IN ({closed}) AND (l.status IN ('replied','meeting') OR EXISTS "
                 f"(SELECT 1 FROM replies x WHERE x.lead_id = l.id AND x.label IN ({actionable})))) OR EXISTS "
                 f"(SELECT 1 FROM replies r WHERE r.lead_id = l.id AND {waiting}))")
    if scope == "mine" or (scope == "closed" and user["role"] != "admin"):
        where += " AND l.claimed_by = %d" % me
    elif user["role"] != "admin":
        where += " AND (l.claimed_by IS NULL OR l.claimed_by = %d)" % me
    suppressed = db.suppressed_emails()
    with conn() as c:
        names = _user_names(c)
        sc = _score_cols(c)
        rows = [dict(r) for r in c.execute(f"""
            SELECT l.id, l.company, l.email, l.industry, l.status, l.website, l.claimed_by, l.claimed_at,
                   {('l.' + sc['score']) if sc['score'] else 'NULL'} AS score,
                   (SELECT COUNT(*) FROM replies r WHERE r.lead_id = l.id AND {waiting}) AS waiting,
                   CASE WHEN l.status IN ({no_email}) THEN 0 ELSE 1 END AS can_email,
                   (SELECT r.id FROM replies r WHERE r.lead_id = l.id AND r.label NOT IN ('bounce','out_of_office')
                        ORDER BY r.id DESC LIMIT 1) AS last_reply_id,
                   (SELECT MAX(m.sent_at) FROM crm_messages m WHERE m.lead_id = l.id AND m.status != 'failed') AS last_answer
            FROM leads l WHERE {where}""")]
        for r in rows:
            last = None
            if r["last_reply_id"]:
                last = c.execute("SELECT label, summary, subject, body, received_at FROM replies WHERE id = ?",
                                 (r["last_reply_id"],)).fetchone()
            r["last_label"] = last["label"] if last else None
            r["last_summary"] = (screen_text(last["summary"]) or (last["body"] or "")[:160]) if last else ""
            r["last_received"] = iso(last["received_at"]) if last else None
            r["last_answer"] = iso(r["last_answer"])
            r["claimed_at"] = iso(r["claimed_at"])
            r["owner"] = names.get(r["claimed_by"])
            r["mine"] = r["claimed_by"] == user["id"]
            r["can_email"] = bool(r["can_email"]) and (r["email"] or "").lower().strip() not in suppressed
            del r["last_reply_id"]
    # Waiting on us first, newest reply first
    rows.sort(key=lambda r: r["last_received"] or "", reverse=True)
    rows.sort(key=lambda r: r["waiting"] == 0)
    counts = {"waiting": sum(1 for r in rows if r["waiting"]),
              "unclaimed": sum(1 for r in rows if not r["claimed_by"]),
              "mine": sum(1 for r in rows if r["mine"])}
    return {"leads": rows, "counts": counts, "me": user["id"]}


@router.get("/api/crm/leads/{lead_id}")
def lead_detail(lead_id: int, user=Depends(current_user)):
    with conn() as c:
        row = _lead_or_404(c, lead_id, user)
        admin = user["role"] == "admin"
        lead = dict(row) if admin else {k: row[k] for k in SALES_FIELDS if k in row.keys()}
        names = _user_names(c)
        if admin:
            lead["score_reasons"] = _reasons(lead.get("score_reasons"))
        sc = _cols(c, "sent")
        subj = "sent_subject" if "sent_subject" in sc else ("subject" if "subject" in sc else None)
        body = "sent_body" if "sent_body" in sc else ("body" if "body" in sc else None)
        step = "step" if "step" in sc else None
        items = []
        for i, r in enumerate(c.execute(f"""SELECT id, sent_on, {step or 'NULL'} AS step,
                {subj or 'NULL'} AS subject, {body or 'NULL'} AS body FROM sent WHERE lead_id = ? ORDER BY id""",
                                        (lead_id,))):
            items.append({"kind": "bot", "id": r["id"], "at": r["sent_on"], "step": r["step"] or i + 1,
                          "subject": r["subject"], "body": r["body"], "who": "LeadBot"})
        for r in c.execute("""SELECT id, from_email, subject, body, label, summary, received_at,
                COALESCE(handled,0) AS handled FROM replies WHERE lead_id = ? ORDER BY id""", (lead_id,)):
            items.append({"kind": "in", "id": r["id"], "at": iso(r["received_at"]), "subject": r["subject"],
                          "body": r["body"], "label": r["label"], "summary": screen_text(r["summary"]),
                          "from": r["from_email"], "handled": bool(r["handled"]), "who": lead["company"]})
        for r in c.execute("SELECT * FROM crm_messages WHERE lead_id = ? ORDER BY id", (lead_id,)):
            items.append({"kind": "out", "id": r["id"], "at": iso(r["sent_at"]), "subject": r["subject"],
                          "body": r["body"], "to": r["to_email"], "status": r["status"], "error": r["error"],
                          "who": names.get(r["user_id"], "Someone")})
        for r in c.execute("SELECT * FROM crm_notes WHERE lead_id = ? ORDER BY id", (lead_id,)):
            items.append({"kind": "note", "id": r["id"], "at": iso(r["created_at"]), "body": r["body"],
                          "who": names.get(r["user_id"], "Someone")})
        events = [{"action": r["action"], "detail": r["detail"], "at": iso(r["created_at"]),
                   "who": names.get(r["user_id"], "LeadBot")}
                  for r in c.execute("SELECT * FROM crm_events WHERE lead_id = ? ORDER BY id DESC LIMIT 50", (lead_id,))
                  ] if admin else []  # the history names colleagues and who a lead was assigned to
        can_reply = _can_email(c, row)
    # bot rows only have a date; put them at the start of that day so order stays sensible
    items.sort(key=lambda x: (x["at"] or "") if "T" in (x["at"] or "") else (x["at"] or "") + "T00:00:00Z")
    lead["owner"] = names.get(lead.get("claimed_by"))
    lead["mine"] = lead.get("claimed_by") == user["id"]
    lead["claimed_at"] = iso(lead.get("claimed_at"))
    lead["created_at"] = iso(lead.get("created_at"))
    return {"lead": lead, "thread": items, "events": events, "can_reply": can_reply,
            "can_act": user["role"] == "admin" or not lead.get("claimed_by") or lead["mine"]}


def _require_owner_or_free(lead, user):
    owner = lead["claimed_by"]
    if owner and owner != user["id"] and user["role"] != "admin":
        raise HTTPException(409, "Someone else is handling this lead. Ask an admin to reassign it.")


@router.post("/api/crm/leads/{lead_id}/claim")
def claim(lead_id: int, user=Depends(current_user)):
    with conn() as c:
        lead = _lead_or_404(c, lead_id, user)
        if lead["claimed_by"] == user["id"]:
            return {"ok": True}
        # Only succeeds if nobody grabbed it in the meantime
        cur = c.execute("UPDATE leads SET claimed_by = ?, claimed_at = ? WHERE id = ? AND claimed_by IS NULL",
                        (user["id"], now_utc(), lead_id))
        if cur.rowcount == 0:
            if user["role"] != "admin":
                raise HTTPException(409, "Someone else claimed this lead just now.")
            owner = _user_names(c).get(lead["claimed_by"], "someone else")
            raise HTTPException(409, f"{owner} already claimed this lead.")
        log_event(c, lead_id, user["id"], "claimed")
    return {"ok": True}


@router.post("/api/crm/leads/{lead_id}/release")
def release(lead_id: int, user=Depends(current_user)):
    with conn() as c:
        lead = _lead_or_404(c, lead_id, user)
        if not lead["claimed_by"]:
            return {"ok": True}
        if lead["claimed_by"] != user["id"] and user["role"] != "admin":
            raise HTTPException(403, "Only the person handling this lead or an admin can release it.")
        c.execute("UPDATE leads SET claimed_by = NULL, claimed_at = NULL WHERE id = ?", (lead_id,))
        log_event(c, lead_id, user["id"], "released")
    return {"ok": True}


class AssignIn(BaseModel):
    user_id: int


@router.post("/api/crm/leads/{lead_id}/assign")
def assign(lead_id: int, body: AssignIn, admin=Depends(admin_only)):
    with conn() as c:
        _lead_or_404(c, lead_id, admin)
        target = c.execute("SELECT id, name, active FROM users WHERE id = ?", (body.user_id,)).fetchone()
        if not target or not target["active"]:
            raise HTTPException(400, "That person isn't an active user.")
        c.execute("UPDATE leads SET claimed_by = ?, claimed_at = ? WHERE id = ?", (target["id"], now_utc(), lead_id))
        log_event(c, lead_id, admin["id"], "assigned", target["name"])
    return {"ok": True}


class NoteIn(BaseModel):
    body: str


@router.post("/api/crm/leads/{lead_id}/notes")
def add_note(lead_id: int, body: NoteIn, user=Depends(current_user)):
    text = body.body.strip()
    if not text:
        raise HTTPException(400, "The note is empty.")
    if len(text) > 5000:
        raise HTTPException(400, "Keep notes under 5,000 characters.")
    with conn() as c:
        _lead_or_404(c, lead_id, user)
        c.execute("INSERT INTO crm_notes (lead_id, user_id, body) VALUES (?,?,?)", (lead_id, user["id"], text))
    return {"ok": True}


class StatusIn(BaseModel):
    status: str


@router.post("/api/crm/leads/{lead_id}/status")
def crm_status(lead_id: int, body: StatusIn, user=Depends(current_user)):
    if body.status not in CRM_STATUSES:
        raise HTTPException(400, "Unknown stage.")
    with conn() as c:
        lead = _lead_or_404(c, lead_id, user)
        _require_owner_or_free(lead, user)
        if lead["status"] in ("unsubscribed", "bounced"):
            raise HTTPException(409, "This lead unsubscribed or bounced, so its stage can't change here.")
        c.execute("UPDATE leads SET status = ? WHERE id = ?", (body.status, lead_id))
        if body.status in ("client", "lost"):
            c.execute("UPDATE replies SET handled = 1 WHERE lead_id = ?", (lead_id,))
        log_event(c, lead_id, user["id"], "stage", body.status)
    return {"ok": True}


@router.post("/api/crm/replies/{reply_id}/done")
def reply_done(reply_id: int, user=Depends(current_user)):
    with conn() as c:
        r = c.execute("SELECT lead_id FROM replies WHERE id = ?", (reply_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Reply not found.")
        if r["lead_id"] is None and user["role"] != "admin":
            raise HTTPException(404, "Reply not found.")
        _require_owner_or_free(_lead_or_404(c, r["lead_id"], user), user)
        c.execute("UPDATE replies SET handled = 1 WHERE id = ?", (reply_id,))
        log_event(c, r["lead_id"], user["id"], "marked_done")
    return {"ok": True}


# ---------------------------------------------------------------- replying

class ReplyIn(BaseModel):
    body: str
    subject: Optional[str] = None
    reply_id: Optional[int] = None   # the incoming message being answered


def signature(user):
    lines = [user["name"], config.COMPANY]
    phone = (user.get("phone") or "").strip() or (config.SENDER_PHONE or "").strip()
    if phone:
        lines.append(phone)
    return "\n".join(lines)


def _quote(text, who, when):
    text = (text or "").strip()
    if not text:
        return ""
    quoted = "\n".join("> " + line for line in text.splitlines()[:60])
    return f"\n\nOn {when}, {who} wrote:\n{quoted}"


def _deliver(msg):
    if CRM_DRY_RUN:
        return
    if not (config.SMTP_HOST and CRM_SMTP_USER and CRM_SMTP_PASS):
        raise RuntimeError("Email isn't set up: SMTP_HOST and the mailbox login are missing from .env.")
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ctx, timeout=30) as s:
            s.login(CRM_SMTP_USER, CRM_SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(CRM_SMTP_USER, CRM_SMTP_PASS)
            s.send_message(msg)


def _file_in_sent(msg):
    """Best effort: put a copy in the mailbox's Sent folder so it shows in webmail."""
    if CRM_DRY_RUN or not CRM_SENT_FOLDER or not config.IMAP_HOST:
        return
    try:
        with imaplib.IMAP4_SSL(config.IMAP_HOST, timeout=20) as m:
            m.login(CRM_SMTP_USER, CRM_SMTP_PASS)
            m.append(CRM_SENT_FOLDER, "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
    except Exception:
        pass


@router.post("/api/crm/leads/{lead_id}/reply")
def send_reply(lead_id: int, body: ReplyIn, user=Depends(current_user)):
    text = body.body.strip()
    if not text:
        raise HTTPException(400, "Write a message first.")
    if not CRM_FROM_EMAIL or not EMAIL_RE.match(CRM_FROM_EMAIL):
        raise HTTPException(503, "Replies aren't set up yet: add FROM_EMAIL (or CRM_FROM_EMAIL) to .env and restart.")
    if len(text) > 20000:
        raise HTTPException(400, "That message is too long.")
    with conn() as c:
        lead = _lead_or_404(c, lead_id, user)
        _require_owner_or_free(lead, user)
        if not _can_email(c, lead):
            raise HTTPException(409, "This lead unsubscribed or their address bounced. Don't email them.")
        orig = None
        if body.reply_id:
            orig = c.execute("SELECT * FROM replies WHERE id = ? AND lead_id = ?", (body.reply_id, lead_id)).fetchone()
            if not orig:
                raise HTTPException(404, "The message you're answering wasn't found.")
        else:
            orig = c.execute("""SELECT * FROM replies WHERE lead_id = ? AND label NOT IN ('bounce','out_of_office')
                                ORDER BY id DESC LIMIT 1""", (lead_id,)).fetchone()
        to = (orig["from_email"] if orig and orig["from_email"] else lead["email"] or "").strip()
        m = re.search(r"<([^>]+)>", to)
        to = (m.group(1) if m else to).strip()
        if not EMAIL_RE.match(to):
            raise HTTPException(400, f"The lead's email address doesn't look valid: {to or 'missing'}.")

        subject = (body.subject or "").strip()
        if not subject:
            base = (orig["subject"] if orig else None) or "TalkSasa"
            subject = base if base.lower().startswith("re:") else "Re: " + base
        subject = subject.replace("\r", " ").replace("\n", " ")[:200]

        # Thread it with the conversation: answer the lead's message, else our last email to them
        refs = []
        rc = _cols(c, "replies")
        if orig is not None and "message_id" in rc and orig["message_id"]:
            refs.append(orig["message_id"])
        last_out = c.execute("SELECT message_id FROM crm_messages WHERE lead_id = ? AND message_id IS NOT NULL "
                             "ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
        last_bot = c.execute("SELECT message_id FROM sent WHERE lead_id = ? AND message_id IS NOT NULL "
                             "ORDER BY id DESC LIMIT 1", (lead_id,)).fetchone()
        for r in (last_out, last_bot):
            if r and r["message_id"] and r["message_id"] not in refs:
                refs.append(r["message_id"])

        full = text + "\n\n" + signature(user)
        if orig is not None:
            full += _quote(orig["body"], orig["from_email"] or lead["company"], (orig["received_at"] or "")[:16])

        msg = EmailMessage()
        msg["From"] = formataddr((f"{user['name']} from TalkSasa", CRM_FROM_EMAIL))
        msg["To"] = to
        msg["Reply-To"] = CRM_FROM_EMAIL
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        domain = (CRM_FROM_EMAIL or "talksasa.co.ke").split("@")[-1]
        msg_id = make_msgid(domain=domain)
        msg["Message-ID"] = msg_id
        if refs:
            msg["In-Reply-To"] = refs[0]
            msg["References"] = " ".join(reversed(refs))
        msg.set_content(full)

        status, error = ("dry_run" if CRM_DRY_RUN else "sent"), None
        try:
            _deliver(msg)
        except Exception as e:  # keep a record of what failed
            status, error = "failed", str(e)[:300]
        c.execute("""INSERT INTO crm_messages (lead_id, user_id, to_email, subject, body, message_id, in_reply_to,
                     status, error) VALUES (?,?,?,?,?,?,?,?,?)""",
                  (lead_id, user["id"], to, subject, full, msg_id, refs[0] if refs else None, status, error))
        if status == "failed":
            log_event(c, lead_id, user["id"], "reply_failed", error)
            c.commit()
            raise HTTPException(502, f"The email didn't send: {error}")
        if not lead["claimed_by"]:
            c.execute("UPDATE leads SET claimed_by = ?, claimed_at = ? WHERE id = ?", (user["id"], now_utc(), lead_id))
            log_event(c, lead_id, user["id"], "claimed", "by replying")
        c.execute("UPDATE replies SET handled = 1 WHERE lead_id = ? AND COALESCE(handled,0) = 0", (lead_id,))
        if lead["status"] in ("sent", "verified", "new"):
            c.execute("UPDATE leads SET status = 'replied' WHERE id = ?", (lead_id,))
        log_event(c, lead_id, user["id"], "replied", to)
    _file_in_sent(msg)
    return {"ok": True, "status": status, "to": to}


# ---------------------------------------------------------------- what's working

NOT_REAL_REPLY = ("bounce", "out_of_office")
MIN_SAMPLE = 20


def _band(score):
    if score is None:
        return "Not scored"
    s = float(score)
    if s >= 80:
        return "80–100"
    if s >= 60:
        return "60–79"
    if s >= 50:
        return "50–59"
    return "Below 50"


@router.get("/api/crm/report")
def report(days: int = 0, user=Depends(admin_only)):
    """Reply results by segment, score band and source, counted per lead that was emailed.

    days = 0 means all time; otherwise only leads first emailed in the last N days.
    """
    if days < 0 or days > 3650:
        raise HTTPException(400, "Pick a period between 1 and 3650 days, or all time.")
    with conn() as c:
        sc = _score_cols(c)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d") if days else "0000"
        rows = c.execute(f"""
            SELECT l.id, COALESCE(NULLIF(l.industry, ''), 'default') AS industry, l.status,
                   {('l.' + sc['score']) if sc['score'] else 'NULL'} AS score,
                   {("COALESCE(NULLIF(l." + sc['source'] + ", ''), 'Web search')") if sc['source'] else "'Web search'"} AS source,
                   MIN(s.sent_on) AS first_sent,
                   MAX(CASE WHEN r.label = 'bounce' THEN 1 ELSE 0 END) AS bounced_reply,
                   MAX(CASE WHEN r.id IS NOT NULL AND r.label NOT IN ('bounce','out_of_office') THEN 1 ELSE 0 END) AS replied,
                   MAX(CASE WHEN r.label = 'interested' THEN 1 ELSE 0 END) AS interested_reply
            FROM leads l
            JOIN sent s ON s.lead_id = l.id
            LEFT JOIN replies r ON r.lead_id = l.id
            GROUP BY l.id
            HAVING MIN(s.sent_on) >= ?""", (since,)).fetchall()

    def blank():
        return {"emailed": 0, "bounced": 0, "reachable": 0, "replied": 0, "interested": 0, "meetings": 0, "clients": 0}

    groups = {"segment": {}, "score": {}, "source": {}}
    total = blank()
    for r in rows:
        bounced = r["status"] == "bounced" or bool(r["bounced_reply"])
        interested = bool(r["interested_reply"]) or r["status"] in ("meeting", "client")
        meeting = r["status"] in ("meeting", "client")
        keys = {"segment": r["industry"], "score": _band(r["score"]), "source": r["source"]}
        for g, key in keys.items():
            for bucket in (groups[g].setdefault(key, blank()),) + ((total,) if g == "segment" else ()):
                bucket["emailed"] += 1
                bucket["bounced"] += bounced
                bucket["reachable"] += not bounced
                bucket["replied"] += bool(r["replied"]) and not bounced
                bucket["interested"] += interested
                bucket["meetings"] += meeting
                bucket["clients"] += r["status"] == "client"

    def finish(d):
        reach = d["reachable"]
        d["reply_rate"] = round(100 * d["replied"] / reach, 1) if reach else None
        d["interested_rate"] = round(100 * d["interested"] / reach, 1) if reach else None
        d["small_sample"] = reach < MIN_SAMPLE
        return d

    order = {"score": ["80–100", "60–79", "50–59", "Below 50", "Not scored"]}
    out = {}
    for g, d in groups.items():
        items = [dict(finish(v), name=k) for k, v in d.items()]
        if g in order:
            items.sort(key=lambda x: order[g].index(x["name"]) if x["name"] in order[g] else 99)
        else:
            items.sort(key=lambda x: (-x["emailed"], x["name"]))  # ties by name, on any database
        out[g] = items
    return {"total": finish(total), "groups": out, "days": days, "min_sample": MIN_SAMPLE,
            "has_scores": bool(sc["score"]), "has_source": bool(sc["source"])}


# ---------------------------------------------------------------- team (admin)

class NewUserIn(BaseModel):
    name: str
    email: str
    role: str = "sales"
    phone: str = ""
    temp_password: str


class EditUserIn(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    temp_password: Optional[str] = None


@router.get("/api/crm/users")
def list_users(user=Depends(current_user)):
    with conn() as c:
        rows = [dict(r) for r in c.execute("""
            SELECT u.id, u.name, u.email, u.phone, u.role, u.active, u.must_change, u.last_login,
                   (SELECT COUNT(*) FROM leads l WHERE l.claimed_by = u.id AND l.status NOT IN ('client','lost')) AS open_leads,
                   (SELECT COUNT(*) FROM crm_messages m WHERE m.user_id = u.id AND m.status != 'failed') AS replies_sent
            FROM users u ORDER BY u.active DESC, u.role, u.name""")]
    for r in rows:
        r["active"], r["must_change"] = bool(r["active"]), bool(r["must_change"])
        r["last_login"] = iso(r["last_login"])
    if user["role"] != "admin":  # sales don't see who handles the other leads
        rows = [{"id": r["id"], "name": r["name"], "active": r["active"]} for r in rows if r["id"] == user["id"]]
    return {"users": rows}


def _clean_email(email):
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "Enter a valid email address.")
    return email


@router.post("/api/crm/users")
def create_user(body: NewUserIn, admin=Depends(admin_only)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Enter a name.")
    if body.role not in ROLES:
        raise HTTPException(400, "Role must be admin or sales.")
    problem = password_problem(body.temp_password)
    if problem:
        raise HTTPException(400, problem)
    email = _clean_email(body.email)
    with conn() as c:
        try:
            c.execute("""INSERT INTO users (name, email, phone, role, password_hash, must_change)
                         VALUES (?,?,?,?,?,1)""",
                      (name, email, body.phone.strip(), body.role, hash_password(body.temp_password)))
        except db.IntegrityError:
            raise HTTPException(409, "Someone with that email already has an account.")
        log_event(c, None, admin["id"], "user_created", f"{email} ({body.role})")
    return {"ok": True}


@router.post("/api/crm/users/{user_id}")
def edit_user(user_id: int, body: EditUserIn, admin=Depends(admin_only)):
    with conn() as c:
        target = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found.")
        demoting = body.role == "sales" and target["role"] == "admin"
        disabling = body.active is False and target["active"]
        if (demoting or disabling) and target["role"] == "admin":
            admins = c.execute("SELECT COUNT(*) n FROM users WHERE role = 'admin' AND active = 1").fetchone()["n"]
            if admins <= 1:
                raise HTTPException(409, "There must always be at least one active admin.")
        if user_id == admin["id"] and (demoting or disabling):
            raise HTTPException(409, "You can't remove your own admin access. Ask another admin.")
        if body.role is not None:
            if body.role not in ROLES:
                raise HTTPException(400, "Role must be admin or sales.")
            c.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
        if body.name is not None and body.name.strip():
            c.execute("UPDATE users SET name = ? WHERE id = ?", (body.name.strip(), user_id))
        if body.phone is not None:
            c.execute("UPDATE users SET phone = ? WHERE id = ?", (body.phone.strip(), user_id))
        if body.active is not None:
            c.execute("UPDATE users SET active = ? WHERE id = ?", (int(body.active), user_id))
            if not body.active:
                c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                c.execute("UPDATE leads SET claimed_by = NULL, claimed_at = NULL WHERE claimed_by = ? "
                          "AND status NOT IN ('client','lost')", (user_id,))
        if body.temp_password:
            problem = password_problem(body.temp_password)
            if problem:
                raise HTTPException(400, problem)
            c.execute("UPDATE users SET password_hash = ?, must_change = 1 WHERE id = ?",
                      (hash_password(body.temp_password), user_id))
            c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        log_event(c, None, admin["id"], "user_updated", target["email"])
    return {"ok": True}


# ---------------------------------------------------------------- command line

def _ask_password():
    while True:
        pw = getpass.getpass("Password (at least %d characters, hidden as you type): " % MIN_PASSWORD)
        problem = password_problem(pw)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Type it again: ") != pw:
            print("They don't match. Try again.")
            continue
        return pw


def _cli():
    ensure_schema()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "create-admin":
        name = input("Your name: ").strip()
        email = input("Your email: ").strip().lower()
        if not name or not EMAIL_RE.match(email):
            sys.exit("A name and a valid email are needed.")
        pw = _ask_password()
        with conn() as c:
            try:
                c.execute("INSERT INTO users (name, email, role, password_hash, must_change) VALUES (?,?,?,?,0)",
                          (name, email, "admin", hash_password(pw)))
            except db.IntegrityError:
                sys.exit("That email already has an account. Use: python crm.py reset-password " + email)
        print(f"Admin account created for {email}. Open http://127.0.0.1:8000/login to sign in.")
    elif cmd == "reset-password" and len(sys.argv) > 2:
        email = sys.argv[2].strip().lower()
        pw = _ask_password()
        with conn() as c:
            cur = c.execute("UPDATE users SET password_hash = ?, must_change = 0, active = 1 WHERE email = ?",
                            (hash_password(pw), email))
            if cur.rowcount == 0:
                sys.exit("No account with that email.")
            uid = c.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]
            c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
        print("Password updated. Any old sign-ins for that account were ended.")
    elif cmd == "list-users":
        with conn() as c:
            for r in c.execute("SELECT name, email, role, active FROM users ORDER BY role, name"):
                print(f"{r['role']:6}  {'active' if r['active'] else 'off   '}  {r['name']}  <{r['email']}>")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
