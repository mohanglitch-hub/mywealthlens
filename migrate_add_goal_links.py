"""
Migration Script — Goals rebuild: step-up SIP + holding links
=================================================================
Two changes:

1. New column on the existing `goal` table: step_up_pct (default 0,
   meaning "flat SIP", identical to today's behaviour for every
   existing goal until the person opts in).

2. The new `goal_holding_link` table. Brand new, so db.create_all()
   (run on every app start) creates it on its own — nothing to do
   here beyond confirming it exists, same as
   migrate_add_mf_transactions.py's own note on this.

Safe to run multiple times.

Run from project root: py migrate_add_goal_links.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Goals rebuild — step-up SIP + holding links")
        print("=" * 60)

        with db.engine.connect() as conn:
            existing_cols = {
                row[1] for row in
                conn.execute(db.text("PRAGMA table_info(goal)"))
            }
            if "step_up_pct" in existing_cols:
                print("  ✓ goal.step_up_pct (already present)")
            else:
                conn.execute(db.text(
                    "ALTER TABLE goal ADD COLUMN step_up_pct FLOAT DEFAULT 0"
                ))
                conn.commit()
                print("  ✓ goal.step_up_pct (added)")

        db.create_all()
        with db.engine.connect() as conn:
            tables = {
                row[0] for row in
                conn.execute(db.text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ))
            }
        if "goal_holding_link" in tables:
            print("  ✓ goal_holding_link table (present)")
        else:
            print("  ✗ goal_holding_link table MISSING — check models.py import")

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