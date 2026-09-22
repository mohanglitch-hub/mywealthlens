"""
Cashflow Centre — Migration Script
=======================================
Brand-new module — creates the two new tables (cashflow_transaction,
cashflow_budget). No old table to migrate data from. Safe to run
multiple times.

Run from project root: py cashflow_centre/migrate.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Cashflow Centre — Migration")
        print("=" * 60)

        print("\nCreating new tables...")
        db.create_all()

        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        required_tables = ["cashflow_transaction", "cashflow_budget"]

        all_ok = True
        for t in required_tables:
            if t in tables:
                print(f"  ✓ {t}")
            else:
                print(f"  ✗ {t} — MISSING")
                all_ok = False

        if not all_ok:
            print("\n✗ Some tables failed to create. Check the errors above.")
            return False

        print(f"\n{'=' * 60}")
        print("Migration complete.")
        print("\nNext steps:")
        print("  1. Restart your Flask server")
        print(f"{'=' * 60}")
        print("\n✅ Migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
