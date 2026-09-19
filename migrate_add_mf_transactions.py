"""
Migration Script — Upload CAS rebuild: transaction-level MF history
=====================================================================
Two changes, both needed for real per-scheme/portfolio XIRR:

1. New columns on the existing `mutual_fund` table:
     isin, amfi_code, invested, xirr
   (a NEW COLUMN on a table that already exists needs an explicit
   ALTER TABLE — db.create_all() never modifies existing tables,
   only creates missing ones, per this project's established
   convention.)

2. The new `mutual_fund_transaction` table. This one IS brand new,
   so db.create_all() (run automatically on every server start)
   creates it on its own — nothing to do here. This script still
   reports on it below so a single run tells you the full picture.

Safe to run multiple times — checks what already exists before
changing anything.

Run from project root: py migrate_add_mf_transactions.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Upload CAS rebuild — transaction-level MF history")
        print("=" * 60)

        new_columns = [
            ("isin",      "VARCHAR(20)"),
            ("amfi_code", "VARCHAR(20)"),
            ("invested",  "FLOAT"),
            ("xirr",      "FLOAT"),
        ]

        with db.engine.connect() as conn:
            existing_cols = {
                row[1] for row in
                conn.execute(db.text("PRAGMA table_info(mutual_fund)"))
            }
            for col_name, col_type in new_columns:
                if col_name in existing_cols:
                    print(f"  ✓ mutual_fund.{col_name} (already present)")
                else:
                    conn.execute(db.text(
                        f"ALTER TABLE mutual_fund ADD COLUMN {col_name} {col_type}"
                    ))
                    conn.commit()
                    print(f"  ✓ mutual_fund.{col_name} (added)")

        # Brand-new table — db.create_all() (called on every app start)
        # already creates this if missing. Just confirm and report.
        db.create_all()
        with db.engine.connect() as conn:
            tables = {
                row[0] for row in
                conn.execute(db.text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ))
            }
        if "mutual_fund_transaction" in tables:
            print("  ✓ mutual_fund_transaction table (present)")
        else:
            print("  ✗ mutual_fund_transaction table MISSING — check models.py import")

        print(f"\n{'=' * 60}")
        print("Migration complete.")
        print("\nNext steps:")
        print("  1. Restart your Flask server")
        print("  2. Re-upload your CAS as the DETAILED statement type")
        print("     (Summary statements have no transaction history and")
        print("     will import holdings only, same as before)")
        print(f"{'=' * 60}")
        print("\n✅ Migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)