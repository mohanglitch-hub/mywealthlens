"""
Migration Script — Add Intended Heir columns to wealth_asset
=================================================================
Adds intended_heir and intended_heir_relationship to the existing
wealth_asset table.

Unlike a brand-new table (which db.create_all() creates automatically
on server start — no migration needed), a NEW COLUMN on a table that
already exists needs an explicit ALTER TABLE. db.create_all() only
ever creates tables that don't exist yet; it never modifies ones
that do, which is why this script exists.

These two columns are the inverse of the existing original_owner /
original_owner_relationship pair: those answer "who gave me this"
(past), these answer "who should get this" (future) — the same
shape as the nominee fields Insurance and Retirement already have,
so a Wealth asset can now participate in Family Centre's People view
and Coverage Gaps the same way a policy or scheme nominee does.

Safe to run multiple times — checks which columns already exist
before adding anything, matching the same convention as
wealth/migrate.py's own column-addition steps.

Run from project root: py migrate_add_intended_heir.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Add Intended Heir columns to wealth_asset")
        print("=" * 60)

        new_columns = [
            ("intended_heir",              "VARCHAR(200)"),
            ("intended_heir_relationship", "VARCHAR(100)"),
        ]

        with db.engine.connect() as conn:
            existing_cols = {
                row[1] for row in
                conn.execute(db.text("PRAGMA table_info(wealth_asset)"))
            }
            for col_name, col_type in new_columns:
                if col_name in existing_cols:
                    print(f"  ✓ {col_name} (already present)")
                else:
                    conn.execute(db.text(
                        f"ALTER TABLE wealth_asset ADD COLUMN {col_name} {col_type}"
                    ))
                    conn.commit()
                    print(f"  ✓ {col_name} (added)")

        print(f"\n{'=' * 60}")
        print("Migration complete:")
        print("  ✓ wealth_asset now has intended_heir + intended_heir_relationship")
        print("\nNext steps:")
        print("  1. Restart your Flask server")
        print(f"{'=' * 60}")
        print("\n✅ Migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)