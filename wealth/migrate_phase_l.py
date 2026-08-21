"""
Wealth — Phase L Migration
=============================
1. Renames wealth_value_snapshot.snapshot_date -> effective_date.
2. Adds wealth_liability.balance_as_of (new column, nullable).

Why the rename is safe (Section 71/72 of spec — "do not invent
dates", "existing records remain intact"): every row Phase J ever
created set snapshot_date to datetime.utcnow().date() at the exact
moment it was recorded — i.e. it always already equaled what
effective_date is now defined to mean for that row (the date the
value was recorded, prior to Phase L ever letting anyone backdate
anything). Renaming the column is therefore a PURE METADATA change:
every existing row's value is preserved byte-for-byte, just under
its now-correctly-named column. No ALTER-with-backfill, no invented
dates, no data loss — verified explicitly in Step 4 below.

Uses SQLite's native ALTER TABLE ... RENAME COLUMN (supported since
SQLite 3.25.0 / 2018 — well within range for any modern Python's
bundled sqlite3), so the column keeps its exact same underlying
storage, index compatibility, and row values; only its name changes.

Follows the established per-phase migration convention. Backup-first,
same as every prior phase's migration.

Run from project root: py wealth/migrate_phase_l.py
"""

import sys, os, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Wealth Module — Phase L Migration")
        print("Backdated Valuations")
        print("=" * 60)

        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)

        # ── Step 1: backup ──
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        if db_uri.startswith("sqlite:///"):
            db_path = db_uri.replace("sqlite:///", "", 1)
            if not os.path.isabs(db_path):
                db_path = os.path.join(app.instance_path, db_path)
            if os.path.exists(db_path):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(
                    os.path.dirname(db_path),
                    f"mywealthlens_pre_phase_l_backup_{ts}.db")
                shutil.copy2(db_path, backup_path)
                print(f"\nStep 1: Backup copy written to:\n  {backup_path}")
            else:
                print("\nStep 1: WARNING — could not locate the .db file "
                      "to back up. Proceeding without a backup copy.")
        else:
            print("\nStep 1: Non-SQLite database — back up manually if this matters.")

        # ── Step 2: capture pre-migration row values for verification ──
        print("\nStep 2: Recording pre-migration wealth_value_snapshot state...")
        existing_columns_pre = {c["name"] for c in inspector.get_columns("wealth_value_snapshot")} \
            if "wealth_value_snapshot" in inspector.get_table_names() else set()
        date_column = "effective_date" if "effective_date" in existing_columns_pre else "snapshot_date"
        # ^ idempotency: on a second run, the column is already
        # renamed, so this must read whichever name currently exists
        # rather than assuming the pre-Phase-L name unconditionally.
        pre_rows = db.session.execute(text(
            f"SELECT id, value, {date_column} FROM wealth_value_snapshot "
            f"ORDER BY id")).fetchall() if existing_columns_pre else []
        print(f"  {len(pre_rows)} existing row(s) recorded for post-migration comparison.")

        # ── Step 3: rename snapshot_date -> effective_date ──
        print("\nStep 3: Renaming wealth_value_snapshot.snapshot_date -> effective_date...")
        existing_columns = {c["name"] for c in inspector.get_columns("wealth_value_snapshot")}
        if "effective_date" in existing_columns:
            print("  'effective_date' column already present — skipping rename.")
        elif "snapshot_date" not in existing_columns:
            print("  ✗ Neither 'snapshot_date' nor 'effective_date' found — "
                  "unexpected schema state. Investigate before proceeding.")
            return False
        else:
            db.session.execute(text(
                "ALTER TABLE wealth_value_snapshot "
                "RENAME COLUMN snapshot_date TO effective_date"))
            db.session.commit()
            print("  ✓ Column renamed.")

        # ── Step 4: add wealth_liability.balance_as_of ──
        print("\nStep 4: Checking wealth_liability.balance_as_of column...")
        liability_columns = {c["name"] for c in inspect(db.engine).get_columns("wealth_liability")}
        if "balance_as_of" in liability_columns:
            print("  'balance_as_of' column already present — skipping.")
        else:
            db.session.execute(text(
                "ALTER TABLE wealth_liability ADD COLUMN balance_as_of DATE"))
            db.session.commit()
            print("  ✓ 'balance_as_of' column added (nullable, no existing "
                  "liability data touched).")

        # ── Step 5: verify no data was lost/altered by the rename ──
        print("\nStep 5: Verifying wealth_value_snapshot data is unchanged...")
        post_rows = db.session.execute(text(
            "SELECT id, value, effective_date FROM wealth_value_snapshot "
            "ORDER BY id")).fetchall()
        print(f"  Row count: pre={len(pre_rows)}, post={len(post_rows)}")
        if len(pre_rows) != len(post_rows):
            print("  ✗ Row count changed — investigate before trusting this database.")
            return False
        mismatches = 0
        for pre, post in zip(pre_rows, post_rows):
            if pre[0] != post[0] or pre[1] != post[1] or str(pre[2]) != str(post[2]):
                mismatches += 1
                print(f"  ✗ Mismatch on id={pre[0]}: pre={pre}, post={post}")
        if mismatches:
            print(f"  ✗ {mismatches} row(s) changed unexpectedly.")
            return False
        print(f"  ✓ All {len(post_rows)} row(s) verified byte-identical "
              f"(id, value, and date all match their pre-migration state).")

        # ── Step 6: verify unrelated tables untouched ──
        print("\nStep 6: Verifying other tables intact...")
        must_survive = [
            "wealth_asset", "wealth_snapshot", "wealth_snapshot_log",
            "wealth_document", "user",
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
        print("✓ Phase L migration complete.")
        print("  wealth_value_snapshot.effective_date active (renamed, not rebuilt).")
        print("  wealth_liability.balance_as_of added.")
        print("  All existing data verified byte-identical.")
        print("=" * 60)
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)
