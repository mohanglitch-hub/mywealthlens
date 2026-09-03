"""
Family Centre — Services
==========================
Business logic layer for the four newly-approved features:
Primary Contact / Next of Kin, Minor + Guardian tracking, and the
Family Centre audit trail. No Flask request/response objects here —
pure Python, matching insurance_centre/services.py and
retirement_centre/services.py's own convention.

The existing add/edit/delete-person routes still live directly in
routes.py (unchanged, to avoid churn on working code) — only the new
functionality introduced this phase lives here.
"""
from models import db
from .models import FamilyPerson, FamilyTimeline, TimelineEvent


# ── Timeline Helper ──────────────────────────────────────────────────────

def log_timeline(user_id, event_type, description, person_name=None):
    """Append an entry to the Family Centre audit trail. Never
    overwrites — matches insurance_centre.services.log_timeline."""
    entry = FamilyTimeline(
        user_id     = user_id,
        event_type  = event_type,
        person_name = person_name,
        description = description,
    )
    db.session.add(entry)


# ── Get-or-create ─────────────────────────────────────────────────────────

def get_or_create_family_person(user_id, name, relationship=None):
    """
    Returns the FamilyPerson row for this name (case-insensitive
    match, matching the People view's own grouping rule), creating
    one with is_manual=False if none exists yet.

    This is the on-demand row creation the design decision commits
    to: a person who only exists today as, say, an Insurance nominee
    gets a FamilyPerson row the FIRST time someone sets a Primary
    Contact flag or Minor+Guardian info on them — not before. Viewing
    the People page never calls this; only an actual field-set action
    does, so browsing the dashboard never silently creates rows.
    """
    name = (name or "").strip()
    if not name:
        return None

    existing = FamilyPerson.query.filter(
        FamilyPerson.user_id == user_id,
        db.func.lower(FamilyPerson.name) == name.lower(),
    ).first()
    if existing:
        return existing

    person = FamilyPerson(
        user_id=user_id, name=name, relationship=relationship, is_manual=False,
    )
    db.session.add(person)
    db.session.flush()  # get an id without a full commit yet
    return person


# ── Primary Contact / Next of Kin ────────────────────────────────────────

def set_primary_contact(user_id, name):
    """
    Marks `name` as the Primary Contact / Next of Kin, unsetting any
    previous holder for this user (enforced here, not at the DB
    level — only one person can be the primary contact at a time).
    """
    person = get_or_create_family_person(user_id, name)
    if person is None:
        return None, "A name is required."

    previous = FamilyPerson.query.filter_by(
        user_id=user_id, is_primary_contact=True
    ).first()
    if previous and previous.id != person.id:
        previous.is_primary_contact = False
        log_timeline(
            user_id, TimelineEvent.PRIMARY_CONTACT_UNSET,
            f"{previous.name} unmarked as Primary Contact "
            f"(replaced by {person.name})",
            person_name=previous.name,
        )

    person.is_primary_contact = True
    log_timeline(
        user_id, TimelineEvent.PRIMARY_CONTACT_SET,
        f"{person.name} marked as Primary Contact",
        person_name=person.name,
    )
    db.session.commit()
    return person, None


def clear_primary_contact(user_id, name):
    person = FamilyPerson.query.filter(
        FamilyPerson.user_id == user_id,
        db.func.lower(FamilyPerson.name) == (name or "").strip().lower(),
    ).first()
    if person and person.is_primary_contact:
        person.is_primary_contact = False
        log_timeline(
            user_id, TimelineEvent.PRIMARY_CONTACT_UNSET,
            f"{person.name} unmarked as Primary Contact",
            person_name=person.name,
        )
        db.session.commit()
    return person


# ── Minor + Guardian ──────────────────────────────────────────────────────

def set_minor_guardian(user_id, name, guardian_name, guardian_relationship, guardian_contact):
    """
    Marks `name` as a minor and records their guardian's details.
    guardian_name is required whenever is_minor is being set — a
    minor flag with no guardian on record defeats the purpose of the
    feature (this mirrors the app's existing "flag rather than allow
    a silently incomplete record" discipline used for Coverage Gaps).
    """
    guardian_name = (guardian_name or "").strip()
    if not guardian_name:
        return None, "Guardian name is required when marking someone as a minor."

    person = get_or_create_family_person(user_id, name)
    if person is None:
        return None, "A name is required."

    person.is_minor = True
    person.guardian_name = guardian_name
    person.guardian_relationship = (guardian_relationship or "").strip() or None
    person.guardian_contact = (guardian_contact or "").strip() or None

    log_timeline(
        user_id, TimelineEvent.MINOR_STATUS_SET,
        f"{person.name} marked as a minor — guardian: {guardian_name}"
        + (f" ({person.guardian_relationship})" if person.guardian_relationship else ""),
        person_name=person.name,
    )
    db.session.commit()
    return person, None


def clear_minor_guardian(user_id, name):
    person = FamilyPerson.query.filter(
        FamilyPerson.user_id == user_id,
        db.func.lower(FamilyPerson.name) == (name or "").strip().lower(),
    ).first()
    if person and person.is_minor:
        person.is_minor = False
        person.guardian_name = None
        person.guardian_relationship = None
        person.guardian_contact = None
        log_timeline(
            user_id, TimelineEvent.MINOR_STATUS_CLEARED,
            f"{person.name} — minor + guardian status cleared",
            person_name=person.name,
        )
        db.session.commit()
    return person


# ── Metadata lookup for the People view ──────────────────────────────────

def get_metadata_by_name(user_id):
    """
    Returns {lowercased_name: FamilyPerson} for every FamilyPerson row
    belonging to this user — used by routes._build_people to attach
    is_primary_contact / is_minor / guardian_* onto each person dict,
    including people who only exist via a nominee/benefactor/heir
    entry (their FamilyPerson row, if any, was created on-demand by
    set_primary_contact / set_minor_guardian above).
    """
    rows = FamilyPerson.query.filter_by(user_id=user_id).all()
    return {p.name.strip().lower(): p for p in rows}
