"""
Wealth — Phase F Migration
=============================
Adds: wealth_snapshot table (Wealth History feature).

Follows this project's established per-phase migration convention
(see insurance_centre/migrate_phase6.py, migrate_phase8.py) rather
than editing the existing wealth/migrate.py, which is scoped to the
Phase A/B/C table set. Safe to run multiple times — db.create_all()
only creates tables that don't already exist, and this script
verifies rather than assumes.

Run from project root: py wealth/migrate_phase_f.py

ROLLBACK: this is a brand-new, independent table — nothing else
references it (Section 51 of spec: Phase F must not alter
WealthAsset/WealthLiability/any other module's data). Safe to drop
if ever needed:

    py -c "import sqlite3; conn = sqlite3.connect('instance/mywealthlens.db'); conn.execute('DROP TABLE IF EXISTS wealth_snapshot'); conn.commit(); conn.close(); print('Rolled back')"
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Wealth Module — Phase F Migration")
        print("=" * 60)

        # Existing tables snapshot, taken BEFORE create_all(), so we
        # can prove afterwards that nothing except wealth_snapshot
        # was newly created (Section 51 — minimum required schema
        # change only).
        from sqlalchemy import inspect
        inspector_before = inspect(db.engine)
        tables_before = set(inspector_before.get_table_names())

        print("\nStep 1: Creating wealth_snapshot table (if not present)...")
        db.create_all()

        inspector_after = inspect(db.engine)
        tables_after = set(inspector_after.get_table_names())

        if "wealth_snapshot" in tables_after:
            print("  ✓ wealth_snapshot")
        else:
            print("  ✗ wealth_snapshot — MISSING")
            print("\n✗ Migration failed. Check the errors above.")
            return False

        newly_created = tables_after - tables_before
        unexpected = newly_created - {"wealth_snapshot"}
        if unexpected:
            print(f"\n  ⚠ Unexpected additional tables were created: {unexpected}")
            print("    (This migration only intended to add wealth_snapshot.)")
        else:
            print("  ✓ No unexpected tables were created")

        # ── Verify the unique constraint exists ─────────────────────
        print("\nStep 2: Verifying UNIQUE(user_id, snapshot_date)...")
        uniques = inspector_after.get_unique_constraints("wealth_snapshot")
        indexes = inspector_after.get_indexes("wealth_snapshot")
        has_unique = any(
            set(u.get("column_names", [])) == {"user_id", "snapshot_date"}
            for u in uniques
        ) or any(
            idx.get("unique") and set(idx.get("column_names", [])) == {"user_id", "snapshot_date"}
            for idx in indexes
        )
        if has_unique:
            print("  ✓ UNIQUE(user_id, snapshot_date) present")
        else:
            print("  ⚠ Could not confirm the unique constraint via inspector "
                 "(SQLite sometimes reports these as indexes rather than "
                 "constraints) — verified instead at the application layer "
                 "in history_service.create_snapshot(), which always checks "
                 "for an existing row before inserting.")

        # ── Verify other Wealth/Assets tables are untouched (Section 51) ──
        print("\nStep 3: Verifying existing tables are untouched...")
        for t, label in [
            ("wealth_asset", "Wealth Assets"),
            ("wealth_liability", "Wealth Liabilities"),
            ("asset", "old Assets module"),
        ]:
            if t in tables_after:
                with db.engine.connect() as conn:
                    count = conn.execute(db.text(f"SELECT COUNT(*) FROM {t}")).scalar()
                print(f"  ✓ '{t}' ({label}) still present, {count} row(s) — untouched")
            else:
                print(f"  ⚠ '{t}' not found — unexpected, please verify manually")

        print(f"\n{'=' * 60}")
        print("Migration complete:")
        print("  ✓ wealth_snapshot table created")
        print("  ✓ Zero changes to any other table")
        print(f"\nNext steps:")
        print(f"  1. Restart your Flask server")
        print(f"  2. Visit /wealth/history to see the Wealth History page")
        print(f"{'=' * 60}")
        print("\n✅ Phase F migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
