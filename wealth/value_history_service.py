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

"""
Wealth — Value History Service (Phase J, extended Phase L)
================================================================
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

Phase L adds explicit effective_date handling (Section 2/3): every
record now distinguishes WHEN a value financially applies
(effective_date, user-controlled, may be backdated) from WHEN
MyWealthLens received it (created_at, application-controlled,
immutable). See record_wealth_value_change() for the full
same-effective-date/correction/future-date rules.
"""

from .models import WealthValueSnapshot
from .timezone_utils import today_ist

ENTITY_ASSET = "asset"
ENTITY_LIABILITY = "liability"

NOTE_INITIAL = "Initial Value"
NOTE_UPDATED = "Value Updated"

# Values are stored as Float (matching WealthAsset.current_value /
# WealthLiability.outstanding_amount, which are also Float — the
# whole app uses Float throughout, not Decimal/paise-integers, so
# this reuses that same representation per Section 27 of the Phase J
# spec rather than introducing a new one). A small epsilon avoids
# treating floating-point rounding noise as a genuine value change.
_EPSILON = 0.005


def values_differ(old_value, new_value):
    """
    Public helper: true numeric comparison for financial values,
    with a small epsilon to avoid floating-point rounding noise being
    mistaken for a genuine change. Exposed so callers
    (wealth/services.py's update_asset/update_liability) can decide
    *whether* this represents a genuine change without needing to
    know record_wealth_value_change()'s own internal logic — both
    use this same comparison, so the two can never disagree.
    """
    if old_value is None:
        return True
    return abs((old_value or 0) - (new_value or 0)) > _EPSILON


class FutureEffectiveDateError(ValueError):
    """
    Raised when a caller attempts to record a value with an
    effective_date after today (Phase L, Section 28/29 — server-side
    enforcement is mandatory regardless of what the client already
    checked). Callers (routes) catch this and surface a clear,
    non-technical error message (Section 109) rather than a raw
    500/traceback.
    """
    pass



def record_for_effective_date(user_id, entity_type, entity_id, effective_date):
    """
    The authoritative record for one EXACT effective_date, if any
    exists (Section 18: "use the latest recorded record as the
    effective valuation for that date" when multiple corrections
    share a date). Used both for the same-date dedup/correction rule
    in record_wealth_value_change() and by callers that want "what do
    we currently believe the value was on this specific date".
    """
    return (WealthValueSnapshot.query
            .filter_by(user_id=user_id, entity_type=entity_type,
                      entity_id=entity_id, effective_date=effective_date)
            .order_by(WealthValueSnapshot.created_at.desc(),
                     WealthValueSnapshot.id.desc())
            .first())


def record_wealth_value_change(db, user_id, entity_type, entity_id, new_value,
                               effective_date=None, is_initial=False):
    """
    The single entry point for creating a value-history record
    (Section 41 of Phase J spec — one reusable service, not
    duplicated per route). Called from wealth/services.py's
    create_asset / update_asset / create_liability / update_liability
    — never called directly from a route or from Phase I's daily
    scheduler.

    effective_date (Phase L, Section 2/10/13): the date this value
    financially applies to. Defaults to today (IST) when omitted —
    every pre-Phase-L caller keeps working with ordinary "today"
    semantics unchanged. Never allowed in the future (Section 28) —
    raises FutureEffectiveDateError, which is the ONLY way this
    function signals a validation failure (it does not silently clamp
    or ignore an invalid date).

    Same-effective-date rule (Section 16-19, 56-58): dedup/correction
    logic operates PER EFFECTIVE DATE, not against the single most
    recent record overall — this is what makes backdating and
    same-day-different-value corrections both work correctly:
      - No record yet exists for this exact effective_date -> always
        create a new one (Section 20/33/80 — a new point on the
        timeline, regardless of what value it happens to share with
        other dates).
      - A record already exists for this exact effective_date with
        the SAME value -> skip, no duplicate (mirrors the original
        Phase J same-value-same-day rule, Section 92 test).
      - A record already exists for this exact effective_date with a
        DIFFERENT value -> this is a correction (Section 56/57):
        create a NEW record, leave the old one untouched forever
        (Section 58/90 — historical records are never overwritten,
        even to "fix" them).

    is_initial only affects the note label (Section 66) — the dedup
    logic above already naturally has nothing to match against for a
    brand-new entity, so no separate bypass branch is needed.

    Does NOT call db.session.commit() — the caller is responsible for
    committing this row in the SAME transaction as the WealthAsset/
    WealthLiability update itself (Section 21/22 of Phase J spec /
    Section 66/67 of Phase L spec — atomic either way).

    Returns the created WealthValueSnapshot, or None if no record was
    needed (an exact duplicate for that effective_date already
    exists).
    """
    if effective_date is None:
        effective_date = today_ist()

    if effective_date > today_ist():
        raise FutureEffectiveDateError(
            "Valuation date cannot be in the future.")

    existing_for_date = record_for_effective_date(
        user_id, entity_type, entity_id, effective_date)
    if existing_for_date is not None and not values_differ(existing_for_date.value, new_value):
        return None

    record = WealthValueSnapshot(
        user_id=user_id, entity_type=entity_type, entity_id=entity_id,
        value=new_value, effective_date=effective_date,
        note=NOTE_INITIAL if is_initial else NOTE_UPDATED,
    )
    db.session.add(record)
    return record


def get_value_history(user_id, entity_type, entity_id, limit=None):
    """
    Full RAW value history for one entity, financial-chronology order
    (effective_date DESC, then created_at DESC as tie-breaker —
    Section 42). "Raw" means every record is included, even a record
    that was later superseded by a same-date correction (Section 19 —
    "what did the user originally record" stays visible in the full
    history, distinct from get_effective_timeline()'s collapsed view
    used for charts/analytics).

    Scoped by user_id — never trust entity_id alone (IDOR guard, on
    top of the caller already having confirmed the entity itself
    belongs to this user).
    """
    q = (WealthValueSnapshot.query
         .filter_by(user_id=user_id, entity_type=entity_type, entity_id=entity_id)
         .order_by(WealthValueSnapshot.effective_date.desc(),
                  WealthValueSnapshot.created_at.desc(),
                  WealthValueSnapshot.id.desc()))
    if limit:
        q = q.limit(limit)
    return q.all()


def get_effective_timeline(user_id, entity_type, entity_id):
    """
    One point per DISTINCT effective_date, oldest first — the
    collapsed view charts and analytics must use (Section 45/76:
    "the chart should use the latest recorded valuation for that
    effective date... do not plot contradictory points at the exact
    same date"). When a same-date correction exists, only the
    latest-recorded record for that date appears here; the earlier,
    superseded one is still visible via get_value_history(), just not
    in this timeline.
    """
    raw = get_value_history(user_id, entity_type, entity_id)  # newest-first
    seen_dates = set()
    collapsed = []
    for record in raw:  # newest created_at wins per date, since raw is
        # already ordered with created_at DESC as the tie-breaker —
        # the FIRST time we see a given effective_date in this loop
        # is therefore automatically the latest-recorded one for it.
        if record.effective_date in seen_dates:
            continue
        seen_dates.add(record.effective_date)
        collapsed.append(record)
    collapsed.reverse()  # oldest first, for chart/CAGR chronology
    return collapsed


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
