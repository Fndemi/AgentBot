# PostgreSQL switch: status report

Last updated: 2026-10-22, after commit `6edaf1c` ("Support PostgreSQL alongside SQLite (DATABASE_URL)").

**Summary:** the code is finished, tested and pushed. The bot and dashboard work on PostgreSQL or SQLite, chosen by one setting, `DATABASE_URL`. It isn't set yet, so everything still runs on `leads.db`. What remains is setting up the real PostgreSQL on the server and copying the data into it.

## 1. Files changed or created

All committed in `6edaf1c` and pushed to `main` and `crm` on GitHub.

| File | What changed |
|---|---|
| `db.py` | Picks PostgreSQL when `DATABASE_URL` is set, otherwise SQLite. Adds a PostgreSQL connection that works like the SQLite one: it translates `?` placeholders and SQLite's time functions, and uses a lock that does the job of `BEGIN IMMEDIATE`. The six `INSERT OR IGNORE` became `ON CONFLICT DO NOTHING`, `lastrowid` became `RETURNING id`, and the `json_extract`/`instr` queries and the bounce-check grouping were rewritten so both databases accept them. |
| `config.py` | New `DATABASE_URL` setting, empty by default. |
| `crm.py` | Uses `db.conn()`, `db.columns()` and `db.IntegrityError`. Creates the bot's tables before its own, so an empty PostgreSQL starts cleanly. The report's segments break ties by name. |
| `web.py` | Uses `db.conn()` and `db.columns()`. The industry list breaks ties by name. |
| `web_extra.py` | Uses `db.conn()` and `db.columns()`. |
| `dashboard.py` | The Streamlit page builds its tables from rows instead of `pd.read_sql` with a SQLite connection. |
| `inbox.py` | Catches errors from either database (`db.Error`). |
| `targeting.py` | `lastrowid` became `RETURNING id`. One connection that was never closed is now closed. |
| `websearch.py` | The upsert says `search_usage.calls`, which PostgreSQL requires. |
| `migrate_to_postgres.py` (new) | Copies `leads.db` into PostgreSQL with every id kept, moves the id counters past them, refuses if the target already has data, and prints row counts. |
| `requirements.txt` | Adds `psycopg[binary]`. |
| `.env.example` | Commented `# DATABASE_URL=` line with an explanation. |
| `tests/conftest.py` | Tests never use the real database from `.env`. With `TEST_DATABASE_URL` set, each test gets its own empty PostgreSQL schema. |
| `tests/test_database.py` (new) | The CRM starts on an empty database, and SQLite-style SQL is translated correctly for PostgreSQL. |
| `tests/test_dry_run.py` | The "nothing changed" check reads tables through `db`, not SQLite's dump. |
| `tests/test_send_safety.py` | The second-process lock test also passes `DATABASE_URL`. |

Also changed, but not in git: `.env.server` gained the same commented `# DATABASE_URL=` line (the file is ignored by git on purpose).

## 2. PostgreSQL

- **Installed as a system service:** no. Only the `psql` client is installed on the laptop.
- **What is running:** a test container, `leadbot-pg-test` (PostgreSQL 16.14), reachable only from the laptop on port 55432, with a database called `leadbot`. It is for testing only.
- **Production:** not set up yet.

## 3. `DATABASE_URL` in `.env`

**No.** `.env.server` and `.env.example` have it as a commented, empty line.

## 4. What each part uses right now

**All SQLite.** Nothing is mixed. Every part opens the database through `db.conn()`, so the one setting moves them all together.

| Part | Now | After `DATABASE_URL` is set |
|---|---|---|
| Bot sending | SQLite | PostgreSQL |
| Inbox check | SQLite | PostgreSQL |
| Dashboard | SQLite | PostgreSQL |
| CRM | SQLite | PostgreSQL |
| `targeting.py` | SQLite | PostgreSQL |

## 5. Alembic or schema file

**There is no Alembic and no `.sql` file.** The tables are created by the code itself with `CREATE TABLE IF NOT EXISTS` (in `db.py`, `crm.py`, `targeting.py` and `websearch.py`), adjusted automatically for PostgreSQL. Starting the bot or the dashboard creates any missing tables, on an empty PostgreSQL too. This has run only on the test container so far.

## 6. Data copied to PostgreSQL

**Only into the test container.** Every value in every row was compared and matched:

| Table | leads.db | PostgreSQL (test) |
|---|---|---|
| leads | 400 | 400 |
| users | 2 | 2 |
| sessions | 1 | 1 |
| sent | 115 | 115 |
| replies | 15 | 15 |
| alert_outbox | 3 | 3 |
| lead_events | 1,365 | 1,365 |
| skipped | 413 | 413 |
| suppression | 9 | 9 |
| send_claims | 0 | 0 |
| locks | 0 | 0 |
| crm_messages | 0 | 0 |
| crm_notes | 0 | 0 |
| crm_events | 8 | 8 |
| import_runs | 1 | 1 |
| search_usage | 1 | 1 |

The bot's reads give the same results on both databases: bounce rate, today's sends, stats, follow-ups due, the batch plan, top scores, the CRM lead list, a lead's email history, the dashboard data and the CRM report.

## 7. `leads.db` unchanged

**Yes.** It is byte-for-byte identical to the copy in `leadbot-sqlite-backup`.

## 8. Does the dashboard start without errors?

| Database | Result |
|---|---|
| SQLite (a copy of `leads.db`) | Yes: sign-in page 200, `/` redirects to sign-in (303), no errors |
| PostgreSQL with data | Yes: the same results |
| Brand-new, empty PostgreSQL | Yes: the same results, and all 14 tables are created |

**Tests:** all 288 pass on PostgreSQL and on SQLite.

## 9. Done and left

**Done**

- The code supports PostgreSQL, chosen by `DATABASE_URL`.
- The dashboard and CRM start on an empty PostgreSQL.
- `psycopg[binary]` is in `requirements.txt`.
- The two sorts have tie-breakers, so both databases give the same order.
- `.env.server` and `.env.example` have a commented `DATABASE_URL` line.
- Committed and pushed (`6edaf1c`).

**Left**

1. **Set up the real PostgreSQL** on or near the server, with a database and a user for LeadBot.
2. **Put its address in the server's `.env`** as `DATABASE_URL=postgresql://user:password@host:5432/dbname`, and run `pip install -r requirements.txt` there.
3. **Copy the data:** with the laptop bot stopped, run `python migrate_to_postgres.py leads.db` on the server, using the latest `leads.db`, and check that the row counts it prints match.
4. **Restart the bot and the dashboard on the server**, and run the bot in one place only from then on.
5. **Remove the test container** (`docker rm -f leadbot-pg-test`) once it's no longer needed.
6. **Speed, worth watching:** the code opens a new database connection for each operation. That's fine on the same machine, but noticeably slower if PostgreSQL is on another server, and it could be improved later.

## How to run things

- Tests on SQLite: `python -m pytest`
- Tests on PostgreSQL: `TEST_DATABASE_URL=postgresql://user:password@host:5432/dbname python -m pytest` (each test makes and drops its own schema)
- Copy the data: set `DATABASE_URL` in `.env`, then `python migrate_to_postgres.py leads.db`
- Go back to SQLite: remove or empty `DATABASE_URL` in `.env` and restart the bot and the dashboard
