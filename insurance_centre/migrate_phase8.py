"""
Insurance Centre — Phase 8 Migration
======================================
Adds: cashless_available, claim_history, property_type, policy_type
Safe to run multiple times.
Run from project root: py insurance_centre/migrate_phase8.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db

with app.app_context():
    print("Phase 8 Migration — Adding category-specific columns")

    new_columns = [
        ("cashless_available", "VARCHAR(5)"),
        ("claim_history",      "TEXT"),
        ("property_type",      "VARCHAR(50)"),
        ("policy_type",        "VARCHAR(20)"),
    ]

    with db.engine.connect() as conn:
        result   = conn.execute(db.text("PRAGMA table_info(insurance_policy)"))
        existing = [row[1] for row in result.fetchall()]

        for col_name, col_type in new_columns:
            if col_name not in existing:
                conn.execute(db.text(
                    f"ALTER TABLE insurance_policy ADD COLUMN {col_name} {col_type}"
                ))
                conn.commit()
                print(f"  ✓ {col_name} added")
            else:
                print(f"  ✓ {col_name} already exists")

    print("\n✅ Phase 8 migration complete")
