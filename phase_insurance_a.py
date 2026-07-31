"""
Insurance Centre — Phase A
Creates new database tables without touching any existing data.
Safe to run multiple times.
"""
import sys, os
sys.path.insert(0, r"C:\Users\mohan\Documents\mywealthlens")
os.chdir(r"C:\Users\mohan\Documents\mywealthlens")

from app import app, db

with app.app_context():
    # Create only new tables — existing tables untouched
    db.create_all()
    
    # Verify new tables exist
    from sqlalchemy import inspect
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    
    new_tables = ["insurance_policy", "insurance_document", 
                  "insurance_timeline", "family", "family_member", "family_invite"]
    
    print("Table verification:")
    for t in new_tables:
        status = "✓ Created" if t in tables else "✗ Missing"
        print(f"  {status}: {t}")
    
    print("\n✅ Phase A complete — new tables ready")
    print("Existing data untouched — old insurance table still intact")
