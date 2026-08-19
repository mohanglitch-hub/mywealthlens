"""
Wealth — Phase H Migration
=============================
Retires the legacy Assets module: drops the `asset` table only.

Follows the same per-phase migration convention as
wealth/migrate_phase_f.py and wealth/migrate_phase_g.py.

CONTEXT (see Phase H final report for the full audit):
  - The legacy `asset` table held 36 records total across 2 users.
  - User 1's 11 records (real financial data, ~₹21,00,498) were
    confirmed by the project owner as already re-entered elsewhere /
    not needed — NOT migrated into wealth_asset.
  - User 2's 25 records (mostly test/scratch data) were confirmed by
    the project owner as a test account — NOT migrated.
  - No other table has a foreign key into `asset` (confirmed by
    audit — the only reference was the now-removed
    User.assets relationship() in models.py). Dropping it does not
    cascade into or affect wealth_asset, wealth_liability,
    wealth_snapshot, wealth_document, net_worth_history, or any
    Insurance/Retirement table.

SAFETY (Section 21/22 of Phase H spec): this project has no existing
backup mechanism, and Section 21 explicitly says not to invent a
complex one. This script does the simplest possible safety net for a
SQLite project — it copies the whole .db file to a timestamped
backup path in `instance/` immediately before the DROP TABLE, so the
destructive step is always preceded by a full point-in-time copy.
This is NOT a substitute for your own backup discipline — if you
keep backups elsewhere, this is just an extra safety margin.

Run from project root: py wealth/migrate_phase_h.py

ROLLBACK: if you need the table back (e.g. you change your mind
about the discarded data), restore from the timestamped backup file
this script prints the path to, or recreate the empty table:

    py -c "from app import app, db; import sqlalchemy as sa;
    app.app_context().push();
    db.session.execute(sa.text('''CREATE TABLE asset (
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
        category VARCHAR(50) NOT NULL, name VARCHAR(200),
        grams FLOAT, price_per_gram FLOAT, sq_ft FLOAT,
        institution VARCHAR(200), value FLOAT NOT NULL DEFAULT 0,
        created_at DATETIME, updated_at DATETIME,
        FOREIGN KEY(user_id) REFERENCES user(id))'''))"

    (this recreates an EMPTY table with the original schema — actual
    row data can only be recovered from the timestamped .db backup)
"""

import sys, os, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Wealth Module — Phase H Migration")
        print("Retire Legacy Assets Module")
        print("=" * 60)

        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())

        if "asset" not in tables:
            print("\n  'asset' table not present — nothing to do.")
            print("  (Already migrated, or this is a fresh database.)")
            print("\n✓ Migration complete (no-op).")
            return True

        # ── Step 1: row count sanity check, printed for the record ──
        result = db.session.execute(text("SELECT COUNT(*) FROM asset"))
        row_count = result.scalar()
        print(f"\nStep 1: Found 'asset' table with {row_count} row(s).")

        # ── Step 2: safety backup copy of the whole DB file ──
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        if db_uri.startswith("sqlite:///"):
            db_path = db_uri.replace("sqlite:///", "", 1)
            if not os.path.isabs(db_path):
                # Flask-SQLAlchemy resolves relative sqlite:/// URIs
                # relative to app.instance_path, not the process cwd.
                db_path = os.path.join(app.instance_path, db_path)
            if os.path.exists(db_path):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(
                    os.path.dirname(db_path),
                    f"mywealthlens_pre_phase_h_backup_{ts}.db")
                shutil.copy2(db_path, backup_path)
                print(f"Step 2: Backup copy written to:\n  {backup_path}")
            else:
                print("Step 2: WARNING — could not locate the .db file to "
                      "back up. Proceeding without a backup copy.")
        else:
            print("Step 2: Non-SQLite database detected — this script only "
                  "knows how to back up SQLite files. Ensure you have your "
                  "own backup before continuing.")
            confirm = input("Type 'yes' to proceed without an automatic "
                            "backup: ").strip().lower()
            if confirm != "yes":
                print("\nMigration aborted by user.")
                return False

        # ── Step 3: drop the table ──
        print("\nStep 3: Dropping 'asset' table...")
        db.session.execute(text("DROP TABLE IF EXISTS asset"))
        db.session.commit()

        # ── Step 4: verify ──
        inspector_after = inspect(db.engine)
        tables_after = set(inspector_after.get_table_names())
        if "asset" in tables_after:
            print("  ✗ 'asset' table still present — DROP failed.")
            print("\n✗ Migration failed. Check the errors above.")
            return False
        print("  ✓ 'asset' table removed.")

        # ── Step 5: confirm untouched tables are intact ──
        print("\nStep 5: Verifying untouched Wealth/Insurance/Retirement "
              "tables are intact...")
        must_survive = [
            "wealth_asset", "wealth_liability", "wealth_snapshot",
            "wealth_document", "wealth_value_snapshot", "net_worth_history",
            "insurance_policy", "retirement_scheme", "user",
        ]
        all_ok = True
        for t in must_survive:
            if t in tables_after:
                print(f"  ✓ {t}")
            else:
                print(f"  ✗ {t} — MISSING (unexpected!)")
                all_ok = False

        if not all_ok:
            print("\n✗ Migration completed the drop, but an unrelated "
                  "table is unexpectedly missing. Investigate before "
                  "trusting this database.")
            return False

        print("\n" + "=" * 60)
        print("✓ Phase H migration complete.")
        print("  Legacy 'asset' table removed.")
        print("  All Wealth/Insurance/Retirement tables intact.")
        print("=" * 60)
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
