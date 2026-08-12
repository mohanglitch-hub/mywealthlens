"""
Retirement Centre — Migration Script
=======================================
Phase A: Creates the new Retirement Centre tables.
Phase B: Adds 3 new nullable columns to retirement_scheme
         (uan_number, basic_salary, contribution_preference)
         that weren't part of the original Phase A schema.

This is a brand-new module — there is no old table to migrate data
from, unlike Insurance Centre's Phase 2 migration. This script only
creates/updates tables and verifies they landed correctly.

Safe to run multiple times — column additions check for existing
columns first via PRAGMA table_info before adding.
Run from project root: py retirement_centre/migrate.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Retirement Centre — Phase A Migration")
        print("=" * 60)

        print("\nStep 1: Creating new tables...")
        db.create_all()

        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        required_tables = [
            "retirement_scheme",
            "retirement_contribution",
            "retirement_balance_snapshot",
            "retirement_timeline",
            "retirement_scheme_nominee",
            "retirement_document",
        ]

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

        # ── Step 2: Add Phase B columns to retirement_scheme ─────
        print("\nStep 2: Checking Phase B columns on retirement_scheme...")

        new_columns = [
            ("uan_number",              "VARCHAR(30)"),
            ("basic_salary",            "FLOAT"),
            ("contribution_preference", "VARCHAR(20)"),
        ]

        with db.engine.connect() as conn:
            existing_cols = {
                row[1] for row in
                conn.execute(db.text("PRAGMA table_info(retirement_scheme)"))
            }
            for col_name, col_type in new_columns:
                if col_name in existing_cols:
                    print(f"  ✓ {col_name} (already present)")
                else:
                    conn.execute(db.text(
                        f"ALTER TABLE retirement_scheme ADD COLUMN {col_name} {col_type}"
                    ))
                    conn.commit()
                    print(f"  ✓ {col_name} (added)")

        print("\nStep 2b: Checking entry_type column on retirement_contribution...")
        with db.engine.connect() as conn:
            contrib_cols = {
                row[1] for row in
                conn.execute(db.text("PRAGMA table_info(retirement_contribution)"))
            }
            if "entry_type" in contrib_cols:
                print("  ✓ entry_type (already present)")
            else:
                conn.execute(db.text(
                    "ALTER TABLE retirement_contribution ADD COLUMN "
                    "entry_type VARCHAR(20) NOT NULL DEFAULT 'Deposit'"
                ))
                conn.commit()
                print("  ✓ entry_type (added, existing rows default to 'Deposit')")

        print("\nStep 3: Checking Document Vault column on retirement_document...")
        with db.engine.connect() as conn:
            doc_cols = {
                row[1] for row in
                conn.execute(db.text("PRAGMA table_info(retirement_document)"))
            }
            if "title" in doc_cols:
                print("  ✓ title (already present)")
            else:
                conn.execute(db.text(
                    "ALTER TABLE retirement_document ADD COLUMN title VARCHAR(255)"
                ))
                conn.commit()
                print("  ✓ title (added)")

        print(f"\n{'=' * 60}")
        print("Migration complete:")
        print("  ✓ 5 tables verified")
        print("  ✓ 3 Phase B columns verified/added")
        print("  ✓ 1 Document Vault column verified/added")
        print("  ℹ No existing data touched or lost")
        print(f"\nNext steps:")
        print(f"  1. Restart your Flask server")
        print(f"  2. Visit /retirement to see the dashboard skeleton")
        print(f"{'=' * 60}")
        print("\n✅ Phase A migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
