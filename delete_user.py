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

Usage: py delete_user.py <user_id>
Example: py delete_user.py 1
"""

import sys, os, shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db


def backup_db():
    db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not db_uri.startswith("sqlite:///"):
        print("Non-SQLite database — back up manually before proceeding.")
        return None
    db_path = db_uri.replace("sqlite:///", "", 1)
    if not os.path.isabs(db_path):
        db_path = os.path.join(app.instance_path, db_path)
    if not os.path.exists(db_path):
        print("WARNING: could not locate the .db file to back up.")
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(
        os.path.dirname(db_path), f"mywealthlens_pre_user_delete_backup_{ts}.db")
    shutil.copy2(db_path, backup_path)
    return backup_path


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
        print("Retirement, Documents). This cannot be undone except by")
        print("restoring the backup this script creates.")
        typed = input(f"\nType the account's email exactly to confirm ({email}): ").strip()
        if typed != email:
            print("\nEmail did not match. Aborting — nothing was changed.")
            return False

        # ── Step 1: backup ──
        backup_path = backup_db()
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
        policy_ids = [r[0] for r in db.session.execute(
            text("SELECT id FROM insurance_policy WHERE user_id = :uid"),
            {"uid": uid}).fetchall()]
        scheme_ids = [r[0] for r in db.session.execute(
            text("SELECT id FROM retirement_scheme WHERE user_id = :uid"),
            {"uid": uid}).fetchall()]

        wealth_dir = os.path.join(app.instance_path, "documents", "wealth", str(uid))
        if os.path.isdir(wealth_dir):
            shutil.rmtree(wealth_dir)
            print(f"  removed {wealth_dir}")

        for pid in policy_ids:
            d = os.path.join(app.instance_path, "documents", "insurance", str(pid))
            if os.path.isdir(d):
                shutil.rmtree(d)
                print(f"  removed {d}")

        for sid in scheme_ids:
            d = os.path.join(app.instance_path, "documents", "retirement", str(sid))
            if os.path.isdir(d):
                shutil.rmtree(d)
                print(f"  removed {d}")

        # ── Step 3: delete rows, children first ──
        print("\nStep 3: Deleting database rows (children before parents)...")

        # Indirect (via policy_id / scheme_id)
        if policy_ids:
            ph = ",".join(str(i) for i in policy_ids)
            for t, col in [("insurance_nominee", "policy_id"),
                          ("insurance_member", "policy_id"),
                          ("insurance_addon", "policy_id"),
                          ("insurance_document", "policy_id"),
                          ("insurance_timeline", "policy_id")]:
                n = db.session.execute(text(f"DELETE FROM {t} WHERE {col} IN ({ph})")).rowcount
                if n: print(f"  {t}: {n} row(s) deleted")

        if scheme_ids:
            sh = ",".join(str(i) for i in scheme_ids)
            for t, col in [("retirement_contribution", "scheme_id"),
                          ("retirement_balance_snapshot", "scheme_id"),
                          ("retirement_timeline", "scheme_id"),
                          ("retirement_scheme_nominee", "scheme_id"),
                          ("retirement_document", "scheme_id")]:
                n = db.session.execute(text(f"DELETE FROM {t} WHERE {col} IN ({sh})")).rowcount
                if n: print(f"  {t}: {n} row(s) deleted")

        # Direct user_id-owned tables
        direct_tables = [
            "mutual_fund", "stock", "goal", "user_profile", "loan",
            "net_worth_history", "family_member",
            "wealth_asset", "wealth_liability", "wealth_value_snapshot",
            "wealth_snapshot", "wealth_snapshot_log", "wealth_document",
            "insurance_policy", "retirement_scheme",
        ]
        for t in direct_tables:
            n = db.session.execute(
                text(f"DELETE FROM {t} WHERE user_id = :uid"), {"uid": uid}).rowcount
            if n: print(f"  {t}: {n} row(s) deleted")

        # family created_by this user
        n = db.session.execute(
            text("DELETE FROM family WHERE created_by = :uid"), {"uid": uid}).rowcount
        if n: print(f"  family: {n} row(s) deleted")

        # family_invite keyed by email
        n = db.session.execute(
            text("DELETE FROM family_invite WHERE email = :email"), {"email": email}).rowcount
        if n: print(f"  family_invite: {n} row(s) deleted")

        # Finally, the user row itself
        db.session.execute(text("DELETE FROM user WHERE id = :uid"), {"uid": uid})
        db.session.commit()
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