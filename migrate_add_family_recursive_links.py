"""
Migration Script — Add recursive spouse/child links to family_person
========================================================================
Adds spouse_id and parent_family_person_id (both self-referential
FKs to family_person.id) to the existing family_person table, so any
person can now have their own recorded spouse and their own children
— a real recursive family graph, not just the three fixed top-level
rows (Parents / You & Family / Children).

family_person already existed before this, so db.create_all() will
NOT add these new columns on its own — same reasoning as the earlier
migrate_add_family_person_metadata.py. Safe to run multiple times —
checks which columns already exist before adding anything.

Run from project root: py migrate_add_family_recursive_links.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Add recursive spouse/child links to family_person")
        print("=" * 60)

        new_columns = [
            ("spouse_id",                "INTEGER REFERENCES family_person(id)"),
            ("parent_family_person_id",  "INTEGER REFERENCES family_person(id)"),
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

        print(f"\n{'=' * 60}")
        print("Migration complete: family_person now supports recursive")
        print("spouse and parent-child links to any depth.")
        print("Restart your Flask server for the change to take effect.")
        print(f"{'=' * 60}")
        print("\n✅ Migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
