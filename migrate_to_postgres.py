"""Copies the SQLite database into PostgreSQL, once, before switching DATABASE_URL on.

  python migrate_to_postgres.py [leads.db]

The target is DATABASE_URL from .env (it must be set). The tables are created there the
way the bot creates them, then every row is copied with its id, and the id counters are
moved past the copied ids. Nothing is copied if any table there already has rows, so it
can't mix two databases. The SQLite file is only read. Afterwards each table's row count
is compared and printed.
"""
import sqlite3
import sys

import config
import crm
import db
import targeting
import websearch

# Parents before the tables that refer to them (PostgreSQL checks every reference)
TABLES = ["leads", "users", "sessions", "sent", "replies", "alert_outbox", "lead_events",
          "skipped", "suppression", "send_claims", "locks", "crm_messages", "crm_notes",
          "crm_events", "import_runs", "search_usage"]
WITH_ID = {"leads", "users", "sent", "replies", "alert_outbox", "lead_events", "crm_messages",
           "crm_notes", "crm_events", "import_runs"}


def create_tables():
    db.init()
    crm.ensure_schema()
    with db.conn() as c:
        c.executescript(targeting.RUNS_SCHEMA)
        c.executescript(websearch.SCHEMA)


def copy(source, target, table):
    """Rows copied from one table, or None if the SQLite file doesn't have it."""
    if not source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return None
    cols = [r["name"] for r in source.execute(f"PRAGMA table_info({table})")]
    cols = [col for col in cols if col in db.columns(target, table)]
    rows = [tuple(r[col] for col in cols) for r in source.execute(f"SELECT * FROM {table} ORDER BY 1")]
    if rows:
        target.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           rows)
    if table in WITH_ID:
        target.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                       f" COALESCE((SELECT MAX(id) FROM {table}), 1),"
                       f" (SELECT MAX(id) FROM {table}) IS NOT NULL)")
    return len(rows)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else config.DB_PATH
    if not db.postgres():
        sys.exit("Set DATABASE_URL in .env to the PostgreSQL database to copy into.")
    create_tables()
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    with db.conn() as target:
        full = [t for t in TABLES if target.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()]
        if full:
            sys.exit(f"Nothing copied: these tables already have rows in PostgreSQL: {', '.join(full)}")
        copied = {t: copy(source, target, t) for t in TABLES}  # one transaction: all or nothing
    with db.conn() as target:
        print(f"{'table':14} {'SQLite':>7} {'PostgreSQL':>10}")
        for t, n in copied.items():
            if n is None:
                print(f"{t:14} {'-':>7} {'(not in SQLite)':>10}")
                continue
            there = target.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"{t:14} {n:>7} {there:>10}{'' if there == n else '   <-- MISMATCH'}")
    source.close()


if __name__ == "__main__":
    main()
