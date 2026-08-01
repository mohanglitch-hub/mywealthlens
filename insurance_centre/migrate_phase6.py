"""
Insurance Centre — Phase 6 Migration
======================================
1. Adds agent_name and agent_contact columns to insurance_policy
2. Safe to run multiple times
Run from project root: py insurance_centre/migrate_phase6.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app, db

with app.app_context():
    print("Phase 6 Migration — Adding agent columns")

    with db.engine.connect() as conn:
        # Check existing columns
        result = conn.execute(db.text("PRAGMA table_info(insurance_policy)"))
        existing = [row[1] for row in result.fetchall()]

        if "agent_name" not in existing:
            conn.execute(db.text(
                "ALTER TABLE insurance_policy ADD COLUMN agent_name VARCHAR(200)"))
            conn.commit()
            print("  ✓ agent_name column added")
        else:
            print("  ✓ agent_name already exists")

        if "agent_contact" not in existing:
            conn.execute(db.text(
                "ALTER TABLE insurance_policy ADD COLUMN agent_contact VARCHAR(50)"))
            conn.commit()
            print("  ✓ agent_contact column added")
        else:
            print("  ✓ agent_contact already exists")

    print("\n✅ Phase 6 migration complete")
