"""
Wealth — Phase J Migration
=============================
Activates the previously-dormant wealth_value_snapshot table (built
in Phase A, never written to until now). No new table and no new
columns — the existing schema already fits Section 11's requirements
exactly (entity_type + entity_id + value + a date field + note).

The one real schema change: adds a second index, alongside the
existing (entity_type, entity_id) one, covering (user_id, entity_type,
entity_id) — matching the actual query shape every real caller uses
(Section 53/54/60/61: every lookup filters by user_id first, as the
IDOR guard, before entity_type/entity_id). The original index stays;
this doesn't replace it, just adds the index that matches how the
table is actually queried now that it's live.

Follows the established per-phase migration convention. Metadata/
index-only — no existing data is touched (there isn't any: this
table has never been written to, confirmed by full-codebase audit
before this phase began).

Run from project root: py wealth/migrate_phase_j.py
"""

import sys, os, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Wealth Module — Phase J Migration")
        print("Asset-Level / Liability-Level Value History")
        print("=" * 60)

        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        tables = set(inspector.get_table_names())

        if "wealth_value_snapshot" not in tables:
            print("\n✗ wealth_value_snapshot table not found. This should have "
                  "been created back in Phase A via db.create_all(). Run "
                  "db.create_all() first, then retry this migration.")
            return False

        print("\nStep 1: wealth_value_snapshot table found.")

        # ── Confirm it's genuinely empty before proceeding (Section 92) ──
        row_count = db.session.execute(
            text("SELECT COUNT(*) FROM wealth_value_snapshot")).scalar()
        print(f"  Existing rows: {row_count}")
        if row_count > 0:
            print("  NOTE: this table already has data. Nothing will be "
                  "deleted or modified — Phase J only adds a new index — "
                  "but this is worth a second look if unexpected.")

        # ── Safety backup (same lightweight pattern as prior phases) ──
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        if db_uri.startswith("sqlite:///"):
            db_path = db_uri.replace("sqlite:///", "", 1)
            if not os.path.isabs(db_path):
                db_path = os.path.join(app.instance_path, db_path)
            if os.path.exists(db_path):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(
                    os.path.dirname(db_path),
                    f"mywealthlens_pre_phase_j_backup_{ts}.db")
                shutil.copy2(db_path, backup_path)
                print(f"\nStep 2: Backup copy written to:\n  {backup_path}")
            else:
                print("\nStep 2: WARNING — could not locate the .db file "
                      "to back up. Proceeding without a backup copy.")
        else:
            print("\nStep 2: Non-SQLite database — back up manually if this matters.")

        # ── Add the new user-scoped index (if not already present) ──
        print("\nStep 3: Checking indexes on wealth_value_snapshot...")
        existing_indexes = {ix["name"] for ix in inspector.get_indexes("wealth_value_snapshot")}
        index_name = "ix_wealth_value_snapshot_user_entity"
        if index_name in existing_indexes:
            print(f"  '{index_name}' already present — skipping.")
        else:
            db.session.execute(text(
                f"CREATE INDEX {index_name} ON wealth_value_snapshot "
                f"(user_id, entity_type, entity_id)"))
            db.session.commit()
            print(f"  ✓ '{index_name}' created.")

        # ── Verify (fresh inspector — the one above may have cached
        # metadata from before the CREATE INDEX above) ──
        print("\nStep 4: Verifying final index list...")
        final_indexes = inspect(db.engine).get_indexes("wealth_value_snapshot")
        for ix in final_indexes:
            print(f"  {ix['name']}: {ix['column_names']}")

        # ── Verify unrelated tables untouched ──
        print("\nStep 5: Verifying other tables intact...")
        must_survive = [
            "wealth_asset", "wealth_liability", "wealth_snapshot",
            "wealth_snapshot_log", "wealth_document", "user",
        ]
        all_ok = True
        tables_after = set(inspect(db.engine).get_table_names())
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
        print("✓ Phase J migration complete.")
        print("  wealth_value_snapshot ready for use.")
        print("  New index added for user-scoped entity lookups.")
        print("  No existing data touched.")
        print("=" * 60)
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
