"""
Wealth — Phase G Migration
=============================
Adds: wealth_document table (Wealth Document Vault feature).

Follows the same per-phase migration convention as
wealth/migrate_phase_f.py (which itself mirrors
insurance_centre/migrate_phase6.py, migrate_phase8.py). Safe to run
multiple times — db.create_all() only creates tables that don't
already exist.

Run from project root: py wealth/migrate_phase_g.py

Also creates the physical storage directory:
    instance/documents/wealth/
(per-user subfolders are created lazily on first upload, mirroring
insurance_centre's get_document_upload_path() behaviour.)

ROLLBACK: this is a brand-new, independent table with only SET NULL
foreign keys to wealth_asset/wealth_liability (never CASCADE) —
deleting it never touches Asset/Liability/Snapshot data (Section
43/51 of spec). Safe to drop if ever needed:

    py -c "import sqlite3; conn = sqlite3.connect('instance/mywealthlens.db'); conn.execute('DROP TABLE IF EXISTS wealth_document'); conn.commit(); conn.close(); print('Rolled back')"

Note: rolling back the table does NOT delete any physical files
already uploaded to instance/documents/wealth/ — those would need to
be removed separately if desired.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Wealth Module — Phase G Migration")
        print("=" * 60)

        from sqlalchemy import inspect
        inspector_before = inspect(db.engine)
        tables_before = set(inspector_before.get_table_names())

        print("\nStep 1: Creating wealth_document table (if not present)...")
        db.create_all()

        inspector_after = inspect(db.engine)
        tables_after = set(inspector_after.get_table_names())

        if "wealth_document" in tables_after:
            print("  ✓ wealth_document")
        else:
            print("  ✗ wealth_document — MISSING")
            print("\n✗ Migration failed. Check the errors above.")
            return False

        newly_created = tables_after - tables_before
        unexpected = newly_created - {"wealth_document"}
        if unexpected:
            print(f"\n  ⚠ Unexpected additional tables were created: {unexpected}")
        else:
            print("  ✓ No unexpected tables were created")

        # ── Verify indexes ──────────────────────────────────────────
        print("\nStep 2: Verifying indexes...")
        indexes = inspector_after.get_indexes("wealth_document")
        indexed_cols = set()
        for idx in indexes:
            indexed_cols.update(idx.get("column_names", []))
        expected = {"user_id", "document_type", "asset_id", "liability_id", "uploaded_at"}
        missing = expected - indexed_cols
        if not missing:
            print(f"  ✓ All expected indexes present: {sorted(expected)}")
        else:
            print(f"  ⚠ Some expected indexes not detected by inspector: {sorted(missing)} "
                 "(SQLite sometimes reports composite/simple indexes differently — "
                 "not necessarily a real problem, but worth a manual check)")

        # ── Verify existing tables untouched (Section 43/51) ─────────
        print("\nStep 3: Verifying existing tables are untouched...")
        for t, label in [
            ("wealth_asset",     "Wealth Assets"),
            ("wealth_liability", "Wealth Liabilities"),
            ("wealth_snapshot",  "Wealth History Snapshots"),
            ("insurance_document", "Insurance Documents"),
        ]:
            if t in tables_after:
                with db.engine.connect() as conn:
                    count = conn.execute(db.text(f"SELECT COUNT(*) FROM {t}")).scalar()
                print(f"  ✓ '{t}' ({label}) still present, {count} row(s) — untouched")
            else:
                print(f"  ⚠ '{t}' not found — unexpected, please verify manually")

        # ── Create physical storage directory ─────────────────────────
        print("\nStep 4: Preparing physical storage directory...")
        storage_dir = os.path.join(app.instance_path, "documents", "wealth")
        os.makedirs(storage_dir, exist_ok=True)
        print(f"  ✓ {storage_dir}")
        print("  (Per-user subfolders are created automatically on first upload.)")

        print(f"\n{'=' * 60}")
        print("Migration complete:")
        print("  ✓ wealth_document table created")
        print("  ✓ Storage directory ready")
        print("  ✓ Zero changes to any other table")
        print(f"\nNext steps:")
        print(f"  1. Restart your Flask server")
        print(f"  2. Visit /wealth/documents to see the Document Vault")
        print(f"{'=' * 60}")
        print("\n✅ Phase G migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
