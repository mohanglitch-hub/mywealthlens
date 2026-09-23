"""
One-Time Data Migration — SQLite → Postgres
===============================================
Copies every row from your existing local SQLite database
(instance/mywealthlens.db) into the new Postgres database, table by
table, in foreign-key-safe order. This is a ONE-TIME cutover step —
run it once, verify your data in Postgres, then start pointing the
app at DATABASE_URL going forward. Your original SQLite file is never
modified or deleted; it's read-only as far as this script is
concerned, so it stays as a fallback/backup automatically.

BEFORE running this:
  1. Set DATABASE_URL to your Postgres connection string.
  2. Run `flask db upgrade` first, so the Postgres schema exists
     (this script inserts data, it does not create tables — see
     README_POSTGRES_MIGRATION.md).

Safe to run more than once in the sense that it always tells you
clearly what it found and refuses to silently duplicate rows: if the
target already has data in a table, that table is skipped unless you
pass --force (which truncates and re-copies it). Row IDs are
preserved exactly as they are in SQLite (this matters — plenty of
foreign keys reference them), and every Postgres auto-increment
sequence is corrected afterwards so new rows created after the
cutover don't collide with the copied ones.

Run from project root, with DATABASE_URL pointing at Postgres:
    py migrate_sqlite_to_postgres.py            (copies everything)
    py migrate_sqlite_to_postgres.py --force     (wipes + re-copies)
    py migrate_sqlite_to_postgres.py --dry-run   (reports counts only, writes nothing)
"""

import sys, os, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def run_migration(force=False, dry_run=False):
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url or database_url.startswith("sqlite"):
        print("=" * 60)
        print("DATABASE_URL is not set to a Postgres connection string.")
        print("Set it first, e.g. (PowerShell):")
        print('  $env:DATABASE_URL = "postgresql://user:pass@localhost:5432/mywealthlens"')
        print("=" * 60)
        return False

    from app import app, db
    from sqlalchemy import create_engine, inspect, text, MetaData, Table

    sqlite_path = os.path.join(app.instance_path, "mywealthlens.db")
    if not os.path.exists(sqlite_path):
        print(f"\n✗ No SQLite database found at {sqlite_path} — nothing to migrate.")
        return False

    print("=" * 60)
    print("MyWealthLens — SQLite → Postgres Data Migration")
    print("=" * 60)
    print(f"\nSource (read-only): {sqlite_path}")
    print(f"Target: {database_url.split('@')[-1] if '@' in database_url else database_url}")

    source_engine = create_engine(f"sqlite:///{sqlite_path}")

    with app.app_context():
        target_inspector = inspect(db.engine)
        if "alembic_version" not in target_inspector.get_table_names():
            print("\n✗ The target Postgres database has no 'alembic_version' table —")
            print("  run `flask db upgrade` first to create the schema, then re-run this.")
            return False

        # db.metadata.sorted_tables is already in foreign-key-safe order
        # (parents before children) — exactly the order rows must be
        # copied in so FK constraints never fail mid-migration.
        tables = list(db.metadata.sorted_tables)
        source_meta = MetaData()

        print(f"\nFound {len(tables)} tables to check.\n")

        total_copied = 0
        skipped_existing = []

        for table in tables:
            table_name = table.name
            source_inspector = inspect(source_engine)
            if table_name not in source_inspector.get_table_names():
                continue  # a table that exists only in the new schema (nothing to copy)

            source_table = Table(table_name, source_meta, autoload_with=source_engine)

            with source_engine.connect() as sconn:
                rows = [dict(r._mapping) for r in sconn.execute(source_table.select()).fetchall()]

            if not rows:
                continue

            with db.engine.connect() as tconn:
                existing_count = tconn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar()

            if existing_count and not force:
                skipped_existing.append((table_name, existing_count))
                continue

            if dry_run:
                print(f"  [dry-run] {table_name}: would copy {len(rows)} row(s)")
                total_copied += len(rows)
                continue

            with db.engine.begin() as tconn:
                if existing_count and force:
                    tconn.execute(text(f'TRUNCATE TABLE "{table_name}" CASCADE'))

                # Coerce SQLite's loose 0/1 integers into real Python
                # booleans for any column the model declares as Boolean —
                # Postgres's boolean type rejects a bare integer.
                bool_cols = {c.name for c in table.columns if str(c.type) == "BOOLEAN"}
                if bool_cols:
                    for row in rows:
                        for col in bool_cols:
                            if col in row and row[col] is not None:
                                row[col] = bool(row[col])

                tconn.execute(table.insert(), rows)

                # Reset the SERIAL sequence past the highest copied id,
                # so the next INSERT from the app doesn't collide with
                # a migrated row.
                if "id" in table.columns:
                    tconn.execute(text(
                        f'SELECT setval(pg_get_serial_sequence(\'"{table_name}"\', \'id\'), '
                        f'GREATEST((SELECT COALESCE(MAX(id), 0) FROM "{table_name}"), 1))'
                    ))

            print(f"  ✓ {table_name}: copied {len(rows)} row(s)")
            total_copied += len(rows)

        print(f"\n{'=' * 60}")
        if dry_run:
            print(f"Dry run complete — {total_copied} row(s) would be copied across "
                  f"{len([t for t in tables])} table(s) checked.")
        else:
            print(f"Migration complete — {total_copied} row(s) copied.")
        if skipped_existing:
            print("\nSkipped (target already has data — re-run with --force to wipe and re-copy):")
            for name, count in skipped_existing:
                print(f"  {name}: {count} existing row(s)")
        print("=" * 60)
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One-time SQLite -> Postgres data migration.")
    parser.add_argument("--force", action="store_true",
                         help="Truncate and re-copy any table that already has data in Postgres.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Report what would be copied without writing anything.")
    args = parser.parse_args()
    success = run_migration(force=args.force, dry_run=args.dry_run)
    sys.exit(0 if success else 1)
