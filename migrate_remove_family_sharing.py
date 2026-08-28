"""
Migration Script — Remove Dormant Family Sharing Tables
===========================================================
Removes the family, family_member, and family_invite tables.

These were built during an earlier phase for a DIFFERENT feature
than the new Family Centre — a multi-user account-sharing system
(inviting another MyWealthLens login to share visibility into
household data), not nominee/beneficiary tracking. Confirmed
completely unreachable before this migration was written: no route
in app.py ever served either of the two templates built for it
(family.html, family_manage.html — both deleted), and the sidebar's
"Family" nav link pointed at a URL with no matching route at all
(a genuine 404 in the live app). The new Family Centre (family_bp,
url_prefix="/family") now owns that same nav link and URL properly.

The model classes, the two orphaned templates, and the (never-used)
imports in app.py have all already been removed from the codebase.
This script's only job is to drop the actual database tables left
behind by that earlier, abandoned development — db.create_all()
only ever creates tables for models that still exist, it never
drops ones that were removed from the code.

Safe to run multiple times — checks whether each table exists
before attempting to drop it. If any table has rows (unlikely,
since nothing in the live app ever wrote to them), their count is
reported before the drop, purely so you have a record of what
existed; the data itself is not recoverable after this runs.

Run from project root: py migrate_remove_family_sharing.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db

TABLES_TO_DROP = ["family_invite", "family_member", "family"]
# ^ Drop order matters: family_invite and family_member both have a
#   foreign key to family, so they must go first.


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Remove Dormant Family Sharing Tables")
        print("=" * 60)

        with db.engine.connect() as conn:
            existing = {
                row[0] for row in
                conn.execute(db.text(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ))
            }

            any_dropped = False
            for table in TABLES_TO_DROP:
                if table not in existing:
                    print(f"\n  ℹ {table} table not found — already clean.")
                    continue

                row_count = conn.execute(
                    db.text(f"SELECT COUNT(*) FROM {table}")
                ).scalar()
                print(f"\n  Found {table} table with {row_count} row(s).")
                conn.execute(db.text(f"DROP TABLE {table}"))
                conn.commit()
                print(f"  ✓ {table} dropped")
                any_dropped = True

        print(f"\n{'=' * 60}")
        if any_dropped:
            print("Migration complete:")
            print("  ✓ Dormant family-sharing tables removed")
            print("\nNext steps:")
            print("  1. Restart your Flask server")
        else:
            print("Migration complete (no-op) — nothing to remove.")
        print(f"{'=' * 60}")
        print("\n✅ Migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
