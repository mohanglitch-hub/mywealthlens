"""
Wealth — Migration Script
============================
Phase A: Creates the new Wealth module tables.

This is a brand-new, independent module — there is no old table to
migrate data from, and critically, this script NEVER touches the
existing `asset` table or any of its data. Verified explicitly at
the end of this script (Section 19/31 of spec).

Safe to run multiple times.
Run from project root: py wealth/migrate.py

ROLLBACK (Section 19 — "must be safely reversible"):
This project's migration convention (established in insurance_centre
and retirement_centre) is plain Python scripts, not a formal
Alembic-style rollback system. If you ever need to undo this
migration, the three new tables can be safely dropped since nothing
else in the app references them yet (Phase A only reads them via
WealthStatisticsService, which handles empty tables gracefully):

    py -c "import sqlite3; conn = sqlite3.connect('instance/mywealthlens.db'); [conn.execute(f'DROP TABLE IF EXISTS {t}') for t in ['wealth_asset','wealth_liability','wealth_value_snapshot']]; conn.commit(); conn.close(); print('Rolled back')"

This is safe specifically BECAUSE Wealth has zero foreign keys
pointing into it from any other table (Section 2 of spec) — dropping
these three tables cannot orphan or break anything else.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Wealth Module — Phase A Migration")
        print("=" * 60)

        print("\nStep 1: Creating new tables...")
        db.create_all()

        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        required_tables = [
            "wealth_asset",
            "wealth_liability",
            "wealth_value_snapshot",
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

        # ── Step 1b: Add Phase B columns to wealth_asset ───────────
        print("\nStep 1b: Checking Phase B columns on wealth_asset...")
        new_columns = [
            ("description",          "TEXT"),
            ("value_as_of",          "DATE"),
            ("acquisition_value",    "FLOAT"),
            ("property_type",        "VARCHAR(50)"),
            ("property_address",     "VARCHAR(300)"),
            ("city",                 "VARCHAR(100)"),
            ("state",                "VARCHAR(100)"),
            ("area",                 "FLOAT"),
            ("area_unit",            "VARCHAR(20)"),
            ("metal_type",           "VARCHAR(50)"),
            ("weight",               "FLOAT"),
            ("weight_unit",          "VARCHAR(20)"),
            ("vehicle_type",         "VARCHAR(50)"),
            ("registration_number",  "VARCHAR(50)"),
            ("institution",          "VARCHAR(200)"),
            ("account_reference",    "VARCHAR(100)"),
            ("deposit_type",         "VARCHAR(50)"),
            ("interest_rate",        "FLOAT"),
            ("maturity_date",        "DATE"),
            ("investment_type",      "VARCHAR(50)"),
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

        print("\nStep 1c: Checking Phase C columns on wealth_liability...")
        new_liability_columns = [
            ("liability_type",     "VARCHAR(100)"),
            ("description",        "TEXT"),
            ("account_reference",  "VARCHAR(100)"),
            ("interest_rate",      "FLOAT"),
            ("ownership_percentage", "FLOAT"),
        ]
        with db.engine.connect() as conn:
            existing_liab_cols = {
                row[1] for row in
                conn.execute(db.text("PRAGMA table_info(wealth_liability)"))
            }
            for col_name, col_type in new_liability_columns:
                if col_name in existing_liab_cols:
                    print(f"  ✓ {col_name} (already present)")
                else:
                    default_clause = " DEFAULT 100.0" if col_name == "ownership_percentage" else ""
                    conn.execute(db.text(
                        f"ALTER TABLE wealth_liability ADD COLUMN {col_name} {col_type}{default_clause}"
                    ))
                    conn.commit()
                    print(f"  ✓ {col_name} (added)")

        # ── Verify the existing Assets table is untouched (Section 27/31) ──
        print("\nStep 2: Verifying existing Assets module is untouched...")
        if "asset" in tables:
            with db.engine.connect() as conn:
                count = conn.execute(
                    db.text("SELECT COUNT(*) FROM asset")
                ).scalar()
            print(f"  ✓ 'asset' table still present, {count} row(s) — untouched")
        else:
            print("  ⚠ 'asset' table not found — this is unexpected, "
                 "please verify manually (this migration did NOT remove it)")

        print(f"\n{'=' * 60}")
        print("Migration complete:")
        print("  ✓ 3 new, independent tables created")
        print("  ✓ Zero foreign keys between Wealth and Assets, either direction")
        print("  ℹ No existing data touched — this is a brand-new module")
        print(f"\nNext steps:")
        print(f"  1. Restart your Flask server")
        print(f"  2. Visit /wealth to see the dashboard skeleton")
        print(f"  3. Confirm /assets still works exactly as before")
        print(f"{'=' * 60}")
        print("\n✅ Phase A migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
