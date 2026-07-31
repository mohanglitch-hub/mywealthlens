"""
models.py — Database Models
============================
Tables:
  1. User             — registered users
  2. Asset            — manual assets (gold, silver, real estate etc.)
  3. MutualFund       — parsed from CAMS/KFintech CAS PDF
  4. Stock            — parsed from CDSL/NSDL CAS PDF
  5. Goal             — financial goals
  6. UserProfile      — life stage profile
  7. Loan             — loans and liabilities
  8. Insurance        — insurance policies
  9. NetWorthHistory  — daily net worth snapshots
  10. EmergencyFund   — emergency fund target
  11. TaxEntry80C     — manual 80C entries
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "user"
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(150), nullable=False)
    email    = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)
    assets       = db.relationship("Asset",      backref="owner", lazy=True, cascade="all, delete-orphan")
    mutual_funds = db.relationship("MutualFund", backref="owner", lazy=True, cascade="all, delete-orphan")
    stocks       = db.relationship("Stock",      backref="owner", lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User {self.email}>"


class Asset(db.Model):
    __tablename__ = "asset"
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    category       = db.Column(db.String(50), nullable=False)
    name           = db.Column(db.String(200), nullable=True)
    grams          = db.Column(db.Float, nullable=True)
    price_per_gram = db.Column(db.Float, nullable=True)
    sq_ft          = db.Column(db.Float, nullable=True)
    institution    = db.Column(db.String(200), nullable=True)
    value          = db.Column(db.Float, nullable=False, default=0)
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MutualFund(db.Model):
    __tablename__ = "mutual_fund"
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    folio       = db.Column(db.String(50),  nullable=True)
    amc         = db.Column(db.String(200), nullable=True)
    scheme      = db.Column(db.String(300), nullable=False)
    units       = db.Column(db.Float, nullable=False)
    nav         = db.Column(db.Float, nullable=True)
    value       = db.Column(db.Float, nullable=False)
    source      = db.Column(db.String(20), default="cams")
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<MutualFund {self.scheme} units={self.units}>"


class Stock(db.Model):
    __tablename__ = "stock"
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    isin             = db.Column(db.String(20),  nullable=False)
    name             = db.Column(db.String(300), nullable=False)
    quantity         = db.Column(db.Float, nullable=False)
    buy_price        = db.Column(db.Float, nullable=True)
    live_price       = db.Column(db.Float, nullable=True)
    value            = db.Column(db.Float, nullable=False)
    ticker           = db.Column(db.String(20), nullable=True)
    source           = db.Column(db.String(20), default="cdsl")
    uploaded_at      = db.Column(db.DateTime, default=datetime.utcnow)
    price_updated_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<Stock {self.name} qty={self.quantity}>"


class Goal(db.Model):
    __tablename__ = "goal"
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name            = db.Column(db.String(200), nullable=False)
    emoji           = db.Column(db.String(10),  nullable=True)
    target_amt      = db.Column(db.Float, nullable=False)
    target_year     = db.Column(db.Integer, nullable=False)
    current_savings = db.Column(db.Float, default=0)
    monthly_sip     = db.Column(db.Float, default=0)
    annual_return   = db.Column(db.Float, default=12.0)
    inflation_rate  = db.Column(db.Float, default=0)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Goal {self.name}>"


class UserProfile(db.Model):
    __tablename__ = "user_profile"
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    age            = db.Column(db.Integer, nullable=False)
    marital_status = db.Column(db.String(20), default="single")
    dependents     = db.Column(db.Integer, default=0)
    updated_at     = db.Column(db.DateTime, default=db.func.now(), onupdate=db.func.now())


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

    def __repr__(self):
        return f"<Loan {self.loan_type} {self.lender}>"


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

    def __repr__(self):
        return f"<Insurance {self.insurance_type} {self.insurer}>"


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

    def __repr__(self):
        return f"<NetWorthHistory {self.snapshot_date} total={self.total}>"


class EmergencyFund(db.Model):
    __tablename__ = "emergency_fund"
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    monthly_expenses = db.Column(db.Float, nullable=False)
    target_months    = db.Column(db.Integer, default=6)
    target_amount    = db.Column(db.Float, nullable=False)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<EmergencyFund target={self.target_amount}>"


class TaxEntry80C(db.Model):
    __tablename__ = "tax_entry_80c"
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    label      = db.Column(db.String(200), nullable=False)
    amount     = db.Column(db.Float, nullable=False)
    fy         = db.Column(db.String(10), default="2024-25")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<TaxEntry80C {self.label} {self.amount}>"


class Family(db.Model):
    __tablename__ = "family"
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    members    = db.relationship("FamilyMember", backref="family",
                                 lazy=True, cascade="all, delete-orphan")
    invites    = db.relationship("FamilyInvite", backref="family",
                                 lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Family {self.name}>"


class FamilyMember(db.Model):
    __tablename__ = "family_member"
    id        = db.Column(db.Integer, primary_key=True)
    family_id = db.Column(db.Integer, db.ForeignKey("family.id"), nullable=False)
    user_id   = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    role      = db.Column(db.String(20), default="member")
    name      = db.Column(db.String(150), nullable=True)
    email     = db.Column(db.String(150), nullable=True)
    is_manual = db.Column(db.Boolean, default=False)
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("family_id", "user_id", name="uq_family_member"),
    )

    def __repr__(self):
        return f"<FamilyMember user={self.user_id} role={self.role}>"


class FamilyInvite(db.Model):
    __tablename__ = "family_invite"
    id         = db.Column(db.Integer, primary_key=True)
    family_id  = db.Column(db.Integer, db.ForeignKey("family.id"), nullable=False)
    email      = db.Column(db.String(150), nullable=False)
    role       = db.Column(db.String(20), default="member")
    token      = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    accepted   = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<FamilyInvite {self.email}>"