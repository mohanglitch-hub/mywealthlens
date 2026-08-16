"""
Wealth — Models
==================
Phase A: Foundation for the Wealth module's database architecture.

CRITICAL ARCHITECTURAL RULE — read before touching this file:
  This module is completely independent from the existing Assets
  module (models.py's `Asset` class). No shared tables, no foreign
  keys in either direction, no compatibility layer, no data sync.
  The two systems must remain independently removable. Assets stays
  untouched until Wealth is fully built, tested, and approved —
  at which point Assets will be deleted, not merged into Wealth.

Tables (Phase A):
  1. WealthAsset          — things the user owns
  2. WealthLiability       — things the user owes
  3. WealthValueSnapshot   — foundation for future historical values
                              (Section 13 of spec) — table exists,
                              but nothing writes to it yet in Phase A

NOT included in Phase A (deliberately — see Section 26/27 of spec):
  - No WealthDocument table. Section 12 only asks that the
    architecture not PREVENT future document attachment — since
    WealthAsset/WealthLiability already have a plain integer `id`,
    a future WealthDocument table can FK to either without any
    redesign needed here. Creating an unused document table now
    would be exactly the "unnecessary file" Section 19 of the
    Retirement Centre spec warned against, and the same discipline
    applies here.
  - No separate "Family & Inherited Wealth" table. Section 11 is
    explicit that this should be a VIEW/classification of existing
    WealthAsset records (via source_type, original_owner,
    original_owner_relationship, date_received, ownership_type),
    not a duplicate copy of the same data.
  - No CRUD routes, forms, or validators wired to any route yet —
    validators.py exists with ready functions for Phase B to call,
    but Phase A itself has zero Add/Edit/Delete routes.
"""

from models import db
from datetime import datetime


# ── Ownership Architecture (Section 8 of Phase A / Section 7 of Phase B) ─────
# Revised in Phase B: Phase A's version conflated ownership structure
# with source-of-wealth. Cleanly separated now — "how is this owned"
# (this class) is distinct from "how did I come to own it" (SourceType
# below). Inherited/Gifted live in SourceType, not here.

class OwnershipType:
    SOLE   = "Sole"
    JOINT  = "Joint"
    FAMILY = "Family"
    OTHER  = "Other"

    ALL = [SOLE, JOINT, FAMILY, OTHER]


# ── Source of Wealth (Section 9) ──────────────────────────────────────────────
# Drives the future "Family & Inherited Wealth" classification view
# (Section 11) — no separate table needed, this field is the hook.

class SourceType:
    SELF_ACQUIRED = "Self Acquired"
    INHERITED     = "Inherited"
    GIFTED        = "Gifted / Transferred"
    FAMILY_OWNED  = "Family Owned"
    JOINT_FAMILY  = "Joint Family Asset"
    OTHER         = "Other"

    ALL = [SELF_ACQUIRED, INHERITED, GIFTED, FAMILY_OWNED, JOINT_FAMILY, OTHER]


# ── Status / Archive Architecture (Section 14) ────────────────────────────────
# Archive is NEVER delete. "Deleted" is a separate, explicit,
# irreversible action (a real DB row removal) — not a status value.

class WealthStatus:
    ACTIVE   = "Active"
    ARCHIVED = "Archived"

    ALL = [ACTIVE, ARCHIVED]


# ── Asset Categories (revised in Phase B) ─────────────────────────────────────
# Phase A had a flat category list mirroring the old Assets module.
# Phase B replaces it with a genuine two-level structure: a broad
# category, plus an asset_type within that category — matching the
# spec's explicit taxonomy. Single source of truth for both the
# dropdown AND the JS show/hide logic — never duplicate this mapping.

class WealthAssetCategory:
    REAL_ESTATE     = "Real Estate"
    PRECIOUS_METALS = "Precious Metals"
    VEHICLES        = "Vehicles"
    BANK_DEPOSITS   = "Bank & Deposits"
    INVESTMENTS     = "Investments"
    BUSINESS        = "Business"
    OTHER           = "Other"

    ALL = [REAL_ESTATE, PRECIOUS_METALS, VEHICLES, BANK_DEPOSITS,
           INVESTMENTS, BUSINESS, OTHER]


# Asset Type options per category — drives the "Asset Type" dropdown,
# which repopulates based on the selected category.
ASSET_TYPES_BY_CATEGORY = {
    WealthAssetCategory.REAL_ESTATE: [
        "House", "Flat / Apartment", "Plot",
        "Farm / Agricultural Land", "Commercial Property", "Other Real Estate",
    ],
    WealthAssetCategory.PRECIOUS_METALS: [
        "Gold", "Silver", "Other Precious Metals",
    ],
    WealthAssetCategory.VEHICLES: [
        "Car", "Two Wheeler", "Commercial Vehicle", "Other Vehicle",
    ],
    WealthAssetCategory.BANK_DEPOSITS: [
        "Savings Account", "Fixed Deposit", "Recurring Deposit", "Other Deposit",
    ],
    WealthAssetCategory.INVESTMENTS: [
        "Bonds", "Other Investments",
    ],
    WealthAssetCategory.BUSINESS: [
        "Business Ownership", "Partnership", "Other Business Interest",
    ],
    WealthAssetCategory.OTHER: [
        "Other",
    ],
}

# Category-specific field GROUPS — which extra fields to show for
# each category. Business and Other need no extra fields; every
# field they need already exists as a common field (Section 8 of
# spec: Business Name = common `name`, Ownership % = common field,
# Current Estimated Value = common `current_value`).
FIELD_GROUPS_BY_CATEGORY = {
    WealthAssetCategory.REAL_ESTATE:     ["real_estate"],
    WealthAssetCategory.PRECIOUS_METALS: ["precious_metals"],
    WealthAssetCategory.VEHICLES:        ["vehicles"],
    # institution_ref is a SHARED sub-group (institution + account
    # reference) used by both Bank & Deposits and Investments — kept
    # as one field pair, not duplicated across two groups, since a
    # duplicated field name in two DOM locations would submit two
    # values for the same form key and risk Flask reading the wrong
    # one depending on which group happened to be visible.
    WealthAssetCategory.BANK_DEPOSITS:   ["institution_ref", "bank_deposits"],
    WealthAssetCategory.INVESTMENTS:     ["institution_ref", "investments"],
    WealthAssetCategory.BUSINESS:        [],
    WealthAssetCategory.OTHER:           [],
}


class AreaUnit:
    SQFT  = "sq.ft."
    ACRES = "acres"
    SQM   = "sq.m."
    GUNTA = "guntha"
    ALL = [SQFT, ACRES, SQM, GUNTA]


class WeightUnit:
    GRAMS = "grams"
    KG    = "kg"
    TOLA  = "tola"
    ALL = [GRAMS, KG, TOLA]


class WealthLiabilityCategory:
    """
    Revised in Phase C — Phase A's version was a flat placeholder
    list. Phase C replaces it with the real 5-category structure,
    each with its own type list (LIABILITY_TYPES_BY_CATEGORY below),
    matching the taxonomy shape already proven for WealthAsset.
    """
    LOANS            = "Loans"
    VEHICLE_FINANCE  = "Vehicle Finance"
    CREDIT           = "Credit"
    FAMILY_INFORMAL  = "Family / Informal Debt"
    OTHER            = "Other"

    ALL = [LOANS, VEHICLE_FINANCE, CREDIT, FAMILY_INFORMAL, OTHER]


LIABILITY_TYPES_BY_CATEGORY = {
    WealthLiabilityCategory.LOANS: [
        "Home Loan / Mortgage", "Personal Loan", "Education Loan",
        "Business Loan", "Other Loan",
    ],
    WealthLiabilityCategory.VEHICLE_FINANCE: [
        "Car Loan", "Two Wheeler Loan", "Commercial Vehicle Loan", "Other Vehicle Loan",
    ],
    WealthLiabilityCategory.CREDIT: [
        "Credit Card", "Line of Credit", "Other Credit",
    ],
    WealthLiabilityCategory.FAMILY_INFORMAL: [
        "Family Loan", "Friend / Informal Loan", "Other Informal Debt",
    ],
    WealthLiabilityCategory.OTHER: [
        "Other Liability",
    ],
}

# Unlike WealthAsset, every liability category uses the exact same
# common field set (Section 20-23 of Phase C spec explicitly list
# the SAME fields — Lender, Reference, Interest Rate, Dates — for
# every category). No category-specific field groups or dynamic
# show/hide are needed for Liabilities — only the Category → Type
# dropdown repopulates, same mechanism as Assets.


# ── Models ────────────────────────────────────────────────────────────────────

class WealthAsset(db.Model):
    """
    Something the user owns. Deliberately NOT linked to the old
    Assets table in any way — separate table, separate primary key
    sequence, separate lifecycle.

    Ownership-adjusted value is NOT computed here in Phase A (that's
    explicitly deferred to the Net Worth phase per Section 20) — but
    the fields needed to compute it later (current_value,
    ownership_percentage) already exist, so no future migration will
    be needed just to add them.
    """
    __tablename__ = "wealth_asset"
    __table_args__ = (
        db.Index("ix_wealth_asset_user",   "user_id"),
        db.Index("ix_wealth_asset_status", "status"),
    )

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer,
                        db.ForeignKey("user.id", ondelete="CASCADE"),
                        nullable=False)

    # ── Core ─────────────────────────────────────────────────────
    name         = db.Column(db.String(200), nullable=False)
    category     = db.Column(db.String(50),  nullable=False)
    asset_type   = db.Column(db.String(100), nullable=True)
    description  = db.Column(db.Text, nullable=True)

    current_value = db.Column(db.Float, nullable=False, default=0)
    value_as_of   = db.Column(db.Date, nullable=True)

    # ── Ownership (Section 8, Phase A / Section 7, Phase B) ──────
    ownership_type       = db.Column(db.String(30), nullable=False,
                                     default=OwnershipType.SOLE)
    ownership_percentage = db.Column(db.Float, nullable=False, default=100.0)

    # ── Source / Family & Inherited Wealth hook (Sections 9, 11) ─
    source_type                 = db.Column(db.String(30), nullable=False,
                                            default=SourceType.SELF_ACQUIRED)
    original_owner               = db.Column(db.String(200), nullable=True)
    original_owner_relationship  = db.Column(db.String(100), nullable=True)
    date_received                = db.Column(db.Date, nullable=True)

    acquisition_date  = db.Column(db.Date, nullable=True)
    acquisition_value = db.Column(db.Float, nullable=True)

    # ── Real Estate specific ─────────────────────────────────────
    property_type    = db.Column(db.String(50),  nullable=True)
    property_address = db.Column(db.String(300), nullable=True)
    city              = db.Column(db.String(100), nullable=True)
    state             = db.Column(db.String(100), nullable=True)
    area              = db.Column(db.Float, nullable=True)
    area_unit         = db.Column(db.String(20), nullable=True)

    # ── Precious Metals specific ─────────────────────────────────
    metal_type  = db.Column(db.String(50), nullable=True)
    weight      = db.Column(db.Float, nullable=True)
    weight_unit = db.Column(db.String(20), nullable=True)

    # ── Vehicles specific ────────────────────────────────────────
    vehicle_type        = db.Column(db.String(50),  nullable=True)
    registration_number = db.Column(db.String(50),  nullable=True)

    # ── Bank & Deposits / Investments specific ───────────────────
    # institution + account_reference are shared between these two
    # categories — same concept ("which institution", "what
    # reference/account number"), no reason to duplicate the columns.
    institution       = db.Column(db.String(200), nullable=True)
    account_reference = db.Column(db.String(100), nullable=True)
    deposit_type       = db.Column(db.String(50),  nullable=True)
    interest_rate       = db.Column(db.Float, nullable=True)
    maturity_date        = db.Column(db.Date, nullable=True)
    investment_type      = db.Column(db.String(50), nullable=True)

    # ── Status / Archive (Section 14) ────────────────────────────
    status      = db.Column(db.String(20), nullable=False,
                            default=WealthStatus.ACTIVE)
    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)

    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    @property
    def attributable_value(self):
        """
        Section 18/30 of Phase B spec: the user's ownership-adjusted
        share of this asset's value. current_value is NEVER modified
        by ownership — this is a computed display value only, never
        stored, so the original asset value is always preserved.
        """
        pct = self.ownership_percentage if self.ownership_percentage is not None else 100.0
        return (self.current_value or 0) * (pct / 100.0)

    @property
    def is_family_or_inherited(self):
        """
        Used for the dashboard's 'Inherited / Family Assets' count.
        Fixed in Phase E — the audit found GIFTED was missing from
        this check entirely, even though Section 14 of the Phase E
        spec explicitly requires Gifted/Transferred assets to qualify
        for the Family & Inherited Wealth view. This was a real
        pre-existing bug, not a Phase E design choice.
        """
        return self.source_type in (
            SourceType.INHERITED, SourceType.GIFTED,
            SourceType.FAMILY_OWNED, SourceType.JOINT_FAMILY
        )

    def __repr__(self):
        return f"<WealthAsset id={self.id} {self.name}>"


class WealthLiability(db.Model):
    """Something the user owes. Independent table, same philosophy
    as WealthAsset — no EMI/amortization logic (Section 36/37 of
    Phase C spec)."""
    __tablename__ = "wealth_liability"
    __table_args__ = (
        db.Index("ix_wealth_liability_user",   "user_id"),
        db.Index("ix_wealth_liability_status", "status"),
    )

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer,
                        db.ForeignKey("user.id", ondelete="CASCADE"),
                        nullable=False)

    name            = db.Column(db.String(200), nullable=False)
    category        = db.Column(db.String(50),  nullable=False)
    liability_type  = db.Column(db.String(100), nullable=True)
    description     = db.Column(db.Text, nullable=True)
    lender          = db.Column(db.String(200), nullable=True)
    account_reference = db.Column(db.String(100), nullable=True)
    # ^ Section 15: sensitive — never displayed in listing rows or
    #   search results, only on the detail page (enforced in templates)

    original_amount    = db.Column(db.Float, nullable=False, default=0)
    outstanding_amount = db.Column(db.Float, nullable=False, default=0)
    interest_rate       = db.Column(db.Float, nullable=True)
    # ^ informational only — never drives any calculation (Section 37)

    ownership_type       = db.Column(db.String(30), nullable=False,
                                     default=OwnershipType.SOLE)
    ownership_percentage = db.Column(db.Float, nullable=False, default=100.0)

    status      = db.Column(db.String(20), nullable=False,
                            default=WealthStatus.ACTIVE)
    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)

    start_date         = db.Column(db.Date, nullable=True)
    expected_end_date  = db.Column(db.Date, nullable=True)
    # ^ labeled "Maturity / End Date" in the UI — kept this column
    #   name from Phase A rather than renaming, avoiding an
    #   unnecessary column-rename migration for a cosmetic label change

    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    @property
    def attributable_liability(self):
        """
        Section 17/18 of spec: the user's ownership-adjusted share of
        this liability. outstanding_amount is NEVER modified by
        ownership — this is a computed display value only, exactly
        mirroring WealthAsset.attributable_value's design.
        """
        pct = self.ownership_percentage if self.ownership_percentage is not None else 100.0
        return (self.outstanding_amount or 0) * (pct / 100.0)

    def __repr__(self):
        return f"<WealthLiability id={self.id} {self.name}>"


class WealthValueSnapshot(db.Model):
    """
    Foundation for future historical values (Section 13). Deliberately
    minimal — a polymorphic-lite design (entity_type + entity_id)
    rather than two separate nullable FK columns, so ONE snapshot
    table can serve both assets and liabilities without a schema
    change later. Nothing writes to this table in Phase A; it exists
    purely so the History phase doesn't require redesigning
    WealthAsset/WealthLiability to add snapshot support.
    """
    __tablename__ = "wealth_value_snapshot"
    __table_args__ = (
        db.Index("ix_wealth_snapshot_entity", "entity_type", "entity_id"),
    )

    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer,
                            db.ForeignKey("user.id", ondelete="CASCADE"),
                            nullable=False)
    entity_type = db.Column(db.String(20), nullable=False)
    # "asset" or "liability"
    entity_id   = db.Column(db.Integer, nullable=False)
    # references WealthAsset.id or WealthLiability.id depending on
    # entity_type — not a formal FK, since it can point to either
    # table; the History phase's service layer is responsible for
    # validating entity_id against the right table.

    value          = db.Column(db.Float, nullable=False)
    snapshot_date  = db.Column(db.Date, nullable=False)
    note           = db.Column(db.String(300), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<WealthValueSnapshot {self.entity_type}={self.entity_id} value={self.value}>"
