"""
Retirement Centre — Models
=============================
Phase A: Foundation for the Retirement Centre database architecture.

Tables:
  1. RetirementScheme            — core scheme record (all scheme types)
  2. RetirementContribution      — actual deposit history (one row per deposit)
  3. RetirementBalanceSnapshot   — historical "balance was reported as X on date Y"
  4. RetirementTimeline          — append-only audit history per scheme

Design principles (mirroring insurance_centre/models.py):
  - Single scheme table — no separate table per scheme type
  - Category-specific fields are nullable columns, populated only
    for the scheme types where they apply (Section 8 of spec)
  - Soft delete via is_archived flag, same philosophy as Insurance Centre
  - Contribution history is REAL rows, never inferred from a single
    "annual contribution" field (Section 9)
  - Balance snapshots are a historical log only — the system never
    infers "return" from the difference between two snapshots (Section 11)

NOT included in Phase A (deliberately — see Section 21 of spec):
  - RetirementNominee table
  - RetirementDocument table
  These will be added in the phase where their UI is actually built,
  following whatever integration point makes sense with Insurance
  Centre's nominee/document architecture at that time.
"""

from models import db
from datetime import datetime


# ── Scheme Type Configuration ────────────────────────────────────────────────
# Single source of truth — every template/dropdown must use SchemeType.ALL,
# never a hard-coded list (Section 7 of spec).

class SchemeType:
    EPF             = "EPF"
    VPF             = "VPF"
    PPF             = "PPF"
    NPS             = "NPS"
    SSY             = "SSY"
    SUPERANNUATION  = "Superannuation"
    OTHER_EMPLOYER  = "Other Employer Retirement Scheme"
    OTHER_GOVT      = "Other Government Scheme"
    OTHER_PENSION   = "Other Pension / Retirement Investment"
    CUSTOM          = "Other (Custom)"

    # Alphabetical, with "Other (Custom)" always forced last, per Section 7.
    _ALPHABETICAL = sorted([
        EPF, VPF, PPF, NPS, SSY, SUPERANNUATION,
        OTHER_EMPLOYER, OTHER_GOVT, OTHER_PENSION,
    ])
    ALL = _ALPHABETICAL + [CUSTOM]

    # Simplified 5-option display list for the Add/Edit Scheme dropdown
    # (label, stored_value) — the underlying model still supports the
    # full ALL list above for backend safety/history, this is purely
    # what the form offers going forward. EPF/VPF share identical
    # FIELD_GROUPS already, so merging them loses no functional
    # behavior — just the EPF-vs-VPF display label distinction.
    # "Other Retirement Schemes" reuses the existing CUSTOM type and
    # its Custom Scheme Name field, rather than a new mechanism.
    DISPLAY_OPTIONS = [
        ("EPF / VPF", EPF),
        ("PPF", PPF),
        ("NPS", NPS),
        ("SSY", SSY),
        ("Other Retirement Schemes", CUSTOM),
    ]

    # Centralized map of which category-specific field GROUPS apply to
    # each scheme type. Single source of truth for both the form's
    # JS show/hide logic and backend validation — never duplicate this
    # mapping anywhere else (Section 29 of Phase B spec).
    #
    # Group names correspond to blocks in the shared scheme form:
    #   employer          -> employer_name
    #   uan_salary        -> uan_number, basic_salary  (EPF/VPF only)
    #   contribution_pct  -> employee_contribution_pct, employer_contribution_pct
    #   target_year       -> target_retirement_year
    #   ppf               -> extension_opted
    #   ssy               -> girl_child_name, girl_child_dob
    #   nps               -> pran_number, tier
    #   custom            -> custom_type (used as "Custom Scheme Name")
    FIELD_GROUPS = {
        EPF:            ["employer", "uan_salary", "contribution_pct", "target_year"],
        VPF:            ["employer", "uan_salary", "contribution_pct", "target_year"],
        PPF:            ["ppf"],
        NPS:            ["nps", "contribution_pct", "target_year"],
        SSY:            ["ssy"],
        SUPERANNUATION: ["employer", "target_year"],
        OTHER_EMPLOYER: [],
        OTHER_GOVT:     [],
        OTHER_PENSION:  [],
        CUSTOM:         ["custom"],
    }


# Phase F, Section 1 — the five DISPLAY categories shown on the
# dashboard, distinct from the more granular SchemeType values above.
# EPF and VPF are shown as one combined category; every other "Other
# ..." scheme type folds into a single catch-all category. This is
# purely a presentation grouping — the underlying scheme_type on each
# RetirementScheme row is unaffected. Single source of truth: never
# duplicate this mapping in a template or route.
RETIREMENT_CATEGORY_GROUPS = {
    "EPF / VPF": [SchemeType.EPF, SchemeType.VPF],
    "PPF":       [SchemeType.PPF],
    "NPS":       [SchemeType.NPS],
    "SSY":       [SchemeType.SSY],
    "Other Retirement Schemes": [
        SchemeType.SUPERANNUATION, SchemeType.OTHER_EMPLOYER,
        SchemeType.OTHER_GOVT, SchemeType.OTHER_PENSION, SchemeType.CUSTOM,
    ],
}
RETIREMENT_CATEGORY_ORDER = list(RETIREMENT_CATEGORY_GROUPS.keys())

# Slugs for category-specific pages (/retirement/category/<slug>),
# mirroring insurance_centre's category_to_slug/slug_to_category
# pattern for consistency across the app.
RETIREMENT_CATEGORY_SLUGS = {
    "epf-vpf": "EPF / VPF",
    "ppf": "PPF",
    "nps": "NPS",
    "ssy": "SSY",
    "other": "Other Retirement Schemes",
}
RETIREMENT_SLUG_FROM_CATEGORY = {v: k for k, v in RETIREMENT_CATEGORY_SLUGS.items()}


def category_to_slug(category):
    return RETIREMENT_SLUG_FROM_CATEGORY.get(
        category, category.lower().replace(" ", "-").replace("/", ""))


def slug_to_category(slug):
    return RETIREMENT_CATEGORY_SLUGS.get(slug, slug)


def scheme_type_to_category(scheme_type):
    """
    Reverse lookup: given a stored scheme_type value, return which of
    the 5 display categories it belongs to. Used so Add/Edit/View
    scheme pages can always know their category context without
    needing a fragile query-string round-trip for View/Edit (a
    scheme's type is always known server-side).
    """
    for category, types in RETIREMENT_CATEGORY_GROUPS.items():
        if scheme_type in types:
            return category
    return None


def category_for_scheme_type(scheme_type):
    """Reverse lookup: given a scheme_type value, return which of the
    5 display categories it belongs to. Used to build 'back to
    category' navigation on the scheme detail page."""
    for cat, types in RETIREMENT_CATEGORY_GROUPS.items():
        if scheme_type in types:
            return cat
    return None


# Full/expanded names for category page headers — abbreviations
# spelled out, matching Insurance Centre's "Life Insurance" (not
# "LI") convention on its own category pages.
RETIREMENT_CATEGORY_FULL_NAMES = {
    "EPF / VPF": "Employees' Provident Fund / Voluntary Provident Fund",
    "PPF": "Public Provident Fund (PPF)",
    "NPS": "National Pension System (NPS)",
    "SSY": "Sukanya Samriddhi Yojana (SSY)",
    "Other Retirement Schemes": "Other Retirement Schemes",
}


class GrowthMethod:
    """
    Distinguishes HOW a scheme grows, since not every scheme earns
    a simple fixed interest rate (Section 6 of spec).
    """
    GOVERNMENT_DECLARED = "government_declared"
    MARKET_LINKED        = "market_linked"
    CUSTOM                = "custom"
    OTHER                 = "other"

    ALL = [GOVERNMENT_DECLARED, MARKET_LINKED, CUSTOM, OTHER]

    LABELS = {
        GOVERNMENT_DECLARED: "Government-Declared Rate",
        MARKET_LINKED:        "Market-Linked Return",
        CUSTOM:                "Custom Assumption",
        OTHER:                  "Other",
    }


class SchemeStatus:
    ACTIVE      = "Active"
    INACTIVE    = "Inactive"
    MATURED     = "Matured"
    CLOSED      = "Closed"
    TRANSFERRED = "Transferred"
    ARCHIVED    = "Archived"

    ALL = [ACTIVE, INACTIVE, MATURED, CLOSED, TRANSFERRED, ARCHIVED]


class ContributionPreference:
    """
    A user's PLANNED contribution pattern — a stated intention, not a
    record of actual deposits. Actual contributions are tracked via
    RetirementContribution (Phase C). Never use this field to compute
    real contribution totals (Section 15 of the Phase B spec).
    """
    FLEXIBLE     = "Flexible"
    MONTHLY      = "Monthly"
    QUARTERLY    = "Quarterly"
    HALF_YEARLY  = "Half-Yearly"
    YEARLY       = "Yearly"
    ONE_TIME     = "One-Time"

    ALL = [FLEXIBLE, MONTHLY, QUARTERLY, HALF_YEARLY, YEARLY, ONE_TIME]


class NPSTier:
    TIER_I  = "Tier I"
    TIER_II = "Tier II"

    ALL = [TIER_I, TIER_II]


class NomineeRelation:
    """Centralized relationship list — single source of truth, per
    Section 23 of the Phase C spec."""
    ALL = ["Spouse", "Father", "Mother", "Son", "Daughter",
           "Brother", "Sister", "Grandparent", "Grandchild", "Other"]


class RetirementDocumentType:
    """
    Centralized document type list — 'Other' forced last, same
    discipline as SchemeType. Replaced entirely per the Document
    Vault phase (superseding the original Phase D list).
    """
    PASSBOOK              = "Passbook"
    ACCOUNT_STATEMENT     = "Account Statement"
    CONTRIBUTION_STATEMENT = "Contribution Statement"
    ANNUAL_STATEMENT      = "Annual Statement"
    NOMINEE_DOCUMENT      = "Nominee Document"
    MATURITY_DOCUMENT     = "Maturity Document"
    INTEREST_CERTIFICATE  = "Interest Certificate"
    OTHER                 = "Other"

    ALL = [PASSBOOK, ACCOUNT_STATEMENT, CONTRIBUTION_STATEMENT,
           ANNUAL_STATEMENT, NOMINEE_DOCUMENT, MATURITY_DOCUMENT,
           INTEREST_CERTIFICATE, OTHER]


class RetirementTimelineEvent:
    CREATED            = "Scheme Created"
    UPDATED            = "Scheme Updated"
    CONTRIBUTION_ADDED = "Contribution Added"
    BALANCE_UPDATED    = "Balance Updated"
    DOCUMENT_UPLOADED  = "Document Uploaded"
    ARCHIVED           = "Archived"
    RESTORED           = "Restored"


# ── Models ────────────────────────────────────────────────────────────────────

class RetirementScheme(db.Model):
    """
    Core retirement/pension scheme record — single table for all
    scheme types (EPF, VPF, PPF, NPS, SSY, and beyond). Category-
    specific fields are nullable and only populated for the scheme
    types where they're relevant.
    """
    __tablename__ = "retirement_scheme"
    __table_args__ = (
        db.Index("ix_ret_scheme_user",   "user_id"),
        db.Index("ix_ret_scheme_type",   "scheme_type"),
        db.Index("ix_ret_scheme_status", "status"),
    )

    # ── Primary ──────────────────────────────────────────────────
    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer,
                        db.ForeignKey("user.id", ondelete="CASCADE"),
                        nullable=False)

    # ── Common Fields ────────────────────────────────────────────
    scheme_type = db.Column(db.String(60), nullable=False)
    custom_type = db.Column(db.String(200), nullable=True)
    # ^ used only when scheme_type == SchemeType.CUSTOM

    institution    = db.Column(db.String(200), nullable=True)
    account_number = db.Column(db.String(100), nullable=True)
    opening_date   = db.Column(db.Date, nullable=True)

    current_balance    = db.Column(db.Float, nullable=False, default=0)
    balance_updated_at = db.Column(db.Date, nullable=True)

    growth_method = db.Column(db.String(30), nullable=False,
                              default=GrowthMethod.GOVERNMENT_DECLARED)
    rate_or_return_assumption = db.Column(db.Float, nullable=True)
    # ^ the numeric %, its meaning depends on growth_method above

    status = db.Column(db.String(20), nullable=False,
                       default=SchemeStatus.ACTIVE)
    notes  = db.Column(db.Text, nullable=True)

    # ── Soft Delete / Archive (same philosophy as Insurance Centre) ─
    is_archived = db.Column(db.Boolean, default=False, nullable=False)
    archived_at = db.Column(db.DateTime, nullable=True)

    # ── EPF / VPF / Superannuation specific ───────────────────────
    employer_name             = db.Column(db.String(200), nullable=True)
    uan_number                = db.Column(db.String(30),  nullable=True)
    # ^ Universal Account Number — EPF/VPF only
    basic_salary              = db.Column(db.Float, nullable=True)
    # ^ Basic/eligible salary used for contribution calc — EPF/VPF only, optional
    employee_contribution_pct = db.Column(db.Float, nullable=True)
    employer_contribution_pct = db.Column(db.Float, nullable=True)
    # ^ shared with NPS below — same meaning, reused rather than duplicated
    target_retirement_year    = db.Column(db.Integer, nullable=True)
    # ^ also used by NPS/Superannuation below — shared field, same meaning

    # ── PPF specific ─────────────────────────────────────────────
    extension_opted = db.Column(db.Boolean, nullable=True)

    # ── SSY specific ─────────────────────────────────────────────
    girl_child_name = db.Column(db.String(200), nullable=True)
    girl_child_dob  = db.Column(db.Date, nullable=True)

    # ── NPS specific ─────────────────────────────────────────────
    pran_number = db.Column(db.String(50), nullable=True)
    tier        = db.Column(db.String(10), nullable=True)
    # employee/employer_contribution_pct and target_retirement_year shared above

    # ── User's planned contribution pattern (Phase B) ─────────────
    # A stated intention, NOT actual contribution history.
    contribution_preference = db.Column(db.String(20), nullable=True)

    # ── Timestamps ───────────────────────────────────────────────
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    # ── Relationships ────────────────────────────────────────────
    contributions = db.relationship("RetirementContribution",
                                    backref="scheme",
                                    lazy="dynamic",
                                    cascade="all, delete-orphan")
    balance_snapshots = db.relationship("RetirementBalanceSnapshot",
                                        backref="scheme",
                                        lazy="dynamic",
                                        cascade="all, delete-orphan")
    nominees = db.relationship("RetirementSchemeNominee",
                               backref="scheme",
                               lazy="dynamic",
                               cascade="all, delete-orphan")
    documents = db.relationship("RetirementDocument",
                                backref="scheme",
                                lazy="dynamic",
                                cascade="all, delete-orphan")
    timeline = db.relationship("RetirementTimeline",
                               backref="scheme",
                               lazy="dynamic",
                               cascade="all, delete-orphan",
                               order_by="RetirementTimeline.created_at.desc()")

    # ── Computed Properties ──────────────────────────────────────
    @property
    def display_type(self):
        if self.scheme_type == SchemeType.CUSTOM and self.custom_type:
            return self.custom_type
        return self.scheme_type

    def __repr__(self):
        return f"<RetirementScheme id={self.id} {self.scheme_type}>"


class ContributionEntryType:
    """
    Distinguishes actual deposits from interest credits within
    Contribution History. Both add to current_balance the same way,
    but only DEPOSIT counts toward 'contribution' totals — interest
    is growth, not money the user put in, and conflating the two
    would misrepresent how much was actually contributed (a rule
    this app has held since Phase C).
    """
    DEPOSIT  = "Deposit"
    INTEREST = "Interest Credited"

    ALL = [DEPOSIT, INTEREST]


class RetirementContribution(db.Model):
    """
    Actual contribution/deposit history. One row per deposit —
    NEVER inferred from a single annual-contribution field, since
    real contribution patterns are irregular (Section 9 of spec).
    """
    __tablename__ = "retirement_contribution"
    __table_args__ = (
        db.Index("ix_ret_contrib_scheme", "scheme_id"),
        db.Index("ix_ret_contrib_date",   "contribution_date"),
    )

    id        = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer,
                          db.ForeignKey("retirement_scheme.id",
                                       ondelete="CASCADE"),
                          nullable=False)
    user_id   = db.Column(db.Integer,
                          db.ForeignKey("user.id"),
                          nullable=False)

    contribution_date = db.Column(db.Date, nullable=False)
    amount             = db.Column(db.Float, nullable=False)
    entry_type         = db.Column(db.String(20), nullable=False,
                                   default=ContributionEntryType.DEPOSIT)
    note               = db.Column(db.String(300), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<RetirementContribution scheme={self.scheme_id} amount={self.amount}>"


class RetirementBalanceSnapshot(db.Model):
    """
    Historical record of reported balances: "on this date, the user
    reported this scheme's balance was ₹X." Purely a log — the system
    never auto-calculates "return" from the gap between two snapshots,
    since contributions, interest, and withdrawals could all explain
    the difference (Section 11 of spec).
    """
    __tablename__ = "retirement_balance_snapshot"
    __table_args__ = (
        db.Index("ix_ret_snapshot_scheme", "scheme_id"),
    )

    id        = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer,
                          db.ForeignKey("retirement_scheme.id",
                                       ondelete="CASCADE"),
                          nullable=False)

    balance      = db.Column(db.Float, nullable=False)
    balance_date = db.Column(db.Date, nullable=False)
    note         = db.Column(db.String(300), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<RetirementBalanceSnapshot scheme={self.scheme_id} balance={self.balance} on={self.balance_date}>"


class RetirementTimeline(db.Model):
    """
    Append-only audit history per scheme — mirrors InsuranceTimeline's
    philosophy exactly, but kept module-specific rather than shared,
    since no generic activity framework exists in the codebase yet
    (Section 16 of spec).
    """
    __tablename__ = "retirement_timeline"
    __table_args__ = (
        db.Index("ix_ret_timeline_scheme", "scheme_id"),
    )

    id        = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer,
                          db.ForeignKey("retirement_scheme.id",
                                       ondelete="CASCADE"),
                          nullable=False)
    user_id   = db.Column(db.Integer,
                          db.ForeignKey("user.id"),
                          nullable=False)

    event_type  = db.Column(db.String(50),  nullable=False)
    description = db.Column(db.String(500), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<RetirementTimeline {self.event_type} scheme={self.scheme_id}>"


class RetirementSchemeNominee(db.Model):
    """
    Nominee for a retirement scheme (Phase C, Part 4).
    Kept as a clean, retirement-specific structure rather than
    reusing Insurance Centre's InsuranceNominee — different modules,
    different lifecycle, no genuine cross-module dependency to share.
    """
    __tablename__ = "retirement_scheme_nominee"
    __table_args__ = (
        db.Index("ix_ret_nominee_scheme", "scheme_id"),
    )

    id        = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer,
                          db.ForeignKey("retirement_scheme.id",
                                       ondelete="CASCADE"),
                          nullable=False)
    user_id   = db.Column(db.Integer,
                          db.ForeignKey("user.id"),
                          nullable=False)

    name         = db.Column(db.String(200), nullable=False)
    relationship = db.Column(db.String(100), nullable=False)
    percentage   = db.Column(db.Float, nullable=True)
    # ^ share of the scheme in case of claim — validated so the sum
    #   across a scheme's nominees never exceeds 100% (see services.py)
    contact = db.Column(db.String(20), nullable=True)
    notes   = db.Column(db.String(300), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<RetirementSchemeNominee {self.name} {self.relationship}>"


class RetirementDocument(db.Model):
    """
    Local document metadata for a retirement scheme (Phase D).
    Files live at: instance/documents/retirement/<scheme_id>/<stored_name>
    NEVER store binary contents in the database — metadata only,
    same philosophy as InsuranceDocument.

    Deliberately CASCADE on scheme delete (unlike InsuranceDocument's
    SET NULL) — Retirement Centre has no permanent-delete route for
    schemes yet, only archive/restore, so this simplification carries
    no practical risk of orphaning audit data today.
    """
    __tablename__ = "retirement_document"
    __table_args__ = (
        db.Index("ix_ret_doc_scheme", "scheme_id"),
        db.Index("ix_ret_doc_user",   "user_id"),
    )

    id        = db.Column(db.Integer, primary_key=True)
    scheme_id = db.Column(db.Integer,
                          db.ForeignKey("retirement_scheme.id",
                                       ondelete="CASCADE"),
                          nullable=False)
    user_id   = db.Column(db.Integer,
                          db.ForeignKey("user.id"),
                          nullable=False)

    doc_type      = db.Column(db.String(50), nullable=False)
    title         = db.Column(db.String(255), nullable=True)
    # ^ user-facing display name, distinct from the uploaded file's own
    #   filename — falls back to original_name when not provided
    original_name = db.Column(db.String(255), nullable=False)
    stored_name   = db.Column(db.String(255), nullable=False)
    file_path     = db.Column(db.String(500), nullable=False)
    file_size     = db.Column(db.Integer, nullable=True)
    notes         = db.Column(db.String(500), nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<RetirementDocument {self.doc_type} {self.original_name}>"

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
