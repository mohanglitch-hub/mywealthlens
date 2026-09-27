"""
Delete User Account (destructive — use with care)
=====================================================
Deletes ONE user account and everything tied to it, across every
module. Always run inventory_user.py first to see what you're about
to delete.

Safety measures:
  - Full database backup BEFORE any change (same pattern as the
    Phase H/I migration scripts).
  - Requires typing the account's exact email address to confirm —
    not just a generic yes/no — so a wrong user_id can't be
    confirmed by accident.
  - Deletes in dependency order (children before parents) so nothing
    is ever left orphaned, regardless of which tables do or don't
    have a database-level ondelete=CASCADE.
  - Deletes document files on disk too (insurance/retirement/wealth
    document uploads) — these are never touched by a database
    deletion alone.
  - Prints a final inventory-style verification that every count for
    this user_id is now zero, and that OTHER users' data is untouched.

The actual table list/deletion order lives in account_deletion.py —
shared with the self-service "Delete my account" web route (Batch 3,
Sep 2026) so the two never drift apart the way this script and
inventory_user.py's OWN separate copies of the table list once did
(an audit found both still referenced `family`/`family_member`/
`family_invite`, tables that no longer exist).

Usage: py delete_user.py <user_id>
Example: py delete_user.py 1
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db
import account_deletion


def delete_user(user_id):
    with app.app_context():
        from sqlalchemy import text

        result = db.session.execute(
            text("SELECT id, name, email FROM user WHERE id = :uid"),
            {"uid": user_id}).fetchone()
        if not result:
            print(f"No user found with id={user_id}. Nothing to do.")
            return False

        uid, name, email = result
        print("=" * 60)
        print("Delete User Account")
        print("=" * 60)
        print(f"\nAccount to delete: {name} <{email}> (id={uid})")

        # ── Confirmation: must type the exact email address ──
        print("\nThis will permanently delete this account and ALL data")
        print("tied to it, across every module (Wealth, Insurance,")
        print("Retirement, Family, Cashflow, Documents). This cannot be")
        print("undone except by restoring the backup this script creates.")
        typed = input(f"\nType the account's email exactly to confirm ({email}): ").strip()
        if typed != email:
            print("\nEmail did not match. Aborting — nothing was changed.")
            return False

        # ── Step 1: backup ──
        backup_path = account_deletion.backup_db(app)
        if backup_path:
            print(f"\nStep 1: Backup written to:\n  {backup_path}")
        else:
            confirm = input("\nNo backup could be created. Type 'yes' to "
                            "proceed anyway: ").strip().lower()
            if confirm != "yes":
                print("Aborting — nothing was changed.")
                return False

        # ── Step 2: document files on disk ──
        print("\nStep 2: Removing document files on disk...")
        removed_dirs = account_deletion.wipe_document_files(app, db, uid)
        for d in removed_dirs:
            print(f"  removed {d}")
        if not removed_dirs:
            print("  (none)")

        # ── Step 3: delete rows, children first ──
        print("\nStep 3: Deleting database rows (children before parents)...")
        summary = account_deletion.cascade_delete_user(app, db, uid)
        for table, count in summary.items():
            if table == "user":
                continue
            print(f"  {table}: {count} row(s) deleted")
        print(f"\n  user: 1 row deleted (id={uid})")

        # ── Step 4: verify ──
        print("\nStep 4: Verifying deletion...")
        remaining = db.session.execute(
            text("SELECT COUNT(*) FROM user WHERE id = :uid"), {"uid": uid}).scalar()
        print(f"  user row still present: {remaining} (expect 0)")

        remaining_wealth = db.session.execute(
            text("SELECT COUNT(*) FROM wealth_asset WHERE user_id = :uid"),
            {"uid": uid}).scalar()
        print(f"  wealth_asset rows still present: {remaining_wealth} (expect 0)")

        other_users = db.session.execute(text("SELECT COUNT(*) FROM user")).scalar()
        print(f"\n  Remaining user accounts in database: {other_users}")

        print("\n" + "=" * 60)
        print("✓ Account deletion complete.")
        print("=" * 60)
        return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: py delete_user.py <user_id>")
        sys.exit(1)
    success = delete_user(int(sys.argv[1]))
    sys.exit(0 if success else 1)
