"""The storage layer on either database: the CRM can start on an empty one, and SQLite-style
SQL is rewritten for PostgreSQL."""
import config
import crm
import db


def test_crm_starts_on_an_empty_database(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "empty.db"))
    crm.ensure_schema()  # its tables refer to leads: the bot's tables come first
    with db.conn() as c:
        assert "email" in db.columns(c, "leads")
        assert "password_hash" in db.columns(c, "users")


def test_sqlite_style_sql_for_postgresql():
    assert db.pg_sql("SELECT 1 FROM t WHERE a=? AND b LIKE '%:%'") == "SELECT 1 FROM t WHERE a=%s AND b LIKE '%%:%%'"
    assert db.pg_sql("SELECT 1 WHERE note = 'why?' AND a = ?") == "SELECT 1 WHERE note = 'why?' AND a = %s"
    assert "CAST(%s AS interval)" in db.pg_sql("WHERE x >= datetime('now', ?)")
    assert "CAST('-8 days' AS interval)" in db.pg_sql("SET r = datetime('now', '-8 days')", with_params=False)
    assert db.pg_sql("VALUES (CURRENT_TIMESTAMP)", with_params=False) == f"VALUES ({db.UTC_NOW})"
