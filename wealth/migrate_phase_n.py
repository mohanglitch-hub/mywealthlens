"""
Wealth — Phase N Migration
=============================
Drops the legacy `loan` table — confirmed dead code (Section 10 of
the Phase N spec): no imports, no foreign keys, no live queries
anywhere in the codebase, and confirmed empty on both real user
accounts at the time of this audit.

Safety-first, matching migrate_phase_h.py's pattern for the legacy
`asset` table: backs up first, and — critically — ABORTS without
dropping anything if the table unexpectedly contains any rows at
migration time. "Investigate before deletion" (Section 10) is
enforced here as a hard runtime check, not just a one-time audit
claim — if your data has changed since this was written, this
script will refuse to silently discard it.

Run from project root: py wealth/migrate_phase_n.py
"""

import sys, os, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Wealth Module — Phase N Migration")
        print("Final Cleanup: Legacy Loan Table")
        print("=" * 60)

        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())

        if "loan" not in tables:
            print("\n  'loan' table not present — nothing to do.")
            print("\n✓ Migration complete (no-op).")
            return True

        # ── Step 1: safety check — abort if any real data exists ──
        row_count = db.session.execute(text("SELECT COUNT(*) FROM loan")).scalar()
        print(f"\nStep 1: 'loan' table row count: {row_count}")
        if row_count > 0:
            print("\n✗ ABORTING — this table is not actually empty.")
            print("  Investigate before deleting: this contradicts the")
            print("  Phase N audit finding of 0 rows. Do not proceed")
            print("  with a blind re-run — find out what changed.")
            return False
        print("  ✓ Confirmed empty — safe to proceed.")

        # ── Step 2: backup ──
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        if db_uri.startswith("sqlite:///"):
            db_path = db_uri.replace("sqlite:///", "", 1)
            if not os.path.isabs(db_path):
                db_path = os.path.join(app.instance_path, db_path)
            if os.path.exists(db_path):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(
                    os.path.dirname(db_path),
                    f"mywealthlens_pre_phase_n_backup_{ts}.db")
                shutil.copy2(db_path, backup_path)
                print(f"\nStep 2: Backup copy written to:\n  {backup_path}")
            else:
                print("\nStep 2: WARNING — could not locate the .db file "
                      "to back up. Proceeding without a backup copy.")
        else:
            print("\nStep 2: Non-SQLite database — back up manually if this matters.")

        # ── Step 3: drop the table ──
        print("\nStep 3: Dropping 'loan' table...")
        db.session.execute(text("DROP TABLE IF EXISTS loan"))
        db.session.commit()

        tables_after = set(inspect(db.engine).get_table_names())
        if "loan" in tables_after:
            print("  ✗ 'loan' table still present — DROP failed.")
            return False
        print("  ✓ 'loan' table removed.")

        # ── Step 4: verify everything else intact ──
        print("\nStep 4: Verifying all other tables intact...")
        must_survive = [
            "wealth_asset", "wealth_liability", "wealth_snapshot",
            "wealth_snapshot_log", "wealth_value_snapshot", "wealth_document",
            "net_worth_history", "insurance_policy", "retirement_scheme", "user",
        ]
        all_ok = True
        for t in must_survive:
            if t in tables_after:
                print(f"  ✓ {t}")
            else:
                print(f"  ✗ {t} — MISSING (unexpected!)")
                all_ok = False

        if not all_ok:
            print("\n✗ An unrelated table is unexpectedly missing. Investigate.")
            return False

        print("\n" + "=" * 60)
        print("✓ Phase N migration complete.")
        print("  Legacy 'loan' table removed (was confirmed empty and unused).")
        print("  All other tables intact.")
        print("=" * 60)
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
