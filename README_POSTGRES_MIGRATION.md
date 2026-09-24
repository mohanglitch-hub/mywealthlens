# Moving to Postgres — one-time setup

Moving to Postgres itself is optional and **nothing breaks if you skip it** —
with no `DATABASE_URL` set, the app keeps using your existing
`instance/mywealthlens.db` exactly as before. This is step 1 of the
production-readiness plan we agreed on (Postgres + Alembic → encryption
design → multi-tenant → responsive UI → desktop).

One thing is **not** optional and needs no action from you: schema
management itself now goes through Alembic on SQLite too, not just Postgres.
The first time you start the app after pulling this change, it will detect
your existing database, print a line saying it's stamping it at the current
schema version, and continue exactly as before — no tables touched, no data
changed, nothing for you to do. Every restart after that is a non-event,
same as it always was with `db.create_all()`. This just means future schema
changes (especially the encryption work) now go through real, version-
tracked migrations instead of plain scripts.

## 1. Install Postgres on Windows

1. Download the installer from https://www.postgresql.org/download/windows/
   (the EDB installer is the standard one).
2. Run it. When it asks:
   - Set a password for the `postgres` superuser — write it down, you'll need it below.
   - Keep the default port `5432`.
3. Once installed, open **pgAdmin** (installed alongside Postgres) or just use
   the `psql` command line, and create a database:
   ```sql
   CREATE DATABASE mywealthlens;
   ```

## 2. Point the app at Postgres

In your PowerShell terminal, **inside your `.venv`**:

```powershell
pip install -r requirements.txt
```

This installs `psycopg2-binary` and `Flask-Migrate`, both newly added.

Then set the connection string for this session (replace `yourpassword`):

```powershell
$env:DATABASE_URL = "postgresql://postgres:yourpassword@localhost:5432/mywealthlens"
```

This only lasts for the current PowerShell window. To make it permanent,
set it as a Windows environment variable (System Properties → Environment
Variables), or add it to a `.env`-loading step if you set one up later.

## 3. Create the schema

```powershell
$env:FLASK_APP = "app.py"
flask db upgrade
```

This applies the baseline migration (already committed in `migrations/`)
and creates all 32 tables in Postgres, matching your current SQLite schema
exactly — verified in testing with zero drift.

## 4. Copy your existing data across

```powershell
py migrate_sqlite_to_postgres.py --dry-run
```

Check the row counts look right, then run it for real:

```powershell
py migrate_sqlite_to_postgres.py
```

Your original `instance/mywealthlens.db` is never touched — this only reads
from it. If anything looks wrong afterwards, just unset `DATABASE_URL` and
you're back on SQLite with your original data completely intact.

## 5. Restart the app

```powershell
py app.py
```

It'll now be running on Postgres. Everything — Wealth, Insurance, Retirement,
Family, Cashflow — was tested end-to-end against Postgres in the same
session this was built, so this should be a non-event.

## Going forward: schema changes now go through Alembic

Schema management now goes through Alembic on **every** database, not just
Postgres — `db.create_all()` and the old hand-written `migrate_xxx.py`
scripts are fully retired. Any future change to a model (a new column, a new
table) should be captured as a migration:

```powershell
flask db migrate -m "describe the change"
flask db upgrade
```

Always read the auto-generated migration file in `migrations/versions/`
before running `upgrade` — autogenerate is good but not perfect, especially
around renames (it may see a rename as "drop one column, add another").

## What this pass did NOT do

This was scoped deliberately to infrastructure only — no encryption yet.
Account numbers, MF folio numbers, and Document Vault files are still stored
in plain text for now. That's the next piece of this phase, and we agreed
on the design direction (a separate recovery passphrase, not your login
password, so a forgotten login doesn't mean permanently lost data) but
haven't built it yet.
