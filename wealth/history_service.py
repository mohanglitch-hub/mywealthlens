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

from .models import WealthSnapshot, WealthSnapshotLog, SnapshotSource
from .services import WealthStatisticsService
from .timezone_utils import today_ist


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


def create_snapshot(db, user_id, snapshot_date, confirm_replace=False,
                    source=SnapshotSource.MANUAL):
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

    source (Phase I, Section 34/79): SnapshotSource.MANUAL or
    .AUTOMATIC — who/what created this row. Defaults to MANUAL so
    every pre-Phase-I caller (the /history/create route) keeps
    working unchanged without passing it explicitly, though the
    route now does pass it explicitly for clarity. The automatic CLI
    (Section 5) is the only caller that ever passes AUTOMATIC, and it
    NEVER passes confirm_replace=True (Section 14/22 — an automatic
    run must skip on duplicate, never overwrite an existing manual OR
    automatic snapshot).
    """
    existing = get_snapshot_by_date(user_id, snapshot_date)

    if existing and not confirm_replace:
        return None, None, True

    fields = _current_wealth_fields(user_id)

    if existing:
        # Replace in place — explicit confirmation already given by
        # the caller (Section 11). Same row, values overwritten.
        # source is intentionally NOT overwritten here: a manual
        # replace of an automatic snapshot (or vice versa) keeps
        # whatever source the row already had, since only the
        # financial VALUES are being corrected, not who originally
        # captured the date (this path is never reached by the
        # automatic CLI regardless — see docstring above).
        for key, value in fields.items():
            setattr(existing, key, value)
        db.session.commit()
        return existing, None, False

    snapshot = WealthSnapshot(user_id=user_id, snapshot_date=snapshot_date,
                              source=source, **fields)
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


# ── Automatic Snapshot Run (Phase I) ──────────────────────────────────────────

def _log(db, user_id, snapshot_date, status, message, snapshot_id=None):
    """
    Write one WealthSnapshotLog row (Section 28/29). Logging failure
    must never take down a run that otherwise succeeded (Section 62)
    — this function swallows its own exceptions rather than letting a
    logging problem look like a snapshot problem to the caller. The
    snapshot itself (if any) has already been committed by the time
    this runs, so a logging failure here cannot lose Wealth data,
    only an operational log line.
    """
    try:
        entry = WealthSnapshotLog(
            user_id=user_id, snapshot_date=snapshot_date,
            status=status, message=message, snapshot_id=snapshot_id)
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()


def run_automatic_snapshot_run(db, dry_run=False):
    """
    The single entry point for automatic Wealth snapshots (Section 5
    of the Phase I spec). Called by the `flask wealth snapshot` CLI
    command — this function contains ALL of the actual logic; the
    CLI layer (wealth/cli.py) is a thin wrapper that only handles
    argument parsing and printing (Section 38: "CLI -> Service ->
    Models", never "CLI -> duplicated calculations -> database").

    For each user, independently (Section 24/25):
      - if a snapshot already exists for today (IST): SKIPPED
      - else, calculate current Wealth and create one: SUCCESS
      - if calculation or creation raises: roll back that user's
        work only, log FAILED, continue to the next user (Section 58)

    dry_run=True (Section 87): determines what WOULD happen for each
    user, performs NO database writes at all — no snapshot created,
    no log row written, since a log row is itself a write. Safe to
    run anytime without side effects, for manual verification.

    Returns a summary dict:
      {"date": date, "processed": int, "created": int,
       "skipped": int, "failed": int, "details": [(user_id, status,
       message), ...]}
    Never includes financial figures (Section 30/39) — only user_id,
    status, and a short operational message.
    """
    from models import User  # local import: root models.py -> User,
    # deferred to avoid any import-order issue between app.py's own
    # startup sequence and this module (wealth/history_service.py is
    # imported very early, via wealth/routes.py, before app.py has
    # finished defining everything at module scope).

    today = today_ist()
    users = User.query.all()  # Section 23: no soft-delete/status
    # concept exists anywhere on User in this codebase (confirmed by
    # audit) — every row in `user` is an authenticated, real account,
    # so "all users" IS "all eligible users". Documented here as the
    # explicit decision this section of the spec asks for.

    summary = {"date": today, "processed": 0, "created": 0,
              "skipped": 0, "failed": 0, "details": []}

    for user in users:
        summary["processed"] += 1
        try:
            existing = get_snapshot_by_date(user.id, today)

            if existing:
                summary["skipped"] += 1
                summary["details"].append((user.id, "SKIPPED", "snapshot already exists"))
                if not dry_run:
                    _log(db, user.id, today, WealthSnapshotLog.STATUS_SKIPPED,
                        "snapshot already exists", snapshot_id=existing.id)
                continue

            if dry_run:
                # Would create — but Section 87 forbids any write here.
                summary["created"] += 1
                summary["details"].append((user.id, "WOULD_CREATE", "no snapshot for today"))
                continue

            # create_snapshot() only ever hits this branch (no
            # `existing` above), so confirm_replace is irrelevant —
            # passed as False regardless, matching Section 14/22
            # (the automatic path must never overwrite anything).
            snapshot, error, needs_confirmation = create_snapshot(
                db, user.id, today, confirm_replace=False,
                source=SnapshotSource.AUTOMATIC)

            if error or needs_confirmation or not snapshot:
                # needs_confirmation=True shouldn't be reachable here
                # (we already checked `existing` above), but treated
                # as a safe FAILED rather than assumed-impossible, in
                # case of a race — see the concurrency note below.
                raise RuntimeError(error or "snapshot creation returned no result")

            summary["created"] += 1
            summary["details"].append((user.id, "SUCCESS", "snapshot created"))
            _log(db, user.id, today, WealthSnapshotLog.STATUS_SUCCESS,
                "snapshot created", snapshot_id=snapshot.id)

        except Exception as exc:
            # Section 25/26: roll back THIS user's partial work only,
            # log FAILED, move on — one user's failure must never
            # stop the run or leave a half-written row.
            db.session.rollback()

            # Section 27/46: two near-simultaneous runs both pass the
            # `existing` check above (neither sees the other's
            # not-yet-committed row), then both attempt to INSERT —
            # the database's UNIQUE(user_id, snapshot_date) constraint
            # (Section 13/78) rejects the second one, which lands
            # here as an IntegrityError. That is the expected,
            # correct outcome of a race, not a real failure — report
            # it as SKIPPED rather than FAILED so an operator reading
            # the log isn't alarmed by something the system was
            # explicitly designed to handle gracefully.
            is_duplicate_race = "UNIQUE" in str(exc).upper() or "unique" in str(exc).lower()
            if is_duplicate_race:
                summary["skipped"] += 1
                summary["details"].append((user.id, "SKIPPED", "duplicate prevented"))
                _log(db, user.id, today, WealthSnapshotLog.STATUS_SKIPPED,
                    "duplicate prevented")
            else:
                summary["failed"] += 1
                # Section 30: never log exc's raw message if it could
                # contain a value — SQLAlchemy/DB errors don't carry
                # financial figures here (this only wraps calculation
                # + row-creation, not user-entered strings), but kept
                # generic regardless as the safer default.
                summary["details"].append((user.id, "FAILED", "snapshot creation error"))
                _log(db, user.id, today, WealthSnapshotLog.STATUS_FAILED,
                    "snapshot creation error")

    return summary


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
