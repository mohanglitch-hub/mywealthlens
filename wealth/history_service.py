"""
Wealth — History Service (Phase F)
=====================================
Dedicated service layer for Wealth History / snapshots, kept as its
own file rather than folded into services.py (which is already 670+
lines) — the same organizational choice this project already makes
for a large, distinct concern (see retirement_centre/export.py).
Section 14 of the Phase F spec explicitly suggests this file name.

CRITICAL: this module NEVER computes Net Worth, Total Assets, or any
other Wealth figure itself. Every current-state number it stores
into a snapshot comes from WealthStatisticsService.summary_dict()
(services.py) — the single authoritative source established in
Phase D and reused by every phase since. This file only adds a time
dimension on top of it: create/read/delete snapshots, and diff two
snapshots. No competing financial formula lives here.
"""

from datetime import date, timedelta

from .models import WealthSnapshot
from .services import WealthStatisticsService


# ── Snapshot Retrieval ────────────────────────────────────────────────────────

def get_snapshot_by_date(user_id, snapshot_date):
    """One snapshot for this user on this exact date, or None."""
    return WealthSnapshot.query.filter_by(
        user_id=user_id, snapshot_date=snapshot_date).first()


def get_snapshot_or_none(snapshot_id, user_id):
    """Fetch a single snapshot, scoped to the owning user (Section 30
    — user isolation enforced at the query level). None if not found
    or not owned — routes turn this into a 404."""
    return WealthSnapshot.query.filter_by(
        id=snapshot_id, user_id=user_id).first()


_RANGE_DAYS = {"3m": 92, "6m": 183, "1y": 366}


def get_snapshots(user_id, range_filter="all"):
    """
    All snapshots for this user within the requested range, newest
    first (Section 17). Filtering happens at the database query level
    (Section 49 — never load everything into Python first).

    range_filter: "3m" | "6m" | "1y" | "all". Unknown values fall
    back to "all" rather than erroring — a bad querystring shouldn't
    break the page.
    """
    query = WealthSnapshot.query.filter_by(user_id=user_id)

    if range_filter in _RANGE_DAYS:
        cutoff = date.today() - timedelta(days=_RANGE_DAYS[range_filter])
        query = query.filter(WealthSnapshot.snapshot_date >= cutoff)

    return query.order_by(WealthSnapshot.snapshot_date.desc()).all()


def get_previous_snapshot(user_id, snapshot_date):
    """
    The snapshot immediately BEFORE the given date for this user, by
    snapshot_date — not by created_at (Section 23/55: comparisons are
    always chronological-by-period, regardless of the order snapshots
    happened to be created in).
    """
    return (WealthSnapshot.query
            .filter(WealthSnapshot.user_id == user_id,
                    WealthSnapshot.snapshot_date < snapshot_date)
            .order_by(WealthSnapshot.snapshot_date.desc())
            .first())


def latest_snapshot(user_id):
    return (WealthSnapshot.query.filter_by(user_id=user_id)
            .order_by(WealthSnapshot.snapshot_date.desc())
            .first())


def snapshot_count(user_id):
    return WealthSnapshot.query.filter_by(user_id=user_id).count()


# ── Snapshot Creation ─────────────────────────────────────────────────────────

def _current_wealth_fields(user_id):
    """
    Pull today's authoritative Wealth position from
    WealthStatisticsService — the ONLY place these numbers are
    calculated (Section 15/16 of spec). Net Worth stored is the
    attributable figure, matching the spec's own Section 13 worked
    example (My Attributable Assets − My Attributable Liabilities),
    not the gross Total Assets − Total Liabilities figure.
    """
    stats = WealthStatisticsService(user_id)
    return {
        "total_asset_value":            stats.total_assets(),
        "attributable_asset_value":     stats.attributable_assets_total(),
        "total_liability_value":        stats.total_liabilities(),
        "attributable_liability_value": stats.attributable_liabilities_total(),
        "net_worth":                    stats.attributable_net_worth(),
    }


def create_snapshot(db, user_id, snapshot_date, confirm_replace=False):
    """
    Create a new Wealth snapshot dated `snapshot_date`, capturing
    TODAY's current Wealth position (Section 15 — snapshot creation
    reads current state, it never reconstructs a past date).

    Returns (snapshot, error, needs_confirmation):
      - success:            (snapshot, None, False)
      - duplicate, no confirm: (None, None, True)  — caller should
        show a "replace?" confirmation, nothing was written
      - validation/permission error: (None, error_string, False)

    Duplicate-date handling (Section 10/11): one snapshot per user
    per date. A second create for the same date is never silently
    accepted — it either bounces back asking for confirmation, or,
    once confirm_replace=True, overwrites the existing row in place
    (same id, so it's an update, not a new row).
    """
    existing = get_snapshot_by_date(user_id, snapshot_date)

    if existing and not confirm_replace:
        return None, None, True

    fields = _current_wealth_fields(user_id)

    if existing:
        # Replace in place — explicit confirmation already given by
        # the caller (Section 11). Same row, values overwritten.
        for key, value in fields.items():
            setattr(existing, key, value)
        db.session.commit()
        return existing, None, False

    snapshot = WealthSnapshot(user_id=user_id, snapshot_date=snapshot_date, **fields)
    db.session.add(snapshot)
    db.session.commit()
    return snapshot, None, False


def delete_snapshot(db, snapshot, user_id):
    """
    Permanently remove a snapshot. Deleting a snapshot is a real row
    delete (Section 29: simple Active/Deleted lifecycle, not the
    Archive→Restore pattern used elsewhere in Wealth) and must NEVER
    touch WealthAsset/WealthLiability records (Section 28/65) — this
    function only ever operates on the WealthSnapshot row itself.
    """
    if snapshot.user_id != user_id:
        return False, "You do not have permission to delete this snapshot."

    db.session.delete(snapshot)
    db.session.commit()
    return True, None


# ── Change Calculations ───────────────────────────────────────────────────────

def calculate_change(current_value, previous_value):
    """
    Absolute and percentage change between two Net Worth figures
    (Section 23/24). Returns {"absolute": float, "percentage": float
    or None}.

    percentage is None (never a divide-by-zero, never a fabricated
    number) whenever previous_value is zero or negative — Section 24
    explicitly forbids showing infinity or a misleading percentage
    off a zero/negative base; absolute change is always shown
    regardless.
    """
    if current_value is None or previous_value is None:
        return None

    absolute = current_value - previous_value
    if previous_value > 0:
        percentage = (absolute / previous_value) * 100
    else:
        percentage = None

    return {"absolute": absolute, "percentage": percentage}


def snapshot_change(snapshot, user_id):
    """
    Change vs. the snapshot immediately before this one by date, or
    None if this is the earliest (or only) snapshot for the user
    (Section 22 — insufficient-history state, no misleading number).
    """
    previous = get_previous_snapshot(user_id, snapshot.snapshot_date)
    if not previous:
        return None
    return calculate_change(snapshot.net_worth, previous.net_worth)


# ── Dashboard Integration (Section 42) ────────────────────────────────────────

def dashboard_trend_summary(user_id):
    """
    Everything the Wealth dashboard's 'Net Worth Trend' card needs,
    or None if fewer than 2 snapshots exist (Section 42: "Only show
    change information if at least two snapshots exist"). Consumes
    the exact same service calls as /wealth/history — no separate
    dashboard-only calculation (Section 43).
    """
    latest = latest_snapshot(user_id)
    if not latest:
        return None

    previous = get_previous_snapshot(user_id, latest.snapshot_date)
    if not previous:
        return None

    return {
        "latest": latest,
        "change": calculate_change(latest.net_worth, previous.net_worth),
    }


# ── Chart Data ─────────────────────────────────────────────────────────────────

def chart_data(snapshots):
    """
    Convert a list of snapshots (any order) into chronological
    (oldest-first) plain-dict points for Chart.js, matching the
    real-data-only rule (Section 18/20 — never fabricate missing
    points).
    """
    ordered = sorted(snapshots, key=lambda s: s.snapshot_date)
    return [
        {
            "date": s.snapshot_date.isoformat(),
            "net_worth": s.net_worth,
            "total_assets": s.total_asset_value,
            "total_liabilities": s.total_liability_value,
        }
        for s in ordered
    ]
