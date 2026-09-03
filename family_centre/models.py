"""
Family Centre — Models
=========================
Family Centre started as a pure read-only view over three existing
sources (InsuranceNominee, RetirementSchemeNominee, WealthAsset's
Family & Inherited Wealth). FamilyPerson was its first owned table —
for someone with no nominee entry and no gifted/inherited asset, but
who you still want on record.

DESIGN DECISION (previously open, now committed): FamilyPerson now
also doubles as the metadata home for ANY person shown in the People
view, not just those manually added. Most people in that view arise
purely from a nominee/benefactor/heir entry and have no FamilyPerson
row at all. Rather than a separate table keyed by name, "Primary
Contact" and "Minor + Guardian" status live directly on FamilyPerson,
and a row is created on-demand (get_or_create, in services.py) the
first time someone sets one of these fields on a person who doesn't
already have a manually-added row. This keeps a single source of
truth for "facts about a person" rather than splitting it across two
tables that both key on name.

is_primary_contact is enforced unique-per-user at the service layer
(services.set_primary_contact unsets any previous holder) — not a DB
constraint, matching this app's general preference for
application-level enforcement over DB-level (see WealthDocument
orphaned-reference handling for precedent).
"""
from datetime import datetime
from models import db


class TimelineEvent:
    PERSON_ADDED          = "Person Added"
    PERSON_UPDATED        = "Person Updated"
    PERSON_REMOVED        = "Person Removed"
    PRIMARY_CONTACT_SET   = "Primary Contact Set"
    PRIMARY_CONTACT_UNSET = "Primary Contact Unset"
    MINOR_STATUS_SET      = "Minor + Guardian Set"
    MINOR_STATUS_CLEARED  = "Minor + Guardian Cleared"


class FamilyPerson(db.Model):
    """A person tracked directly by Family Centre — either added
    manually (no nominee/benefactor/heir entry elsewhere) or created
    on-demand to hold Primary Contact / Minor+Guardian metadata for
    someone who otherwise only exists as a nominee/benefactor/heir
    entry in another module.

    is_manual distinguishes the two: True means this row is itself
    "the" record for the person (shown as a Family Member entry,
    editable/deletable in the People view). False means this row
    exists ONLY to hold metadata for a person who already has a
    connection elsewhere — deleting it would just clear that
    metadata, not remove the person (they'd still show up via their
    nominee/benefactor/heir entry).
    """
    __tablename__ = "family_person"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name         = db.Column(db.String(200), nullable=False)
    relationship = db.Column(db.String(100), nullable=True)
    is_manual    = db.Column(db.Boolean, nullable=False, default=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    # ── Primary Contact / Next of Kin ──────────────────────────────
    is_primary_contact = db.Column(db.Boolean, nullable=False, default=False)

    # ── Minor + Guardian ─────────────────────────────────────────────
    is_minor               = db.Column(db.Boolean, nullable=False, default=False)
    guardian_name          = db.Column(db.String(200), nullable=True)
    guardian_relationship  = db.Column(db.String(100), nullable=True)
    guardian_contact       = db.Column(db.String(100), nullable=True)

    def __repr__(self):
        return f"<FamilyPerson {self.name} ({self.relationship})>"


class FamilyTimeline(db.Model):
    """
    Audit history for Family Centre's own actions — matches the
    established Timeline pattern from Insurance/Retirement
    (InsuranceTimeline / RetirementTimeline): append-only, never
    updated or deleted.

    Keyed by user_id rather than a single parent record, since Family
    Centre's actions span multiple kinds of things (adding a person,
    flagging a primary contact, setting guardian info) rather than
    events on one owning entity the way a policy or scheme's timeline
    is. person_name is stored as a plain string snapshot (not just a
    FK to family_person) so the history entry still reads correctly
    even if that FamilyPerson row is later deleted.
    """
    __tablename__ = "family_timeline"
    __table_args__ = (
        db.Index("ix_fam_timeline_user", "user_id"),
    )

    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    event_type  = db.Column(db.String(50), nullable=False)
    # From TimelineEvent constants

    person_name = db.Column(db.String(200), nullable=True)
    # Snapshot of who the event was about, where applicable

    description = db.Column(db.String(500), nullable=False)
    # Human-readable: "Meenakshi marked as Primary Contact"

    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<FamilyTimeline {self.event_type} user={self.user_id}>"
