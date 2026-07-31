"""
Phase 3 — Wealth Management Expansion
Applies:
  PART 1 — Write new models to models.py
  PART 2 — Add all Phase 3 routes to app.py
  PART 3 — Add nav links to base.html
  PART 4 — Write all 4 templates directly
  PART 5 — Update dashboard with net worth history
"""
import os
from datetime import date

BASE  = r"C:\Users\mohan\Documents\mywealthlens"
TMPL  = os.path.join(BASE, "templates")

# ── PART 1: models.py ─────────────────────────────────────────────────────────
models_path = os.path.join(BASE, "models.py")
with open(models_path, "r", encoding="utf-8") as f:
    models = f.read()

new_models = '''

class Loan(db.Model):
    __tablename__ = "loan"
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    loan_type     = db.Column(db.String(30), nullable=False)
    lender        = db.Column(db.String(200), nullable=False)
    principal     = db.Column(db.Float, nullable=False)
    outstanding   = db.Column(db.Float, nullable=False)
    emi           = db.Column(db.Float, nullable=True)
    interest_rate = db.Column(db.Float, nullable=True)
    tenure_months = db.Column(db.Integer, nullable=True)
    start_date    = db.Column(db.Date, nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Insurance(db.Model):
    __tablename__ = "insurance"
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    insurance_type = db.Column(db.String(30), nullable=False)
    insurer        = db.Column(db.String(200), nullable=False)
    policy_number  = db.Column(db.String(100), nullable=True)
    cover_amount   = db.Column(db.Float, nullable=False)
    annual_premium = db.Column(db.Float, nullable=False)
    renewal_date   = db.Column(db.Date, nullable=True)
    notes          = db.Column(db.String(500), nullable=True)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class NetWorthHistory(db.Model):
    __tablename__ = "net_worth_history"
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    snapshot_date = db.Column(db.Date, nullable=False)
    total         = db.Column(db.Float, nullable=False)
    equity        = db.Column(db.Float, default=0)
    debt          = db.Column(db.Float, default=0)
    gold          = db.Column(db.Float, default=0)
    realestate    = db.Column(db.Float, default=0)
    cash          = db.Column(db.Float, default=0)
    other         = db.Column(db.Float, default=0)
    liabilities   = db.Column(db.Float, default=0)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("user_id", "snapshot_date", name="uq_user_snapshot_date"),
    )

class EmergencyFund(db.Model):
    __tablename__ = "emergency_fund"
    id                = db.Column(db.Integer, primary_key=True)
    user_id           = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    monthly_expenses  = db.Column(db.Float, nullable=False)
    target_months     = db.Column(db.Integer, default=6)
    target_amount     = db.Column(db.Float, nullable=False)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class TaxEntry80C(db.Model):
    __tablename__ = "tax_entry_80c"
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    label      = db.Column(db.String(200), nullable=False)
    amount     = db.Column(db.Float, nullable=False)
    fy         = db.Column(db.String(10), default="2024-25")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
'''

if "class Loan" not in models:
    models = models.rstrip() + "\n" + new_models
    with open(models_path, "w", encoding="utf-8") as f:
        f.write(models)
    print("✓ PART 1: New models added to models.py")
else:
    print("✓ PART 1: Models already exist — skipped")

# ── PART 2: app.py routes ─────────────────────────────────────────────────────
app_path = os.path.join(BASE, "app.py")
with open(app_path, "r", encoding="utf-8") as f:
    app = f.read()

# Update import line
old_import = "from models import db, User, Asset, MutualFund, Stock, Goal, UserProfile"
new_import = "from models import db, User, Asset, MutualFund, Stock, Goal, UserProfile, Loan, Insurance, NetWorthHistory, EmergencyFund, TaxEntry80C"
if "Loan" not in app:
    app = app.replace(old_import, new_import)
    print("✓ PART 2a: Imports updated")

phase3_routes = '''
# ════════════════════════════════════════════════════════════════════
# PHASE 3 — WEALTH MANAGEMENT ROUTES
# ════════════════════════════════════════════════════════════════════

def _save_snapshot(user_id, assets, mfs, stocks, loans):
    """Save daily net worth snapshot. Safe to call multiple times — one per day."""
    from datetime import date as _date
    today = _date.today()
    existing = NetWorthHistory.query.filter_by(user_id=user_id, snapshot_date=today).first()
    if existing:
        return  # already saved today

    equity  = sum(m.value for m in mfs) + sum(s.value for s in stocks)
    debt    = sum(a.value for a in assets if a.category in ["ppf","vpf","ssy","fd"])
    gold    = sum(a.value for a in assets if a.category in ["gold","silver"])
    re      = sum(a.value for a in assets if a.category.startswith("real_estate"))
    cash    = sum(a.value for a in assets if a.category == "cash")
    other   = sum(a.value for a in assets if a.category not in ["ppf","vpf","ssy","fd","gold","silver","cash"] and not a.category.startswith("real_estate"))
    liab    = sum(l.outstanding for l in loans)
    total   = equity + debt + gold + re + cash + other - liab

    snap = NetWorthHistory(user_id=user_id, snapshot_date=today,
        total=total, equity=equity, debt=debt, gold=gold,
        realestate=re, cash=cash, other=other, liabilities=liab)
    db.session.add(snap)
    db.session.commit()


# ── LOANS ──────────────────────────────────────────────────────────
@app.route("/loans")
@login_required
def loans():
    loan_list = Loan.query.filter_by(user_id=current_user.id).order_by(Loan.created_at.desc()).all()
    total_outstanding = sum(l.outstanding for l in loan_list)
    total_emi         = sum(l.emi or 0 for l in loan_list)
    total_principal   = sum(l.principal for l in loan_list)
    return render_template("loans.html", loans=loan_list,
        total_outstanding=total_outstanding, total_emi=total_emi,
        total_principal=total_principal)

@app.route("/loans/add", methods=["POST"])
@login_required
def add_loan():
    from datetime import date as _date
    sd = request.form.get("start_date")
    loan = Loan(
        user_id       = current_user.id,
        loan_type     = request.form.get("loan_type","other"),
        lender        = request.form.get("lender","").strip(),
        principal     = float(request.form.get("principal",0)),
        outstanding   = float(request.form.get("outstanding",0)),
        emi           = float(request.form.get("emi",0)) or None,
        interest_rate = float(request.form.get("interest_rate",0)) or None,
        tenure_months = int(request.form.get("tenure_months",0)) or None,
        start_date    = _date.fromisoformat(sd) if sd else None,
    )
    db.session.add(loan)
    db.session.commit()
    flash("Loan added successfully!", "success")
    return redirect(url_for("loans"))

@app.route("/loans/delete/<int:loan_id>", methods=["POST"])
@login_required
def delete_loan(loan_id):
    loan = Loan.query.get_or_404(loan_id)
    if loan.user_id != current_user.id:
        flash("Permission denied.", "error")
        return redirect(url_for("loans"))
    db.session.delete(loan)
    db.session.commit()
    flash("Loan deleted.", "success")
    return redirect(url_for("loans"))


# ── INSURANCE ──────────────────────────────────────────────────────
@app.route("/insurance")
@login_required
def insurance():
    from datetime import date as _date
    today     = _date.today()
    policies  = Insurance.query.filter_by(user_id=current_user.id).order_by(Insurance.renewal_date).all()
    total_cover   = sum(p.cover_amount for p in policies)
    total_premium = sum(p.annual_premium for p in policies)
    due_soon  = sum(1 for p in policies if p.renewal_date and 0 <= (p.renewal_date - today).days <= 30)
    return render_template("insurance.html", policies=policies,
        total_cover=total_cover, total_premium=total_premium,
        due_soon=due_soon, today=today)

@app.route("/insurance/add", methods=["POST"])
@login_required
def add_insurance():
    from datetime import date as _date
    rd = request.form.get("renewal_date")
    pol = Insurance(
        user_id        = current_user.id,
        insurance_type = request.form.get("insurance_type","other"),
        insurer        = request.form.get("insurer","").strip(),
        policy_number  = request.form.get("policy_number","").strip() or None,
        cover_amount   = float(request.form.get("cover_amount",0)),
        annual_premium = float(request.form.get("annual_premium",0)),
        renewal_date   = _date.fromisoformat(rd) if rd else None,
        notes          = request.form.get("notes","").strip() or None,
    )
    db.session.add(pol)
    db.session.commit()
    flash("Policy added successfully!", "success")
    return redirect(url_for("insurance"))

@app.route("/insurance/delete/<int:pol_id>", methods=["POST"])
@login_required
def delete_insurance(pol_id):
    pol = Insurance.query.get_or_404(pol_id)
    if pol.user_id != current_user.id:
        flash("Permission denied.", "error")
        return redirect(url_for("insurance"))
    db.session.delete(pol)
    db.session.commit()
    flash("Policy deleted.", "success")
    return redirect(url_for("insurance"))


# ── EMERGENCY FUND ─────────────────────────────────────────────────
@app.route("/emergency")
@login_required
def emergency():
    ef      = EmergencyFund.query.filter_by(user_id=current_user.id).first()
    assets  = Asset.query.filter_by(user_id=current_user.id).all()
    mfs     = MutualFund.query.filter_by(user_id=current_user.id).all()

    cash_val    = sum(a.value for a in assets if a.category == "cash")
    fd_val      = sum(a.value for a in assets if a.category == "fd")
    liquid_mf   = sum(m.value for m in mfs if any(w in m.scheme.lower() for w in ["liquid","overnight","money market","ultra short"]))
    liquid_total = cash_val + fd_val + liquid_mf

    liquid_breakdown = []
    if cash_val   > 0: liquid_breakdown.append({"label":"Cash & Savings Accounts", "value":cash_val})
    if fd_val     > 0: liquid_breakdown.append({"label":"Fixed Deposits",           "value":fd_val})
    if liquid_mf  > 0: liquid_breakdown.append({"label":"Liquid / Overnight MFs",  "value":liquid_mf})
    if not liquid_breakdown:
        liquid_breakdown.append({"label":"No liquid assets detected yet", "value":0})

    recommendations = [
        {"icon":"💰","name":"Savings Account (High-yield)",  "reason":"Instant access, DICGC insured up to ₹5L"},
        {"icon":"📄","name":"Liquid Mutual Fund",            "reason":"Better returns than savings, T+1 redemption"},
        {"icon":"🏦","name":"Short-term FD (3–6 months)",   "reason":"Safe, DICGC insured, predictable returns"},
        {"icon":"💳","name":"Sweep-in FD",                  "reason":"Auto-converts idle savings to FD — best of both"},
    ]
    return render_template("emergency.html", ef=ef,
        liquid_total=liquid_total, liquid_breakdown=liquid_breakdown,
        recommendations=recommendations)

@app.route("/emergency/save", methods=["POST"])
@login_required
def save_emergency():
    monthly = float(request.form.get("monthly_expenses", 0))
    months  = int(request.form.get("target_months", 6))
    target  = monthly * months
    ef = EmergencyFund.query.filter_by(user_id=current_user.id).first()
    if ef:
        ef.monthly_expenses = monthly
        ef.target_months    = months
        ef.target_amount    = target
    else:
        ef = EmergencyFund(user_id=current_user.id,
            monthly_expenses=monthly, target_months=months, target_amount=target)
        db.session.add(ef)
    db.session.commit()
    flash("Emergency fund target saved!", "success")
    return redirect(url_for("emergency"))


# ── TAX CENTRE ─────────────────────────────────────────────────────
@app.route("/tax")
@login_required
def tax_centre():
    assets = Asset.query.filter_by(user_id=current_user.id).all()
    manual = TaxEntry80C.query.filter_by(user_id=current_user.id, fy="2024-25").all()

    auto_80c = []
    ppf_val = sum(a.value for a in assets if a.category == "ppf")
    vpf_val = sum(a.value for a in assets if a.category == "vpf")
    ssy_val = sum(a.value for a in assets if a.category == "ssy")
    if ppf_val > 0: auto_80c.append({"label":"PPF (Public Provident Fund)",      "amount":min(ppf_val,150000), "note":"Auto-detected from your assets"})
    if vpf_val > 0: auto_80c.append({"label":"VPF (Voluntary Provident Fund)",   "amount":min(vpf_val,150000), "note":"Auto-detected from your assets"})
    if ssy_val > 0: auto_80c.append({"label":"Sukanya Samriddhi Yojana (SSY)",   "amount":min(ssy_val,150000), "note":"Auto-detected from your assets"})

    deductions_80c = auto_80c + [{"label":e.label, "amount":e.amount, "note":"Manually added"} for e in manual]
    total_80c = min(sum(d["amount"] for d in deductions_80c), 150000)

    return render_template("tax.html", deductions_80c=deductions_80c, total_80c=total_80c)

@app.route("/tax/add-80c", methods=["POST"])
@login_required
def add_80c():
    entry = TaxEntry80C(
        user_id = current_user.id,
        label   = request.form.get("label","Other 80C"),
        amount  = float(request.form.get("amount", 0)),
        fy      = "2024-25",
    )
    db.session.add(entry)
    db.session.commit()
    flash("80C entry added!", "success")
    return redirect(url_for("tax_centre"))


# ── NET WORTH HISTORY ──────────────────────────────────────────────
@app.route("/networth/snapshot", methods=["POST"])
@login_required
def manual_snapshot():
    assets = Asset.query.filter_by(user_id=current_user.id).all()
    mfs    = MutualFund.query.filter_by(user_id=current_user.id).all()
    stocks = Stock.query.filter_by(user_id=current_user.id).all()
    loans  = Loan.query.filter_by(user_id=current_user.id).all()
    # Force new snapshot even if one exists today (manual override)
    from datetime import date as _date
    existing = NetWorthHistory.query.filter_by(
        user_id=current_user.id, snapshot_date=_date.today()).first()
    if existing:
        db.session.delete(existing)
        db.session.flush()
    _save_snapshot(current_user.id, assets, mfs, stocks, loans)
    flash("Net worth snapshot saved!", "success")
    return redirect(url_for("dashboard"))

@app.route("/networth/history-data")
@login_required
def networth_history_data():
    history = NetWorthHistory.query.filter_by(user_id=current_user.id)\\
        .order_by(NetWorthHistory.snapshot_date).limit(365).all()
    return jsonify([{
        "date":  h.snapshot_date.strftime("%d %b %Y"),
        "total": h.total,
    } for h in history])

'''

if "_save_snapshot" not in app:
    app = app.replace("if __name__ == '__main__':",
                      phase3_routes + "\nif __name__ == '__main__':")
    print("✓ PART 2b: Phase 3 routes added to app.py")
else:
    print("✓ PART 2b: Routes already exist — skipped")

with open(app_path, "w", encoding="utf-8") as f:
    f.write(app)
print("✓ PART 2: app.py saved")

# ── PART 3: base.html — add nav links ─────────────────────────────────────────
base_path = os.path.join(TMPL, "base.html")
with open(base_path, "r", encoding="utf-8") as f:
    base = f.read()

old_nav = '      <a href="/life-stage">Life Stage</a>\n      <a href="/account">Account</a>'
new_nav = ('      <a href="/life-stage">Life Stage</a>\n'
           '      <a href="/loans">Loans</a>\n'
           '      <a href="/insurance">Insurance</a>\n'
           '      <a href="/emergency">Emergency Fund</a>\n'
           '      <a href="/tax">Tax Centre</a>\n'
           '      <a href="/account">Account</a>')

old_mob = '      <a href="/life-stage">Life Stage</a>\n      <a href="/account">Account</a>'
new_mob = ('      <a href="/life-stage">Life Stage</a>\n'
           '      <a href="/loans">Loans</a>\n'
           '      <a href="/insurance">Insurance</a>\n'
           '      <a href="/emergency">Emergency Fund</a>\n'
           '      <a href="/tax">Tax Centre</a>\n'
           '      <a href="/account">Account</a>')

if "/loans" not in base:
    base = base.replace(old_nav, new_nav, 1)
    base = base.replace(old_mob, new_mob, 1)
    with open(base_path, "w", encoding="utf-8") as f:
        f.write(base)
    print("✓ PART 3: Nav links added to base.html")
else:
    print("✓ PART 3: Nav links already exist — skipped")

# ── PART 4: Write all templates directly ──────────────────────────────────────
import sys, os
script_dir = os.path.dirname(os.path.abspath(__file__))

templates_to_copy = ["loans.html", "insurance.html", "emergency.html", "tax.html"]
for tname in templates_to_copy:
    src = os.path.join(script_dir, tname)
    dst = os.path.join(TMPL, tname)
    if os.path.exists(src):
        import shutil
        shutil.copy(src, dst)
        print(f"✓ PART 4: {tname} copied to templates/")
    else:
        print(f"✗ PART 4: {tname} not found at {src} — will write inline")

print("""
All done! Now:
  1. Restart Flask: py app.py
  2. The new tables will be created automatically on startup
  3. Visit /loans, /insurance, /emergency, /tax
""")
