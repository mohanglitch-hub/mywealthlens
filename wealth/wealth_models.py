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

    balance_as_of = db.Column(db.Date, nullable=True)
    # ^ Phase L. WealthAsset already had this exact field (as
    #   value_as_of) since Phase A, just dormant until now —
    #   WealthLiability never had an equivalent, so this is a new
    #   column. Same role: the effective date for the current
    #   outstanding_amount figure, and the default effective_date
    #   passed to record_wealth_value_change() when this changes.

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
    Item-level valuation history for WealthAsset/WealthLiability.
    Activated in Phase J (this table existed since Phase A but was
    dormant until then); given explicit temporal semantics in Phase L.

    Phase L (Section 2/3): every row distinguishes two dates —
      effective_date — the date the VALUE financially applies to
                       (user-controlled, may be backdated, never future)
      created_at     — when MyWealthLens actually received/recorded
                       this row (application-controlled, immutable,
                       never user-editable)

    Migration note: this column was named `snapshot_date` prior to
    Phase L. For every row created under Phase J, that value was
    always `datetime.utcnow().date()` at the moment of recording —
    i.e. it always equaled the same date `created_at` would give you.
    Renaming the column (Section 71: "effective_date = existing
    recorded timestamp/date is the safest default") is therefore a
    pure metadata change with zero data loss and zero invented
    values — every pre-Phase-L row's effective_date is exactly what
    it was already, just now correctly named for what it represents.
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
    effective_date = db.Column(db.Date, nullable=False)
    # ^ Phase L: the date this VALUE financially applies to. Never a
    #   future date (Section 28/29). May be backdated relative to
    #   created_at (that's the entire point of this phase). Was
    #   named `snapshot_date` prior to Phase L — see class docstring.
    note           = db.Column(db.String(300), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # ^ Phase L's "recorded_at" (Section 11) — kept the existing
    #   column name rather than adding a duplicate field, since this
    #   already fulfilled that exact role since Phase J: application-
    #   generated, immutable after creation, never user-editable.

    def __repr__(self):
        return f"<WealthValueSnapshot {self.entity_type}={self.entity_id} value={self.value}>"


class SnapshotSource:
    """
    Phase I — distinguishes how a snapshot was created. Added because
    the History UI benefits from showing this (Section 34/79 of the
    Phase I spec), not because the snapshot's financial meaning
    changes at all — a manual and an automatic snapshot for the same
    date represent identically-structured Wealth data.
    """
    MANUAL    = "manual"
    AUTOMATIC = "automatic"

    ALL = [MANUAL, AUTOMATIC]


class WealthSnapshot(db.Model):
    """
    Phase F — Wealth History. An aggregate, point-in-time record of
    the user's whole Wealth position (Total/Attributable Assets,
    Total/Attributable Liabilities, Net Worth).

    NOT the same thing as WealthValueSnapshot above — that table is a
    Phase A foundation for future PER-ENTITY (single asset/liability)
    value history, which Section 36/37 of the Phase F spec explicitly
    puts out of scope. This table is deliberately separate: one row
    = one Wealth-wide position on one date, immutable once created
    (except for an explicit, confirmed replace — Section 6/11).

    Net Worth stored here = My Attributable Assets − My Attributable
    Liabilities, matching the Phase F spec's own worked example in
    Section 13 (₹1,00,00,000 attributable assets − ₹10,00,000
    attributable liabilities = ₹90,00,000 net worth, NOT the
    ₹1,30,00,000 that gross totals would give). This mirrors
    WealthStatisticsService.attributable_net_worth() exactly — no
    competing formula (Section 15/74).

    Phase N (Sections 7/8): also NOT the same thing as the root
    models.py NetWorthHistory table, despite both tracking "net
    worth over time" — investigated with evidence and deliberately
    kept as two separate systems. NetWorthHistory's scope includes
    MutualFund/Stock (CAS-imported holdings, outside the Wealth
    Centre entirely) and powers the main app dashboard's own trend
    chart; this table's scope is strictly Assets + Liabilities and
    powers the Wealth Centre's own History page. See the full
    rationale on NetWorthHistory's own class docstring in root
    models.py.
    """
    __tablename__ = "wealth_snapshot"
    __table_args__ = (
        db.UniqueConstraint("user_id", "snapshot_date",
                            name="uq_wealth_snapshot_user_date"),
        db.Index("ix_wealth_snapshot_user_date", "user_id", "snapshot_date"),
    )

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer,
                        db.ForeignKey("user.id", ondelete="CASCADE"),
                        nullable=False)

    snapshot_date = db.Column(db.Date, nullable=False)
    # ^ the financial date this snapshot represents — distinct from
    #   created_at below (Section 7: a snapshot may be created after
    #   the period it represents, e.g. dated 31 Mar but created 1 Apr)

    total_asset_value            = db.Column(db.Float, nullable=False, default=0)
    attributable_asset_value     = db.Column(db.Float, nullable=False, default=0)
    total_liability_value        = db.Column(db.Float, nullable=False, default=0)
    attributable_liability_value = db.Column(db.Float, nullable=False, default=0)
    net_worth                    = db.Column(db.Float, nullable=False, default=0)

    status = db.Column(db.String(20), nullable=False, default="Active")
    # ^ Section 29: no archive lifecycle for snapshots, just a simple
    #   Active/Deleted — but rows are hard-deleted on delete (Section
    #   28), so in practice this column is always "Active". Kept as
    #   an explicit column (rather than relying on row-existence
    #   alone) purely so a future phase could soft-delete without a
    #   schema change — never read as ARCHIVED anywhere in Phase F.

    source = db.Column(db.String(20), nullable=False,
                       default=SnapshotSource.MANUAL,
                       server_default=SnapshotSource.MANUAL)
    # ^ Phase I. server_default ensures the migration backfills every
    #   pre-existing row as "manual" (Section 54/55 — historical
    #   snapshots were all created through the manual route; it would
    #   be factually wrong to relabel them "automatic" after the
    #   fact). New rows explicitly pass source= at creation time
    #   (Section 79 — no arbitrary strings; only SnapshotSource.ALL
    #   values are ever written).


class WealthSnapshotLog(db.Model):
    """
    Phase I — operational log of automatic snapshot CLI runs
    (Section 28/29). Deliberately separate from WealthSnapshot: this
    table records what the SCHEDULER did (including SKIPPED/FAILED
    outcomes that never produce a WealthSnapshot row at all), not
    Wealth financial data. Never contains asset values, account
    details, or any other private figure (Section 30) — status and a
    short operational message only.

    One row per user per CLI run attempt (not one row per CLI
    invocation) so a single run touching 10 users produces 10 log
    rows, each independently showing that user's outcome — this is
    what lets one user's FAILED not obscure another user's SUCCESS
    (Section 25/58) in the log table itself, not just in memory
    during the run.
    """
    __tablename__ = "wealth_snapshot_log"

    STATUS_SUCCESS = "SUCCESS"
    STATUS_SKIPPED = "SKIPPED"
    STATUS_FAILED  = "FAILED"
    ALL_STATUSES = [STATUS_SUCCESS, STATUS_SKIPPED, STATUS_FAILED]

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer,
                        db.ForeignKey("user.id", ondelete="CASCADE"),
                        nullable=False)

    snapshot_date = db.Column(db.Date, nullable=False)
    # ^ the IST calendar date this run attempt was FOR — always
    #   "today" at the moment the CLI executed (Section 43), never
    #   backfilled or derived from a missed schedule's original time.

    run_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    # ^ wall-clock time the log row was written, distinct from
    #   snapshot_date (Section 12) — this can differ from
    #   snapshot_date's "midnight IST" framing since a run can happen
    #   at any time of day (manual test runs, retries, etc).

    status  = db.Column(db.String(10), nullable=False)
    message = db.Column(db.String(300), nullable=True)
    # ^ short operational text only, e.g. "snapshot created" /
    #   "snapshot already exists" / "calculation error" — never a
    #   financial figure or exception's raw args (Section 30).

    snapshot_id = db.Column(db.Integer,
                            db.ForeignKey("wealth_snapshot.id", ondelete="SET NULL"),
                            nullable=True)
    # ^ set only on SUCCESS, so a log row can link to the snapshot it
    #   produced; SET NULL rather than CASCADE so deleting a snapshot
    #   later (Section 29 of Phase F) never deletes its own log
    #   history — the log is a record that the run happened, which
    #   remains true regardless of what happens to the snapshot after.

    def __repr__(self):
        return f"<WealthSnapshotLog user={self.user_id} {self.snapshot_date} {self.status}>"
class WealthDocumentCategory:
    """
    Top-level document categories (Section 10 of Phase G spec).
    Mirrors the WealthAssetCategory / ASSET_TYPES_BY_CATEGORY pattern
    already established for Assets — a `category` column plus a
    `document_type` column, rather than a single flat type string
    (which is what Insurance Centre's simpler DocumentType uses).
    This is a deliberate departure from Insurance's flatter model:
    Wealth documents span a much wider category range (Property,
    Loans, Retirement, Gold, Tax, ...) where two-level classification
    genuinely helps filtering, and the Wealth module already has a
    working two-level convention to reuse.
    """
    PROPERTY       = "Property"
    BANKING        = "Banking & Deposits"
    INVESTMENTS    = "Investments"
    LOANS          = "Loans"
    RETIREMENT     = "Retirement"
    GOLD           = "Gold & Precious Assets"
    VEHICLE        = "Vehicle"
    FAMILY_ESTATE  = "Family & Estate"
    TAX            = "Tax"
    OTHER          = "Other"

    ALL = [PROPERTY, BANKING, INVESTMENTS, LOANS, RETIREMENT,
           GOLD, VEHICLE, FAMILY_ESTATE, TAX, OTHER]


DOCUMENT_TYPES_BY_CATEGORY = {
    WealthDocumentCategory.PROPERTY: [
        "Sale Deed", "Purchase Agreement", "Registration Document",
        "Property Tax Document", "Property Valuation", "Other Property Document",
    ],
    WealthDocumentCategory.BANKING: [
        "Bank Statement", "Fixed Deposit", "Recurring Deposit",
        "Account Document", "Other Banking Document",
    ],
    WealthDocumentCategory.INVESTMENTS: [
        "Mutual Fund Statement", "Demat Statement", "Stock Statement",
        "Bond Document", "Investment Statement", "Other Investment Document",
    ],
    WealthDocumentCategory.LOANS: [
        "Loan Agreement", "Sanction Letter", "Loan Statement",
        "Repayment Schedule", "Closure Certificate", "Other Loan Document",
    ],
    WealthDocumentCategory.RETIREMENT: [
        "PPF", "EPF", "NPS", "Pension Document", "Other Retirement Document",
    ],
    WealthDocumentCategory.GOLD: [
        "Purchase Invoice", "Valuation Certificate", "Ownership Document",
        "Other Gold Document",
    ],
    WealthDocumentCategory.VEHICLE: [
        "Registration Document", "Purchase Invoice", "Loan Document",
        "Other Vehicle Document",
    ],
    WealthDocumentCategory.FAMILY_ESTATE: [
        "Will", "Inheritance Document", "Nomination Document",
        "Succession Document", "Family Asset Document", "Other Estate Document",
    ],
    WealthDocumentCategory.TAX: [
        "Income Tax Document", "Capital Gains Document", "Tax Statement",
        "Other Tax Document",
    ],
    WealthDocumentCategory.OTHER: [
        "Other Wealth Document",
    ],
}

CATEGORY_OF_DOCUMENT_TYPE = {
    dtype: cat
    for cat, types in DOCUMENT_TYPES_BY_CATEGORY.items()
    for dtype in types
}


class WealthDocument(db.Model):
    """
    Phase G — Wealth Document Vault. Metadata only; the physical file
    lives on disk (Section 65: database stores metadata, filesystem
    stores content — mirrors Insurance/Retirement's InsuranceDocument
    / RetirementDocument exactly).

    A document may OPTIONALLY relate to one Asset and/or one
    Liability (Section 7) — both are nullable, and a document is
    equally valid standalone (e.g. a Will with no Asset/Liability at
    all, Section 81). SET NULL on delete of the related Asset/
    Liability, mirroring InsuranceDocument's policy_id relationship —
    the document record survives even if its related Asset is later
    deleted, for the user's own record-keeping.

    Storage path convention: instance/documents/wealth/<user_id>/
    (Section 16) — keyed by user_id, NOT by asset_id/liability_id,
    specifically because standalone documents have no such id to key
    on. This is the one deliberate difference from Insurance/
    Retirement's per-policy/per-scheme folder convention.
    """
    __tablename__ = "wealth_document"
    __table_args__ = (
        db.Index("ix_wealth_doc_user",       "user_id"),
        db.Index("ix_wealth_doc_type",       "document_type"),
        db.Index("ix_wealth_doc_asset",      "asset_id"),
        db.Index("ix_wealth_doc_liability",  "liability_id"),
        db.Index("ix_wealth_doc_uploaded",   "uploaded_at"),
    )

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer,
                        db.ForeignKey("user.id", ondelete="CASCADE"),
                        nullable=False)

    asset_id = db.Column(db.Integer,
                         db.ForeignKey("wealth_asset.id", ondelete="SET NULL"),
                         nullable=True)
    liability_id = db.Column(db.Integer,
                             db.ForeignKey("wealth_liability.id", ondelete="SET NULL"),
                             nullable=True)

    category      = db.Column(db.String(50),  nullable=False)
    document_type = db.Column(db.String(50),  nullable=False)

    title       = db.Column(db.String(255), nullable=True)
    description = db.Column(db.String(500), nullable=True)

    original_name = db.Column(db.String(255), nullable=False)
    stored_name   = db.Column(db.String(255), nullable=False)
    file_path     = db.Column(db.String(500), nullable=False)
    file_extension = db.Column(db.String(10), nullable=True)
    mime_type      = db.Column(db.String(100), nullable=True)
    file_size      = db.Column(db.Integer, nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def display_name(self):
        return self.title or self.original_name

    @property
    def file_size_display(self):
        if not self.file_size:
            return "Unknown"
        if self.file_size < 1024:
            return f"{self.file_size} B"
        if self.file_size < 1024 * 1024:
            return f"{self.file_size / 1024:.1f} KB"
        return f"{self.file_size / (1024*1024):.1f} MB"

    def __repr__(self):
        return f"<WealthDocument {self.document_type} user={self.user_id} name={self.original_name}>"