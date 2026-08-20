"""
Wealth — Phase I Migration
=============================
Adds: `source` column on the existing wealth_snapshot table, and the
new wealth_snapshot_log table (Automatic Wealth Snapshots feature).

Follows this project's established per-phase migration convention
(migrate_phase_f.py, migrate_phase_h.py). Metadata-only (Section 55):
no existing snapshot's date, financial values, or ownership figures
are touched — only a new column is added, backfilled to 'manual' for
every pre-existing row (Section 54 — it would be factually wrong to
label historical snapshots as 'automatic', since Phase I is the
first phase that can ever create one).

Safe to run multiple times — checks for the column/table before
trying to add either.

Run from project root: py wealth/migrate_phase_i.py

ROLLBACK:
    py -c "import sqlite3; conn = sqlite3.connect('instance/mywealthlens.db'); conn.execute('DROP TABLE IF EXISTS wealth_snapshot_log'); conn.commit(); conn.close(); print('wealth_snapshot_log dropped')"

  (The `source` column on wealth_snapshot cannot be cleanly dropped
  in SQLite without rebuilding the table. If you need to fully
  revert, restore from a pre-migration backup instead — see Step 2
  below, which creates one automatically before making any change.)
"""

import sys, os, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Wealth Module — Phase I Migration")
        print("Automatic Wealth Snapshots")
        print("=" * 60)

        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)

        # ── Step 1: safety backup copy of the whole DB file ──
        # Same lightweight approach as migrate_phase_h.py — this
        # project has no other backup mechanism (Section 21 of the
        # Phase H spec, still true here).
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        if db_uri.startswith("sqlite:///"):
            db_path = db_uri.replace("sqlite:///", "", 1)
            if not os.path.isabs(db_path):
                db_path = os.path.join(app.instance_path, db_path)
            if os.path.exists(db_path):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(
                    os.path.dirname(db_path),
                    f"mywealthlens_pre_phase_i_backup_{ts}.db")
                shutil.copy2(db_path, backup_path)
                print(f"\nStep 1: Backup copy written to:\n  {backup_path}")
            else:
                print("\nStep 1: WARNING — could not locate the .db file to "
                      "back up. Proceeding without a backup copy.")
        else:
            print("\nStep 1: Non-SQLite database — back up manually before "
                  "proceeding if this matters to you.")

        # ── Step 2: add `source` column to wealth_snapshot ──
        print("\nStep 2: Checking wealth_snapshot.source column...")
        existing_columns = {c["name"] for c in inspector.get_columns("wealth_snapshot")}
        if "source" in existing_columns:
            print("  'source' column already present — skipping.")
        else:
            db.session.execute(text(
                "ALTER TABLE wealth_snapshot "
                "ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'manual'"))
            db.session.commit()
            print("  ✓ 'source' column added, existing rows backfilled to 'manual'.")

        # ── Step 3: create wealth_snapshot_log table ──
        print("\nStep 3: Creating wealth_snapshot_log table (if not present)...")
        tables_before = set(inspect(db.engine).get_table_names())
        db.create_all()  # only creates tables that don't exist yet
        tables_after = set(inspect(db.engine).get_table_names())

        if "wealth_snapshot_log" in tables_after:
            print("  ✓ wealth_snapshot_log present.")
        else:
            print("  ✗ wealth_snapshot_log missing after create_all() — unexpected.")
            print("\n✗ Migration failed. Check the errors above.")
            return False

        newly_created = tables_after - tables_before
        unexpected = newly_created - {"wealth_snapshot_log"}
        if unexpected:
            print(f"  NOTE: create_all() also created: {unexpected} "
                  "(unexpected extra tables — investigate if this "
                  "wasn't intended).")

        # ── Step 4: verify existing snapshot data untouched ──
        print("\nStep 4: Verifying existing wealth_snapshot data is unchanged...")
        result = db.session.execute(text(
            "SELECT COUNT(*), COUNT(DISTINCT source) FROM wealth_snapshot"))
        row_count, distinct_sources = result.fetchone()
        print(f"  wealth_snapshot row count: {row_count}")
        result2 = db.session.execute(text(
            "SELECT source, COUNT(*) FROM wealth_snapshot GROUP BY source"))
        for source_val, count in result2.fetchall():
            print(f"    source='{source_val}': {count} row(s)")

        # ── Step 5: verify unrelated tables untouched ──
        print("\nStep 5: Verifying other Wealth/Insurance/Retirement tables intact...")
        must_survive = [
            "wealth_asset", "wealth_liability", "wealth_document",
            "wealth_value_snapshot", "insurance_policy",
            "retirement_scheme", "user",
        ]
        all_ok = True
        for t in must_survive:
            if t in tables_after:
                print(f"  ✓ {t}")
            else:
                print(f"  ✗ {t} — MISSING (unexpected!)")
                all_ok = False

        if not all_ok:
            print("\n✗ Migration made its intended changes, but an "
                  "unrelated table is unexpectedly missing. Investigate "
                  "before trusting this database.")
            return False

        print("\n" + "=" * 60)
        print("✓ Phase I migration complete.")
        print("  wealth_snapshot.source column added (existing rows = 'manual').")
        print("  wealth_snapshot_log table created.")
        print("  All other tables intact.")
        print("=" * 60)
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
