"""
Migration Script — Add Primary Contact / Minor+Guardian columns to family_person
==================================================================================
Adds is_manual, is_primary_contact, is_minor, guardian_name,
guardian_relationship, guardian_contact to the existing family_person
table.

family_person already existed (from the original manual Add Family
Member feature) so db.create_all() will NOT add these new columns on
its own — same reasoning as migrate_add_intended_heir.py. The new
family_timeline table needs no migration at all: it's brand new, so
db.create_all() creates it automatically on next server start.

is_manual backfills to TRUE for all existing rows — every family_person
row that exists today was created through the manual Add Family Member
form, before the on-demand get_or_create path existed.

Safe to run multiple times — checks which columns already exist
before adding anything.

Run from project root: py migrate_add_family_person_metadata.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Add Primary Contact / Minor+Guardian columns to family_person")
        print("=" * 60)

        new_columns = [
            ("is_manual",              "BOOLEAN DEFAULT 1"),
            ("is_primary_contact",     "BOOLEAN DEFAULT 0"),
            ("is_minor",               "BOOLEAN DEFAULT 0"),
            ("guardian_name",          "VARCHAR(200)"),
            ("guardian_relationship",  "VARCHAR(100)"),
            ("guardian_contact",       "VARCHAR(100)"),
        ]

        with db.engine.connect() as conn:
            existing_cols = {
                row[1] for row in
                conn.execute(db.text("PRAGMA table_info(family_person)"))
            }
            for col_name, col_type in new_columns:
                if col_name in existing_cols:
                    print(f"  ✓ {col_name} (already present)")
                else:
                    conn.execute(db.text(
                        f"ALTER TABLE family_person ADD COLUMN {col_name} {col_type}"
                    ))
                    conn.commit()
                    print(f"  ✓ {col_name} (added)")

            # Backfill is_manual = 1 for any pre-existing row where it
            # landed NULL (SQLite applies the column default only to
            # rows inserted AFTER the ALTER TABLE, not existing ones).
            result = conn.execute(db.text(
                "UPDATE family_person SET is_manual = 1 WHERE is_manual IS NULL"
            ))
            conn.commit()
            if result.rowcount:
                print(f"  ✓ backfilled is_manual=1 on {result.rowcount} existing row(s)")

        print(f"\n{'=' * 60}")
        print("Migration complete:")
        print("  ✓ family_person now has is_manual, is_primary_contact, is_minor,")
        print("    guardian_name, guardian_relationship, guardian_contact")
        print("\nNext steps:")
        print("  1. Restart your Flask server (db.create_all() will also create")
        print("     the new family_timeline table automatically)")
        print(f"{'=' * 60}")
        print("\n✅ Migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
