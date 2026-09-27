"""
User Data Inventory (read-only)
==================================
Reports exactly what's tied to a given user_id across every module —
every table account_deletion.py's DIRECT_TABLES/GOAL_ID_TABLES would
touch, so this stays in sync with delete_user.py and the self-service
"Delete my account" web route by construction rather than by a second,
separately-maintained table list (an audit found the old, hand-copied
list here had drifted stale — it referenced `family`/`family_member`/
`family_invite`, tables that no longer exist from an abandoned old
feature, and was missing several tables added since).

This script makes NO changes. It's purely so you can see what's
actually there before deciding whether/how to delete an account.

Usage: py inventory_user.py <user_id>
Example: py inventory_user.py 2
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db
import account_deletion


def inventory(user_id):
    with app.app_context():
        from sqlalchemy import text

        print("=" * 60)
        print(f"Data Inventory — user_id = {user_id}")
        print("=" * 60)

        # ── Confirm the user exists ──
        result = db.session.execute(
            text("SELECT id, name, email FROM user WHERE id = :uid"),
            {"uid": user_id}).fetchone()
        if not result:
            print(f"\nNo user found with id={user_id}. Nothing to report.")
            return
        print(f"\nAccount: {result[1]} <{result[2]}>")

        print("\n── goal_holding_link (via goal_id, no user_id of its own) ──")
        for table in account_deletion.GOAL_ID_TABLES:
            count = db.session.execute(text(
                f"SELECT COUNT(*) FROM {table} WHERE goal_id IN "
                f"(SELECT id FROM goal WHERE user_id = :uid)"
            ), {"uid": user_id}).scalar()
            marker = "  " if count == 0 else "→ "
            print(f"{marker}{table}: {count}")

        print("\n── Directly-owned rows (by user_id) ──")
        for table in account_deletion.DIRECT_TABLES:
            try:
                count = db.session.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE user_id = :uid"),
                    {"uid": user_id}).scalar()
                marker = "  " if count == 0 else "→ "
                print(f"{marker}{table}: {count}")
            except Exception as e:
                print(f"  {table}: ERROR ({e})")

        # ── Document files on disk (never touched by DB deletion) ──
        print("\n── Document files on disk ──")
        policy_ids = [r[0] for r in db.session.execute(
            text("SELECT id FROM insurance_policy WHERE user_id = :uid"),
            {"uid": user_id}).fetchall()]
        scheme_ids = [r[0] for r in db.session.execute(
            text("SELECT id FROM retirement_scheme WHERE user_id = :uid"),
            {"uid": user_id}).fetchall()]

        wealth_docs_dir = os.path.join(app.instance_path, "documents", "wealth", str(user_id))
        wealth_file_count = 0
        if os.path.isdir(wealth_docs_dir):
            wealth_file_count = sum(len(files) for _, _, files in os.walk(wealth_docs_dir))
        print(f"  wealth documents ({wealth_docs_dir}): {wealth_file_count} file(s)")

        insurance_file_count = 0
        for pid in policy_ids:
            d = os.path.join(app.instance_path, "documents", "insurance", str(pid))
            if os.path.isdir(d):
                insurance_file_count += sum(len(files) for _, _, files in os.walk(d))
        print(f"  insurance documents (across {len(policy_ids)} polic(ies)): {insurance_file_count} file(s)")

        retirement_file_count = 0
        for sid in scheme_ids:
            d = os.path.join(app.instance_path, "documents", "retirement", str(sid))
            if os.path.isdir(d):
                retirement_file_count += sum(len(files) for _, _, files in os.walk(d))
        print(f"  retirement documents (across {len(scheme_ids)} scheme(s)): {retirement_file_count} file(s)")

        print("\n" + "=" * 60)
        print("This was a READ-ONLY report. Nothing was changed.")
        print("=" * 60)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: py inventory_user.py <user_id>")
        sys.exit(1)
    inventory(int(sys.argv[1]))
