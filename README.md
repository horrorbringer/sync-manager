# Database Sync Manager

A conservative Flask MVP for one-way MySQL synchronization. It supports authentication, RBAC, encrypted database credentials, connection testing, table discovery, schema checks, dry runs, batch inserts, job history, and audit logs.

It intentionally does not implement bidirectional synchronization, deletion propagation, automatic conflict resolution, or automatic schema changes.

The safest default sync mode is **Add new records only**. Use that when you want to insert missing rows without changing existing target data. Switch to **Add new records and update existing ones** only when the source should overwrite rows that already exist in the target.

## Core behavior

- Sync is one-way from a source MySQL database to a target MySQL database.
- `Validate` checks selected tables without writing data.
- `Run synchronization` validates first, then writes data.
- `Bulk sync all tables` discovers every table, orders them by dependency, and runs them in bulk.
- Parent tables are synchronized before child tables when dependencies are known.
- Cyclic foreign key groups run in cycle-safe mode instead of being blocked.
- Empty tables are skipped automatically.
- In `Add new records only` mode, tables with no new rows are skipped.
- Foreign keys are copied directly by default. Use an explicit mapping rule only when source and target keys differ.
- `inline` execution runs inside the Flask process.
- `celery` execution queues jobs in the background through Redis.

## Operational notes

- Job details show inserted, updated, skipped, and dropped counts.
- Dropped rows mean the sync tried to process them, but the database or remap logic rejected them.
- A clean sync is not guaranteed just because validation passed; target constraints can still reject rows at write time.
- The job page keeps the detailed counts and dropped-row reasons, while the dashboard stays compact.
- The advanced foreign-key mapping UI is hidden by default and can be enabled when needed.

## SWOT

**Strengths**

- Clear one-way sync model with conservative defaults.
- Dependency ordering reduces parent/child sync mistakes.
- Cycle-safe execution keeps larger schemas moving.
- Strong job history and validation visibility.

**Weaknesses**

- Insert-only mode can look successful in validation but still drop rows at write time if target constraints reject them.
- Foreign key mapping is still advanced and easy to misunderstand.
- Inline execution can block the request for long jobs.

**Opportunities**

- Add a first-class FK mapping rule workflow for power users.
- Improve per-row drop explanations and retry guidance.
- Add preset sync modes for speed, safety, and bulk migrations.
- Add more compact job summaries and progress feedback for long runs.

**Threats**

- Incorrect source/target connection selection can make sync results look wrong.
- Complex schemas with many foreign keys increase the chance of drops or cyclic behavior.
- Large bulk syncs can be slow in inline mode and may feel unresponsive without Celery.

## Local setup

Python 3.9 or newer is supported.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
flask --app run.py generate-encryption-key
```

Put the generated value in `.env` as `FLASK_CREDENTIAL_ENCRYPTION_KEY`. The value itself normally contains 44 characters and must not receive another `FLASK_` prefix. Also set `FLASK_SECRET_KEY`.

```bash
flask --app run.py init-db
flask --app run.py create-user
flask --app run.py run --debug
```

The default `FLASK_SYNC_EXECUTION_MODE=inline` runs synchronization in the Flask process and does not require Redis. The HTTP request remains open until the synchronization finishes, so this mode is intended for local use and smaller tables.

To run jobs in the background, set `FLASK_SYNC_EXECUTION_MODE=celery`, configure the Redis URLs, then start Redis, Celery, and Flask in separate terminals:

```bash
redis-server
celery -A celery_worker.celery_app worker --loglevel=INFO --concurrency=1
flask --app run.py run --debug
```

For production, run behind TLS with Gunicorn and Nginx. Source and target database accounts should receive only `SELECT` access on sources and only the required `SELECT`, `INSERT`, and `UPDATE` access on targets.

## Background execution

Synchronization supports two execution modes:

- `inline` is the default and has no Redis dependency. It is suitable for local use and smaller jobs.
- `celery` queues jobs through Redis so long-running synchronization does not block HTTP requests. Use this mode for production and run one worker process until distributed locking is added.

For multi-process production use, configure the management database in `FLASK_SQLALCHEMY_DATABASE_URI` as MySQL rather than SQLite.
