"""
Cashflow Centre — Recurring Payments Migration
==================================================
Adds the new `cashflow_recurring_payment` table (SIP/EMI/subscription
reminders for the "upcoming 30 days" dashboard widget). This is a
brand-new table, not a reshape of an existing one, so this migration
is simple: db.create_all() only creates tables that don't already
exist and never touches ones that do, so it's always safe to re-run.

Run from project root: py cashflow_centre/migrate_recurring_payments.py
"""

import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        from sqlalchemy import inspect

        print("=" * 60)
        print("Cashflow Centre — Recurring Payments Migration")
        print("=" * 60)

        inspector = inspect(db.engine)
        if "cashflow_recurring_payment" in inspector.get_table_names():
            print("\n✓ cashflow_recurring_payment already exists — nothing to do.")
            return True

        print("\nCreating cashflow_recurring_payment table...")
        db.create_all()

        inspector = inspect(db.engine)
        if "cashflow_recurring_payment" not in inspector.get_table_names():
            print("  ✗ Table was not created. Aborting.")
            return False

        print("  ✓ cashflow_recurring_payment created")
        print(f"\n{'=' * 60}")
        print("Migration complete. Add your EMIs/SIPs/subscriptions from")
        print("the new Recurring Payments page under Cashflow.")
        print("=" * 60)
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
