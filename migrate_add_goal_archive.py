"""
Migration Script — Goal archiving (Achieved / Dropped)
=========================================================================
New columns on the existing `goal` table:
  - is_archived     BOOLEAN DEFAULT 0
  - archived_at     DATETIME
  - archive_reason  VARCHAR(20)   -- 'achieved' | 'dropped'

Same convention already used by WealthAsset / RetirementScheme /
InsurancePolicy (Archive -> Restore, never a straight delete).

Safe to run multiple times.

Run from project root: py migrate_add_goal_archive.py
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
        print("Goal archiving (Achieved / Dropped)")
        print("=" * 60)

        with db.engine.connect() as conn:
            _add_columns(conn, "goal", [
                ("is_archived",    "BOOLEAN DEFAULT 0"),
                ("archived_at",    "DATETIME"),
                ("archive_reason", "VARCHAR(20)"),
            ])

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
