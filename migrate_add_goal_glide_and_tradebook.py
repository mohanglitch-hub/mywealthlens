"""
Migration Script — Goal glide path/drawdown/review + stock tradebook
=========================================================================
Three changes:

1. New columns on the existing `goal` table: glide_start_equity_pct,
   glide_end_equity_pct, is_retirement_goal, retirement_age,
   life_expectancy, monthly_expense_today, expense_inflation_pct,
   post_retirement_return_pct, last_reviewed_at.

2. New column on the existing `goal_holding_link` table: asset_class.

3. New columns on the existing `stock` table: invested, xirr.
   Plus the new `stock_transaction` table — brand new, so
   db.create_all() creates it on its own, same as
   migrate_add_mf_transactions.py's mutual_fund_transaction table.

Safe to run multiple times.

Run from project root: py migrate_add_goal_glide_and_tradebook.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db


def _add_columns(conn, table, columns):
    existing_cols = {
        row[1] for row in
        conn.execute(db.text(f"PRAGMA table_info({table})"))
    }
    for col_name, col_type in columns:
        if col_name in existing_cols:
            print(f"  ✓ {table}.{col_name} (already present)")
        else:
            conn.execute(db.text(
                f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
            ))
            conn.commit()
            print(f"  ✓ {table}.{col_name} (added)")


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Goal glide path / drawdown / review + stock tradebook")
        print("=" * 60)

        with db.engine.connect() as conn:
            _add_columns(conn, "goal", [
                ("glide_start_equity_pct",      "FLOAT DEFAULT 75.0"),
                ("glide_end_equity_pct",        "FLOAT DEFAULT 30.0"),
                ("is_retirement_goal",          "BOOLEAN DEFAULT 0"),
                ("retirement_age",              "INTEGER"),
                ("life_expectancy",             "INTEGER DEFAULT 85"),
                ("monthly_expense_today",       "FLOAT"),
                ("expense_inflation_pct",       "FLOAT DEFAULT 6.0"),
                ("post_retirement_return_pct",  "FLOAT DEFAULT 7.0"),
                ("last_reviewed_at",            "DATETIME"),
            ])
            _add_columns(conn, "goal_holding_link", [
                ("asset_class", "VARCHAR(10) DEFAULT 'equity'"),
            ])
            _add_columns(conn, "stock", [
                ("invested", "FLOAT"),
                ("xirr",     "FLOAT"),
            ])

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
        if "stock_transaction" in tables:
            print("  ✓ stock_transaction table (present)")
        else:
            print("  ✗ stock_transaction table MISSING — check models.py import")

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
