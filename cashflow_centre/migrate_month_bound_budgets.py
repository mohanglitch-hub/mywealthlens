"""
Cashflow Centre — Month-Bound Budgets Migration
==================================================
Budgets used to be one row per (user, category) — a single limit that
silently applied to every month forever. This migration reshapes
`cashflow_budget` to be one row per (user, category, year, month), so
a limit set for September doesn't quietly apply to October too.

The old UNIQUE constraint was on (user_id, category) only, which would
now incorrectly block budgeting the same category in two different
months. SQLite can't ALTER a UNIQUE constraint in place, so this
rebuilds the table:

  1. Rename the existing `cashflow_budget` table out of the way.
  2. Let db.create_all() create a fresh `cashflow_budget` with the new
     schema (year, month columns + the new composite unique index).
  3. Copy every existing row across, assigning it to the CURRENT IST
     calendar month (existing budgets had no month concept — they were
     "the" ongoing limit for that category, so the most faithful
     migration is to treat them as this month's limit; use the
     Budgets page's "Apply to future months" action afterwards to
     carry them forward).
  4. Drop the renamed old table.

Safe to run multiple times — skips step 1-4 if the old-shaped table
(no `year` column) isn't found, i.e. migration already ran.

Run from project root: py cashflow_centre/migrate_month_bound_budgets.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        from sqlalchemy import inspect, text
        from wealth.timezone_utils import today_ist

        print("=" * 60)
        print("Cashflow Centre — Month-Bound Budgets Migration")
        print("=" * 60)

        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        if "cashflow_budget" not in tables:
            print("\ncashflow_budget table not found — creating fresh schema.")
            db.create_all()
            print("✅ Migration complete (no existing data to migrate).")
            return True

        existing_cols = {c["name"] for c in inspector.get_columns("cashflow_budget")}
        if "year" in existing_cols and "month" in existing_cols:
            print("\n✓ cashflow_budget already has year/month columns — nothing to do.")
            return True

        print("\nStep 1: Backing up existing cashflow_budget rows...")
        with db.engine.connect() as conn:
            old_rows = [dict(r._mapping) for r in conn.execute(
                text("SELECT * FROM cashflow_budget")).fetchall()]
        print(f"  Found {len(old_rows)} existing budget row(s).")

        print("\nStep 2: Renaming old table out of the way...")
        with db.engine.connect() as conn:
            conn.execute(text("ALTER TABLE cashflow_budget RENAME TO cashflow_budget_old_premonth"))
            conn.commit()
        print("  ✓ cashflow_budget -> cashflow_budget_old_premonth")

        print("\nStep 3: Creating new month-bound cashflow_budget table...")
        db.create_all()
        inspector = inspect(db.engine)
        if "cashflow_budget" not in inspector.get_table_names():
            print("  ✗ New table was not created. Aborting.")
            return False
        print("  ✓ cashflow_budget (new schema)")

        print("\nStep 4: Migrating rows into the current IST month...")
        year, month = today_ist().year, today_ist().month
        from cashflow_centre.models import Budget
        migrated = 0
        for row in old_rows:
            b = Budget(
                user_id=row["user_id"], category=row["category"],
                year=year, month=month,
                monthly_limit=row["monthly_limit"],
                created_at=row.get("created_at"), updated_at=row.get("updated_at"),
            )
            db.session.add(b)
            migrated += 1
        db.session.commit()
        print(f"  ✓ Migrated {migrated} row(s) into {year}-{month:02d}")

        print("\nStep 5: Dropping old table...")
        with db.engine.connect() as conn:
            conn.execute(text("DROP TABLE cashflow_budget_old_premonth"))
            conn.commit()
        print("  ✓ cashflow_budget_old_premonth dropped")

        print(f"\n{'=' * 60}")
        print("Migration complete:")
        print(f"  ✓ {migrated} budget(s) carried forward into {year}-{month:02d}")
        print("  ℹ Use \"Apply to future months\" on the Budgets page to extend")
        print("    any of them into upcoming months.")
        print("=" * 60)
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
