"""
User Data Inventory (read-only)
==================================
Reports exactly what's tied to a given user_id across every module —
directly-owned tables, and tables owned indirectly via a policy/
scheme (e.g. insurance documents, which key off policy_id, not
user_id directly). Also checks for uploaded document files on disk,
since those are never touched by a database-level deletion at all.

This script makes NO changes. It's purely so you can see what's
actually there before deciding whether/how to delete an account.

Usage: py inventory_user.py <user_id>
Example: py inventory_user.py 2
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db


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

        # ── Direct user_id-owned tables, across every module ──
        direct_tables = [
            ("mutual_fund", "user_id"),
            ("stock", "user_id"),
            ("goal", "user_id"),
            ("user_profile", "user_id"),
            ("loan", "user_id"),
            ("net_worth_history", "user_id"),
            ("family", "created_by"),
            ("family_member", "user_id"),
            ("wealth_asset", "user_id"),
            ("wealth_liability", "user_id"),
            ("wealth_value_snapshot", "user_id"),
            ("wealth_snapshot", "user_id"),
            ("wealth_snapshot_log", "user_id"),
            ("wealth_document", "user_id"),
            ("insurance_policy", "user_id"),
            ("retirement_scheme", "user_id"),
        ]

        print("\n── Directly-owned rows ──")
        for table, col in direct_tables:
            try:
                count = db.session.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE {col} = :uid"),
                    {"uid": user_id}).scalar()
                marker = "  " if count == 0 else "→ "
                print(f"{marker}{table}: {count}")
            except Exception as e:
                print(f"  {table}: ERROR ({e})")

        # family_invite is keyed by email, not user_id — check separately
        invite_count = db.session.execute(
            text("SELECT COUNT(*) FROM family_invite WHERE email = :email"),
            {"email": result[2]}).scalar()
        marker = "  " if invite_count == 0 else "→ "
        print(f"{marker}family_invite (by email, pending invites TO this address): {invite_count}")

        # ── Indirectly-owned rows (via policy_id / scheme_id) ──
        print("\n── Indirectly-owned rows (via policy/scheme) ──")
        policy_ids = [r[0] for r in db.session.execute(
            text("SELECT id FROM insurance_policy WHERE user_id = :uid"),
            {"uid": user_id}).fetchall()]
        scheme_ids = [r[0] for r in db.session.execute(
            text("SELECT id FROM retirement_scheme WHERE user_id = :uid"),
            {"uid": user_id}).fetchall()]

        indirect_tables = [
            ("insurance_nominee", "policy_id", policy_ids),
            ("insurance_member", "policy_id", policy_ids),
            ("insurance_addon", "policy_id", policy_ids),
            ("insurance_document", "policy_id", policy_ids),
            ("insurance_timeline", "policy_id", policy_ids),
            ("retirement_contribution", "scheme_id", scheme_ids),
            ("retirement_balance_snapshot", "scheme_id", scheme_ids),
            ("retirement_timeline", "scheme_id", scheme_ids),
            ("retirement_scheme_nominee", "scheme_id", scheme_ids),
            ("retirement_document", "scheme_id", scheme_ids),
        ]
        for table, col, ids in indirect_tables:
            if not ids:
                print(f"  {table}: 0 (no parent policy/scheme)")
                continue
            placeholders = ",".join(str(i) for i in ids)
            count = db.session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {col} IN ({placeholders})")).scalar()
            marker = "  " if count == 0 else "→ "
            print(f"{marker}{table}: {count}")

        # ── Document files on disk (never touched by DB deletion) ──
        print("\n── Document files on disk ──")
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