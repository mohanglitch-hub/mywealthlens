"""
models.py — Database Models
============================
Tables:
  1. User             — registered users
  2. MutualFund       — parsed from CAMS/KFintech CAS PDF
  3. Stock            — parsed from CDSL/NSDL CAS PDF
  4. Goal              — financial goals
  5. UserProfile      — life stage profile
  6. Insurance        — insurance policies
  7. NetWorthHistory  — daily net worth snapshots (see its own class
                          docstring for why this coexists with
                          wealth.models.WealthSnapshot — Phase N)

Note (Phase H): the legacy Asset model/table has been retired. The
authoritative Assets system is wealth.models.WealthAsset. See the
Phase H final report for the full audit and migration decision.

Note (Phase N): the legacy Loan model/table has also been retired —
confirmed unused (no imports, no FKs, no live queries anywhere in
the codebase) and confirmed empty (0 rows on both real user accounts
at time of removal). The authoritative Liabilities system is
wealth.models.WealthLiability. See wealth/migrate_phase_n.py for the
table-drop migration and the Phase N final report for the full audit.
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
    mutual_funds = db.relationship("MutualFund", backref="owner", lazy=True, cascade="all, delete-orphan")
    stocks       = db.relationship("Stock",      backref="owner", lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User {self.email}>"


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


class NetWorthHistory(db.Model):
    """
    Phase N architectural decision (Sections 7/8 of the Phase N spec):
    this table and wealth.models.WealthSnapshot both track "net worth
    over time" and have been repeatedly flagged as a possible
    accidental duplicate architecture across Phases H, I, and M.

    Investigated with evidence, not assumption. Conclusion: KEEP BOTH
    — they have materially distinct, non-overlapping responsibilities
    (spec's Option C), not a legacy-vs-current split:

      NetWorthHistory  — powers the MAIN APP dashboard's (/dashboard)
                          trend chart. Scope INCLUDES MutualFund/Stock
                          (CAS-imported broker holdings — a feature
                          entirely outside the Wealth Centre) alongside
                          WealthAsset/WealthLiability category totals.
                          Written on page-load (deduped per calendar
                          day) by _save_snapshot() in app.py.

      WealthSnapshot   — powers the WEALTH CENTRE's own History page
                          (/wealth/history). Scope is STRICTLY
                          WealthAsset + WealthLiability — deliberately
                          excludes MutualFund/Stock (Phase D's
                          established Wealth-Centre boundary). Written
                          only by explicit user action or the Phase I
                          scheduled CLI, never by a page visit.

    Because NetWorthHistory's scope genuinely includes non-Wealth-
    Centre data (CAS investment holdings), merging it into
    WealthSnapshot would either silently change what WealthSnapshot
    has always meant (Phase F's explicit scope), or require dropping
    CAS holdings from the main dashboard's trend chart — a real,
    working, independent feature, not legacy debt. Neither is an
    acceptable side effect of an internal architecture cleanup.
    Kept as two deliberately separate systems, now documented as such
    on both classes.
    """
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