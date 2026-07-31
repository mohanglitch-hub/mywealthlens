"""
Insurance Centre — App Registration
=====================================
Patches app.py to register the insurance_centre blueprint
and update the models import.

Run once from project root: py insurance_centre/register.py
"""

import os
import sys

BASE = r"C:\Users\mohan\Documents\mywealthlens"
APP  = os.path.join(BASE, "app.py")

with open(APP, "r", encoding="utf-8") as f:
    content = f.read()

# ── Step 1: Add blueprint import after Flask imports ──────────────
blueprint_import = (
    "from insurance_centre import insurance_bp\n"
    "from insurance_centre.models import (\n"
    "    InsurancePolicy, InsuranceNominee, InsuranceMember,\n"
    "    InsuranceAddon, InsuranceDocument, InsuranceTimeline,\n"
    "    InsuranceCategory, InsuranceType, PolicyStatus,\n"
    "    PremiumFrequency, DocumentType, TimelineEvent\n"
    ")\n"
)

if "from insurance_centre import insurance_bp" not in content:
    # Insert after the last 'from models import' line
    old = "from models import db, User, Asset, MutualFund, Stock, Goal, UserProfile, Loan, Insurance, NetWorthHistory, EmergencyFund, TaxEntry80C, Family, FamilyMember, FamilyInvite"
    new = old + "\n" + blueprint_import
    content = content.replace(old, new)
    print("✓ Step 1: Blueprint import added")
else:
    print("✓ Step 1: Import already present — skipped")

# ── Step 2: Register blueprint with app ───────────────────────────
register_line = "app.register_blueprint(insurance_bp)\n"

if "app.register_blueprint(insurance_bp)" not in content:
    # Insert after db.init_app(app)
    old_init = "db.init_app(app)"
    new_init = "db.init_app(app)\n" + register_line
    content = content.replace(old_init, new_init)
    print("✓ Step 2: Blueprint registered with app")
else:
    print("✓ Step 2: Already registered — skipped")

# ── Step 3: Update navbar Insurance link in base.html ─────────────
BASE_HTML = os.path.join(BASE, "templates", "base.html")
with open(BASE_HTML, "r", encoding="utf-8") as f:
    base = f.read()

if 'href="/insurance-centre"' not in base:
    base = base.replace(
        'href="/insurance">Insurance',
        'href="/insurance-centre">Insurance'
    )
    with open(BASE_HTML, "w", encoding="utf-8") as f:
        f.write(base)
    print("✓ Step 3: Navbar updated — Insurance now points to /insurance-centre")
else:
    print("✓ Step 3: Navbar already updated — skipped")

# ── Save app.py ───────────────────────────────────────────────────
with open(APP, "w", encoding="utf-8") as f:
    f.write(content)
print("✓ app.py saved")

print("""
╔══════════════════════════════════════════════════════╗
║  Insurance Centre registration complete!             ║
║                                                      ║
║  Next steps:                                         ║
║  1. Run migration: py insurance_centre/migrate.py    ║
║  2. Restart Flask: py app.py                         ║
║  3. Visit: http://127.0.0.1:5000/insurance-centre    ║
╚══════════════════════════════════════════════════════╝
""")
