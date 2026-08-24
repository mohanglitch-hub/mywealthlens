"""
Retirement Centre — Migration Script
=======================================
Removes the retirement_balance_snapshot table.

RetirementBalanceSnapshot was wired up early in this module's
development, then deliberately removed at the user's request:
balance changes and interest are now captured as real Contribution
rows instead of a separate balance-history log — confirmed not
needed, matching the same decision already made for Wealth
(WealthValueSnapshot exists there; the equivalent was explicitly
declined for Retirement).

The model class, its relationship, the get_balance_history() service
function, the validate_balance_update() validator, and the "Recent
Balance History" block in the PDF export have all already been
removed from the codebase. This script's only job is to drop the
actual database table left behind by that earlier development —
db.create_all() only ever creates tables for models that still
exist, it never drops ones that were removed from the code.

Safe to run multiple times — checks whether the table exists before
attempting to drop it. If the table has rows (from back when the
feature was live), their count is reported before the drop, purely
so you have a record of what existed; the data itself is not
recoverable after this runs.

Run from project root: py retirement_centre/migrate_remove_balance_snapshot.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Retirement Centre — Remove Balance Snapshot Table")
        print("=" * 60)

        with db.engine.connect() as conn:
            tables = {
                row[0] for row in
                conn.execute(db.text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ))
            }

            if "retirement_balance_snapshot" not in tables:
                print("\n  ℹ retirement_balance_snapshot table not found "
                      "— nothing to remove, already clean.")
                print(f"\n{'=' * 60}")
                print("✅ Migration complete (no-op)")
                print(f"{'=' * 60}")
                return True

            row_count = conn.execute(db.text(
                "SELECT COUNT(*) FROM retirement_balance_snapshot"
            )).scalar()
            print(f"\n  Found retirement_balance_snapshot table "
                  f"with {row_count} row(s).")

            print("\nStep 1: Dropping retirement_balance_snapshot...")
            conn.execute(db.text("DROP TABLE retirement_balance_snapshot"))
            conn.commit()
            print("  ✓ Table dropped")

        print(f"\n{'=' * 60}")
        print("Migration complete:")
        print("  ✓ retirement_balance_snapshot table removed")
        print("  ℹ No other tables touched")
        print(f"\nNext steps:")
        print(f"  1. Restart your Flask server")
        print(f"{'=' * 60}")
        print("\n✅ Migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)