"""
Insurance Centre — Migration Script
=====================================
Phase 2: Creates new tables and migrates existing data
from old `insurance` table into new `insurance_policy` table.

Safe to run multiple times — uses INSERT OR IGNORE logic.
Run from project root: py insurance_centre/migrate.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db
from datetime import datetime


def run_migration():
    with app.app_context():
        print("=" * 60)
        print("Insurance Centre — Phase 2 Migration")
        print("=" * 60)

        # ── Step 1: Create new tables ─────────────────────────────
        print("\nStep 1: Creating new tables...")
        db.create_all()

        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()

        required_tables = [
            "insurance_policy",
            "insurance_nominee",
            "insurance_member",
            "insurance_addon",
            "insurance_document",
            "insurance_timeline",
            "family",
            "family_member",
            "family_invite",
        ]

        all_ok = True
        for t in required_tables:
            if t in tables:
                print(f"  ✓ {t}")
            else:
                print(f"  ✗ {t} — MISSING")
                all_ok = False

        if not all_ok:
            print("\n✗ Some tables failed to create. Aborting migration.")
            return False

        # ── Step 2: Check old insurance table ────────────────────
        print("\nStep 2: Checking old insurance table...")
        if "insurance" not in tables:
            print("  ℹ Old insurance table not found — skipping migration")
            print("\n✅ Phase 2 complete — new tables ready")
            return True

        # Count old records
        with db.engine.connect() as conn:
            result = conn.execute(db.text("SELECT COUNT(*) FROM insurance"))
            old_count = result.fetchone()[0]
        print(f"  Found {old_count} existing policies to migrate")

        if old_count == 0:
            print("  ℹ No records to migrate")
            print("\n✅ Phase 2 complete — new tables ready")
            return True

        # ── Step 3: Migrate old records ───────────────────────────
        print("\nStep 3: Migrating old policies...")

        from insurance_centre.models import (
            InsurancePolicy, InsuranceTimeline,
            InsuranceCategory, PolicyStatus,
            PremiumFrequency, TimelineEvent
        )

        with db.engine.connect() as conn:
            result = conn.execute(db.text("SELECT * FROM insurance"))
            old_records = [dict(r._mapping) for r in result.fetchall()]
        migrated = 0
        skipped  = 0

        # Map old insurance_type to new category
        category_map = {
            "term":    InsuranceCategory.LIFE,
            "health":  InsuranceCategory.HEALTH,
            "vehicle": InsuranceCategory.MOTOR,
            "home":    InsuranceCategory.PROPERTY,
            "other":   InsuranceCategory.GENERAL,
        }

        type_map = {
            "term":    "Term Insurance",
            "health":  "Individual Health Insurance",
            "vehicle": "Car Insurance (Comprehensive)",
            "home":    "Home Insurance",
            "other":   "Other (Custom)",
        }

        for row in old_records:
            # Check if already migrated (by policy_number + user_id)
            existing = InsurancePolicy.query.filter_by(
                user_id=row["user_id"],
                policy_number=row.get("policy_number") or None,
                insurer=row.get("insurer", "Unknown"),
            ).first()

            if existing:
                print(f"  ⟳ Skipping (already migrated): {row.get('insurer')}")
                skipped += 1
                continue

            old_type = row.get("insurance_type", "other")
            category = category_map.get(old_type, InsuranceCategory.GENERAL)
            ins_type = type_map.get(old_type, "Other (Custom)")

            def to_date(val):
                if not val:
                    return None
                if hasattr(val, 'year'):
                    return val
                try:
                    from datetime import datetime as _dt
                    return _dt.strptime(str(val)[:10], "%Y-%m-%d").date()
                except Exception:
                    return None

            policy = InsurancePolicy(
                user_id           = row["user_id"],
                category          = category,
                insurance_type    = ins_type,
                custom_type       = row.get("insurer") if ins_type == "Other (Custom)" else None,
                insurer           = row.get("insurer") or "Unknown",
                policy_number     = row.get("policy_number") or None,
                sum_assured       = float(row.get("cover_amount") or 0),
                premium_amount    = float(row.get("annual_premium") or 0),
                premium_frequency = PremiumFrequency.YEARLY,
                status            = PolicyStatus.ACTIVE,
                renewal_date      = to_date(row.get("renewal_date")),
                notes             = row.get("notes") or None,
                created_at        = datetime.utcnow(),
            )
            db.session.add(policy)
            db.session.flush()

            # Log migration in timeline
            timeline = InsuranceTimeline(
                policy_id   = policy.id,
                user_id     = row["user_id"],
                event_type  = TimelineEvent.CREATED,
                description = f"Migrated from old insurance module (original type: {old_type})",
                created_at  = datetime.utcnow(),
            )
            db.session.add(timeline)
            migrated += 1
            print(f"  ✓ Migrated: {row.get('insurer')} ({category})")

        db.session.commit()

        # ── Step 4: Summary ───────────────────────────────────────
        print(f"\n{'=' * 60}")
        print(f"Migration complete:")
        print(f"  ✓ Migrated:  {migrated} policies")
        print(f"  ⟳ Skipped:   {skipped} (already existed)")
        print(f"  ℹ Old table: 'insurance' kept intact as backup")
        print(f"\nNext steps:")
        print(f"  1. Verify data at /insurance-centre")
        print(f"  2. Test all features")
        print(f"  3. Drop old table only after full verification")
        print(f"{'=' * 60}")
        print(f"\n✅ Phase 2 migration complete")
        return True


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)