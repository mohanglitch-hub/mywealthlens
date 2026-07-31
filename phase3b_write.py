"""
Phase 3b — Net Worth History, NPS, Inflation Goals
Deploys all changes directly to your templates and project files.
"""
import os, shutil

BASE = r"C:\Users\mohan\Documents\mywealthlens"
TMPL = os.path.join(BASE, "templates")

# ── PART 1: Add inflation_rate to Goal model ──────────────────────────────────
models_path = os.path.join(BASE, "models.py")
with open(models_path, "r", encoding="utf-8") as f:
    models = f.read()

if "inflation_rate" not in models:
    models = models.replace(
        "    annual_return   = db.Column(db.Float, default=12.0)\n    created_at",
        "    annual_return   = db.Column(db.Float, default=12.0)\n    inflation_rate  = db.Column(db.Float, default=0)\n    created_at"
    )
    with open(models_path, "w", encoding="utf-8") as f:
        f.write(models)
    print("✓ PART 1: inflation_rate added to Goal model")
else:
    print("✓ PART 1: inflation_rate already in Goal model — skipped")

# ── PART 2: Write dashboard.html ──────────────────────────────────────────────
dashboard_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
dashboard_dst = os.path.join(TMPL, "dashboard.html")
if os.path.exists(dashboard_src):
    shutil.copy(dashboard_src, dashboard_dst)
    print("✓ PART 2: dashboard.html deployed")
else:
    print("✗ PART 2: dashboard.html not found")

# ── PART 3: Write goals.html ──────────────────────────────────────────────────
goals_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goals.html")
goals_dst = os.path.join(TMPL, "goals.html")
if os.path.exists(goals_src):
    shutil.copy(goals_src, goals_dst)
    print("✓ PART 3: goals.html deployed")
else:
    print("✗ PART 3: goals.html not found")

# ── PART 4: Write assets.html ─────────────────────────────────────────────────
assets_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets_fixed.html")
assets_dst = os.path.join(TMPL, "assets.html")
if os.path.exists(assets_src):
    shutil.copy(assets_src, assets_dst)
    print("✓ PART 4: assets.html deployed with NPS")
else:
    print("✗ PART 4: assets_fixed.html not found")

# ── PART 5: Update app.py ─────────────────────────────────────────────────────
app_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
app_dst = os.path.join(BASE, "app.py")
if os.path.exists(app_src):
    shutil.copy(app_src, app_dst)
    print("✓ PART 5: app.py deployed")
else:
    print("✗ PART 5: app.py not found")

print("""
All done! Restart Flask:
  py app.py

New features active:
  • Net Worth History — stacked area chart on dashboard (builds over days)
  • Save Snapshot button — manual snapshot anytime
  • NPS — new asset type under Govt Schemes
  • Inflation-adjusted Goals — set inflation % per goal
""")
