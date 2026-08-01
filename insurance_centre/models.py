"""
Insurance Centre — Models
=========================
Phase 2: Full database architecture for MyWealthLens Insurance Centre.

Tables:
  1. InsurancePolicy     — core policy record (all categories)
  2. InsuranceNominee    — nominees per policy (one-to-many)
  3. InsuranceMember     — health insured members (health only)
  4. InsuranceAddon      — motor insurance add-ons (motor only)
  5. InsuranceDocument   — local file metadata per policy
  6. InsuranceTimeline   — audit history per policy

Design principles:
  - Single policy table — no separate table per category
  - Soft delete via is_archived flag + archived_at timestamp
  - All relationships use cascade delete-orphan except documents
  - SQLite-compatible, PostgreSQL-ready
"""

from models import db
from datetime import datetime, date



# ── Enumerations ─────────────────────────────────────────────────────────────

class InsuranceCategory:
    LIFE       = "Life Insurance"
    HEALTH     = "Health Insurance"
    MOTOR      = "Motor Insurance"
    PROPERTY   = "Property Insurance"
    GENERAL    = "General Insurance"

    ALL = [LIFE, HEALTH, MOTOR, PROPERTY, GENERAL]


class InsuranceType:
    """Predefined types per category. Frontend uses these for dropdowns."""
    LIFE = [
        "Term Insurance",
        "Whole Life Insurance",
        "Endowment Plan",
        "ULIP (Unit Linked Insurance Plan)",
        "Money Back Policy",
        "Child Plan",
        "Pension / Annuity Plan",
        "Group Term Insurance",
        "Other (Custom)",
    ]
    HEALTH = [
        "Individual Health Insurance",
        "Family Floater",
        "Senior Citizen Health Insurance",
        "Critical Illness Cover",
        "Personal Accident Insurance",
        "Group Health Insurance",
        "Top-Up / Super Top-Up",
        "Hospital Daily Cash",
        "Maternity Insurance",
        "Other (Custom)",
    ]
    MOTOR = [
        "Car Insurance (Comprehensive)",
        "Car Insurance (Third Party)",
        "Two-Wheeler Insurance (Comprehensive)",
        "Two-Wheeler Insurance (Third Party)",
        "Commercial Vehicle Insurance",
        "Other (Custom)",
    ]
    PROPERTY = [
        "Home Insurance",
        "Renter's Insurance",
        "Fire Insurance",
        "Flood Insurance",
        "Burglary Insurance",
        "Shop / Office Insurance",
        "Other (Custom)",
    ]
    GENERAL = [
        "Travel Insurance",
        "Marine Insurance",
        "Liability Insurance",
        "Cyber Insurance",
        "Event Insurance",
        "Pet Insurance",
        "Mobile Insurance",
        "Other (Custom)",
    ]

    @classmethod
    def for_category(cls, category):
        mapping = {
            InsuranceCategory.LIFE:     cls.LIFE,
            InsuranceCategory.HEALTH:   cls.HEALTH,
            InsuranceCategory.MOTOR:    cls.MOTOR,
            InsuranceCategory.PROPERTY: cls.PROPERTY,
            InsuranceCategory.GENERAL:  cls.GENERAL,
        }
        return mapping.get(category, ["Other (Custom)"])


class PolicyStatus:
    ACTIVE      = "Active"
    CLAIMED     = "Claimed"
    EXPIRED     = "Expired"
    LAPSED      = "Lapsed"
    MATURED     = "Matured"
    SURRENDERED = "Surrendered"
    ARCHIVED    = "Archived"

    ALL = [ACTIVE, CLAIMED, EXPIRED, LAPSED, MATURED, SURRENDERED, ARCHIVED]


class PremiumFrequency:
    MONTHLY     = "Monthly"
    QUARTERLY   = "Quarterly"
    HALF_YEARLY = "Half-Yearly"
    YEARLY      = "Yearly"
    ONE_TIME    = "One-Time"

    ALL = [MONTHLY, QUARTERLY, HALF_YEARLY, YEARLY, ONE_TIME]


class NomineeRelation:
    ALL = ["Spouse", "Father", "Mother", "Son", "Daughter",
           "Brother", "Sister", "Grandparent", "Grandchild",
           "Friend", "Other"]


class MemberRelation:
    ALL = ["Self", "Spouse", "Father", "Mother", "Son", "Daughter", "Other"]


class MotorAddonType:
    ALL = [
        "Zero Depreciation",
        "Engine Protection",
        "Roadside Assistance",
        "Consumables Cover",
        "Key Replacement",
        "Return to Invoice",
        "Passenger Cover",
        "Other",
    ]


class DocumentType:
    POLICY_PDF       = "Policy PDF"
    PREMIUM_RECEIPT  = "Premium Receipt"
    RENEWAL_RECEIPT  = "Renewal Receipt"
    CLAIM_DOCUMENTS  = "Claim Documents"
    OTHER_DOCUMENTS  = "Other Documents"

    ALL = [POLICY_PDF, PREMIUM_RECEIPT, RENEWAL_RECEIPT,
           CLAIM_DOCUMENTS, OTHER_DOCUMENTS]


class TimelineEvent:
    CREATED           = "Policy Created"
    COVERAGE_UPDATED  = "Coverage Updated"
    PREMIUM_UPDATED   = "Premium Updated"
    NOMINEE_UPDATED   = "Nominee Updated"
    MEMBER_UPDATED    = "Member Updated"
    ADDON_UPDATED     = "Add-on Updated"
    DOCUMENT_UPLOADED = "Document Uploaded"
    DOCUMENT_DELETED  = "Document Deleted"
    POLICY_RENEWED    = "Policy Renewed"
    STATUS_CHANGED    = "Status Changed"
    NOTES_UPDATED     = "Notes Updated"
    ARCHIVED          = "Archived"
    RESTORED          = "Restored"


# ── Models ────────────────────────────────────────────────────────────────────

class InsurancePolicy(db.Model):
    """
    Core policy record — single table for all insurance categories.
    Category + insurance_type distinguish policies.
    Soft delete via is_archived + archived_at.
    """
    __tablename__ = "insurance_policy"
    __table_args__ = (
        db.Index("ix_ins_policy_user",     "user_id"),
        db.Index("ix_ins_policy_category", "category"),
        db.Index("ix_ins_policy_archived", "is_archived"),
        db.Index("ix_ins_policy_renewal",  "renewal_date"),
        db.Index("ix_ins_policy_number",   "user_id", "policy_number"),
    )

    # ── Primary ──────────────────────────────────────────────────
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer,
                                db.ForeignKey("user.id", ondelete="CASCADE"),
                                nullable=False)

    # ── Category & Type ──────────────────────────────────────────
    category        = db.Column(db.String(50),  nullable=False)
    insurance_type  = db.Column(db.String(100), nullable=False)
    custom_type     = db.Column(db.String(200), nullable=True)
    # ^ Used only when insurance_type == "Other (Custom)"

    # ── Policy Details ───────────────────────────────────────────
    insurer         = db.Column(db.String(200), nullable=False)
    policy_name     = db.Column(db.String(200), nullable=True)
    policy_number   = db.Column(db.String(100), nullable=True)
    policy_holder   = db.Column(db.String(200), nullable=True)
    # ^ Name of person in whose name policy is held

    # ── Coverage ─────────────────────────────────────────────────
    sum_assured     = db.Column(db.Float, nullable=False, default=0)

    # ── Premium ──────────────────────────────────────────────────
    premium_amount  = db.Column(db.Float, nullable=False, default=0)
    premium_frequency = db.Column(db.String(20),
                                  nullable=False,
                                  default=PremiumFrequency.YEARLY)
    # Annual equivalent for calculations
    @property
    def annual_premium(self):
        multipliers = {
            PremiumFrequency.MONTHLY:     12,
            PremiumFrequency.QUARTERLY:   4,
            PremiumFrequency.HALF_YEARLY: 2,
            PremiumFrequency.YEARLY:      1,
            PremiumFrequency.ONE_TIME:    0,
        }
        return self.premium_amount * multipliers.get(self.premium_frequency, 1)

    # ── Status ───────────────────────────────────────────────────
    status          = db.Column(db.String(20),
                                nullable=False,
                                default=PolicyStatus.ACTIVE)

    # ── Dates ────────────────────────────────────────────────────
    start_date      = db.Column(db.Date, nullable=True)
    maturity_date   = db.Column(db.Date, nullable=True)
    renewal_date    = db.Column(db.Date, nullable=True)
    expiry_date     = db.Column(db.Date, nullable=True)
    next_premium_due = db.Column(db.Date, nullable=True)

    # ── Soft Delete ──────────────────────────────────────────────
    is_archived     = db.Column(db.Boolean, default=False, nullable=False)
    archived_at     = db.Column(db.DateTime, nullable=True)
    archived_by     = db.Column(db.Integer,
                                db.ForeignKey("user.id"),
                                nullable=True)

    # ── Notes ────────────────────────────────────────────────────
    notes           = db.Column(db.Text, nullable=True)

    # ── Life Insurance specific ──────────────────────────────────
    agent_name      = db.Column(db.String(200), nullable=True)
    agent_contact   = db.Column(db.String(50),  nullable=True)

    # ── Search support fields (Motor/Property specific) ──────────
    vehicle_number  = db.Column(db.String(50), nullable=True)
    property_name   = db.Column(db.String(200), nullable=True)

    # ── Timestamps ───────────────────────────────────────────────
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    # ── Relationships ─────────────────────────────────────────────
    nominees  = db.relationship("InsuranceNominee",
                                backref="policy",
                                lazy="dynamic",
                                cascade="all, delete-orphan")
    members   = db.relationship("InsuranceMember",
                                backref="policy",
                                lazy="dynamic",
                                cascade="all, delete-orphan")
    addons    = db.relationship("InsuranceAddon",
                                backref="policy",
                                lazy="dynamic",
                                cascade="all, delete-orphan")
    documents = db.relationship("InsuranceDocument",
                                backref="policy",
                                lazy="dynamic",
                                passive_deletes=True)
    # ^ passive_deletes=True — documents are NOT auto-deleted
    #   when policy is archived. Must be explicitly deleted.
    timeline  = db.relationship("InsuranceTimeline",
                                backref="policy",
                                lazy="dynamic",
                                cascade="all, delete-orphan",
                                order_by="InsuranceTimeline.created_at.desc()")

    # ── Computed Properties ───────────────────────────────────────
    @property
    def display_type(self):
        if self.insurance_type == "Other (Custom)" and self.custom_type:
            return self.custom_type
        return self.insurance_type

    @property
    def days_to_renewal(self):
        if not self.renewal_date:
            return None
        return (self.renewal_date - date.today()).days

    @property
    def renewal_status(self):
        days = self.days_to_renewal
        if days is None:       return "unknown"
        if days < 0:           return "overdue"
        if days <= 30:         return "due_soon"
        if days <= 90:         return "upcoming"
        return "active"

    @property
    def total_nominees_percentage(self):
        return sum(n.percentage or 0 for n in self.nominees)

    def __repr__(self):
        return f"<InsurancePolicy id={self.id} {self.category} {self.insurer}>"


class InsuranceNominee(db.Model):
    """Nominees for any insurance policy. One policy → many nominees."""
    __tablename__ = "insurance_nominee"
    __table_args__ = (
        db.Index("ix_ins_nominee_policy", "policy_id"),
    )

    id           = db.Column(db.Integer, primary_key=True)
    policy_id    = db.Column(db.Integer,
                             db.ForeignKey("insurance_policy.id",
                                           ondelete="CASCADE"),
                             nullable=False)
    user_id      = db.Column(db.Integer,
                             db.ForeignKey("user.id"),
                             nullable=False)

    name         = db.Column(db.String(200), nullable=False)
    relationship = db.Column(db.String(100), nullable=False)
    percentage   = db.Column(db.Float, nullable=True)
    # ^ Share of sum assured in case of claim (should sum to 100)
    contact      = db.Column(db.String(20), nullable=True)

    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow,
                             onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<InsuranceNominee {self.name} {self.relationship}>"


class InsuranceMember(db.Model):
    """
    Health insured members — only for Health Insurance category.
    Tracks who is covered under the policy.
    """
    __tablename__ = "insurance_member"
    __table_args__ = (
        db.Index("ix_ins_member_policy", "policy_id"),
    )

    id           = db.Column(db.Integer, primary_key=True)
    policy_id    = db.Column(db.Integer,
                             db.ForeignKey("insurance_policy.id",
                                           ondelete="CASCADE"),
                             nullable=False)
    user_id      = db.Column(db.Integer,
                             db.ForeignKey("user.id"),
                             nullable=False)

    member_name  = db.Column(db.String(200), nullable=False)
    age          = db.Column(db.Integer, nullable=True)
    relationship = db.Column(db.String(100), nullable=False)
    # Self / Spouse / Father / Mother / Son / Daughter / Other

    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at   = db.Column(db.DateTime, default=datetime.utcnow,
                             onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<InsuranceMember {self.member_name} age={self.age}>"


class InsuranceAddon(db.Model):
    """
    Motor insurance add-ons — only for Motor Insurance category.
    Stored as individual rows, not comma-separated.
    """
    __tablename__ = "insurance_addon"
    __table_args__ = (
        db.Index("ix_ins_addon_policy", "policy_id"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    policy_id  = db.Column(db.Integer,
                           db.ForeignKey("insurance_policy.id",
                                         ondelete="CASCADE"),
                           nullable=False)
    user_id    = db.Column(db.Integer,
                           db.ForeignKey("user.id"),
                           nullable=False)

    addon_name = db.Column(db.String(100), nullable=False)
    # From MotorAddonType.ALL or custom

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<InsuranceAddon {self.addon_name}>"


class InsuranceDocument(db.Model):
    """
    Local document metadata for insurance policies.
    Files stored at: instance/documents/insurance/<policy_id>/<stored_filename>
    NEVER store binary contents in database.
    """
    __tablename__ = "insurance_document"
    __table_args__ = (
        db.Index("ix_ins_doc_policy", "policy_id"),
        db.Index("ix_ins_doc_user",   "user_id"),
    )

    id            = db.Column(db.Integer, primary_key=True)
    policy_id     = db.Column(db.Integer,
                              db.ForeignKey("insurance_policy.id",
                                            ondelete="SET NULL"),
                              nullable=True)
    # ^ SET NULL — if policy deleted, document record stays
    #   for audit purposes. Clean up separately.
    user_id       = db.Column(db.Integer,
                              db.ForeignKey("user.id"),
                              nullable=False)

    doc_type      = db.Column(db.String(50), nullable=False)
    # From DocumentType.ALL

    original_name = db.Column(db.String(255), nullable=False)
    # Original filename from user's computer

    stored_name   = db.Column(db.String(255), nullable=False)
    # UUID-based stored filename (prevents collisions)

    file_path     = db.Column(db.String(500), nullable=False)
    # Full local path: instance/documents/insurance/<policy_id>/<stored_name>

    file_size     = db.Column(db.Integer, nullable=True)
    # Size in bytes

    notes         = db.Column(db.String(500), nullable=True)
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<InsuranceDocument {self.doc_type} {self.original_name}>"

    @property
    def file_size_display(self):
        if not self.file_size:
            return "Unknown"
        if self.file_size < 1024:
            return f"{self.file_size} B"
        if self.file_size < 1024 * 1024:
            return f"{self.file_size / 1024:.1f} KB"
        return f"{self.file_size / (1024*1024):.1f} MB"


class InsuranceTimeline(db.Model):
    """
    Audit history for every insurance policy.
    Append-only — never update or delete existing entries.
    """
    __tablename__ = "insurance_timeline"
    __table_args__ = (
        db.Index("ix_ins_timeline_policy", "policy_id"),
    )

    id          = db.Column(db.Integer, primary_key=True)
    policy_id   = db.Column(db.Integer,
                            db.ForeignKey("insurance_policy.id",
                                          ondelete="CASCADE"),
                            nullable=False)
    user_id     = db.Column(db.Integer,
                            db.ForeignKey("user.id"),
                            nullable=False)

    event_type  = db.Column(db.String(50),  nullable=False)
    # From TimelineEvent constants

    description = db.Column(db.String(500), nullable=False)
    # Human-readable: "Coverage updated from ₹50L to ₹1Cr"

    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<InsuranceTimeline {self.event_type} policy={self.policy_id}>"
