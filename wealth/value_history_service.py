"""
Wealth — Value History Service (Phase J)
============================================
Item-level financial-value history for individual WealthAsset and
WealthLiability records — distinct from WealthSnapshot (the Wealth-
wide aggregate, Phase F). See the class docstring on
WealthValueSnapshot in models.py for the full architectural
rationale; this module is the service layer that activates that
previously-dormant table.

Two entity types share one table via a lightweight entity_type +
entity_id design (not a formal cross-table FK, since SQLite/SQLAlchemy
can't FK to "either of two tables") — every function here takes an
explicit entity_type so callers never have to guess the convention.
"""

from datetime import datetime

from .models import WealthValueSnapshot

ENTITY_ASSET = "asset"
ENTITY_LIABILITY = "liability"

NOTE_INITIAL = "Initial Value"
NOTE_UPDATED = "Value Updated"

# Values are stored as Float (matching WealthAsset.current_value /
# WealthLiability.outstanding_amount, which are also Float — the
# whole app uses Float throughout, not Decimal/paise-integers, so
# this reuses that same representation per Section 27 of the spec
# rather than introducing a new one). A small epsilon avoids treating
# floating-point rounding noise as a genuine value change.
_EPSILON = 0.005


def values_differ(old_value, new_value):
    """
    Public helper: true numeric comparison for financial values,
    with a small epsilon to avoid floating-point rounding noise being
    mistaken for a genuine change (Section 26/27). Exposed so callers
    (wealth/services.py's update_asset/update_liability) can decide
    *whether* to call record_wealth_value_change() without needing to
    know that function's own internal deduplication logic — both use
    this same comparison, so the two can never disagree.
    """
    if old_value is None:
        return True
    return abs((old_value or 0) - (new_value or 0)) > _EPSILON


def _values_differ(old_value, new_value):
    return values_differ(old_value, new_value)


def latest_value_record(user_id, entity_type, entity_id):
    """Most recent value-history row for this entity, or None."""
    return (WealthValueSnapshot.query
            .filter_by(user_id=user_id, entity_type=entity_type, entity_id=entity_id)
            .order_by(WealthValueSnapshot.created_at.desc(),
                     WealthValueSnapshot.id.desc())
            .first())


def record_wealth_value_change(db, user_id, entity_type, entity_id, new_value,
                               is_initial=False):
    """
    The single entry point for creating a value-history record
    (Section 41 — one reusable service, not duplicated per route).

    Called from wealth/services.py's create_asset / update_asset /
    create_liability / update_liability — never called directly from
    a route or from Phase I's daily scheduler (Section 15/83: the
    trigger is a genuine value change, not a page visit or a cron
    tick).

    Deduplication (Section 8/25/26): compares against the entity's
    OWN most recent record (not "today's" record — an entity can
    have zero or several genuine changes on the same calendar day,
    Section 24) using a numeric epsilon comparison, never a string
    comparison of formatted currency (Section 26).

    Does NOT call db.session.commit() — the caller is responsible
    for committing this row in the SAME transaction as the
    WealthAsset/WealthLiability update itself (Section 21/22:
    atomic — if the commit fails, neither the value update nor its
    history record should have taken effect). This function only
    adds to the session.

    Returns the created WealthValueSnapshot, or None if no record
    was needed (value unchanged from the entity's last recorded
    value).
    """
    if not is_initial:
        latest = latest_value_record(user_id, entity_type, entity_id)
        if latest is not None and not _values_differ(latest.value, new_value):
            return None

    record = WealthValueSnapshot(
        user_id=user_id, entity_type=entity_type, entity_id=entity_id,
        value=new_value, snapshot_date=datetime.utcnow().date(),
        note=NOTE_INITIAL if is_initial else NOTE_UPDATED,
    )
    db.session.add(record)
    return record


def get_value_history(user_id, entity_type, entity_id, limit=None):
    """
    Full value history for one entity, newest first (Section 48).
    Scoped by user_id — never trust entity_id alone (Section 59-61:
    this is the IDOR guard, on top of the caller already having
    confirmed the entity itself belongs to this user).
    """
    q = (WealthValueSnapshot.query
         .filter_by(user_id=user_id, entity_type=entity_type, entity_id=entity_id)
         .order_by(WealthValueSnapshot.created_at.desc(),
                  WealthValueSnapshot.id.desc()))
    if limit:
        q = q.limit(limit)
    return q.all()


def delete_value_history_for_entity(db, user_id, entity_type, entity_id):
    """
    Removes all value-history rows for one entity. Called ONLY from
    the permanent-delete path (Section 34/35 — entity_id has no
    formal database-level FK/cascade, by design, since it's shared
    across two possible parent tables, so this explicit cleanup is
    what prevents orphaned "Unknown Asset" rows from ever being
    possible once the parent is gone for good).

    Never called for archive/restore (Section 32/33 — history
    survives archiving) — only for the genuinely permanent delete
    path. Does NOT commit — same atomic-transaction responsibility
    as record_wealth_value_change().
    """
    WealthValueSnapshot.query.filter_by(
        user_id=user_id, entity_type=entity_type, entity_id=entity_id
    ).delete()
