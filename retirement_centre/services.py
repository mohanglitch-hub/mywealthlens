"""
Retirement Centre — Services
===============================
Phase A: minimal statistics service to support the dashboard skeleton.
Phase B: Create/Update/Archive/Restore scheme business logic.
Phase C: Contribution history, balance snapshots, nominees, and
         maturity/target-retirement calculations.

Routes should always call into this layer rather than querying or
mutating models directly, matching insurance_centre's pattern.
"""

from datetime import datetime, date

from sqlalchemy import func

from .models import (
    RetirementScheme, RetirementContribution, RetirementBalanceSnapshot,
    RetirementSchemeNominee, RetirementDocument, RetirementTimeline,
    RetirementTimelineEvent, SchemeType, SchemeStatus, GrowthMethod,
)
from .utils import current_financial_year, financial_year_bounds


# ── Form Parsing Helpers ──────────────────────────────────────────────────────

def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _parse_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _scheme_fields_from_form(form):
    """
    Build a dict of RetirementScheme field values from a submitted form.
    Shared by create_scheme and update_scheme so Add and Edit can never
    drift apart (Section 3 of Phase B spec — one source of truth).
    """
    scheme_type = (form.get("scheme_type") or "").strip()

    fields = {
        "scheme_type":    scheme_type,
        "custom_type":    (form.get("custom_type") or "").strip() or None,
        "institution":    (form.get("institution") or "").strip() or None,
        "account_number": (form.get("account_number") or "").strip() or None,
        "opening_date":   _parse_date(form.get("opening_date")),

        "current_balance":     _parse_float(form.get("current_balance"), default=0) or 0,
        "balance_updated_at":  _parse_date(form.get("balance_updated_at")),

        "growth_method":              form.get("growth_method") or GrowthMethod.GOVERNMENT_DECLARED,
        "rate_or_return_assumption":  _parse_float(form.get("rate_or_return_assumption")),

        "status": form.get("status") or SchemeStatus.ACTIVE,
        "notes":  (form.get("notes") or "").strip() or None,

        "contribution_preference": form.get("contribution_preference") or None,

        # Category-specific — always parsed and stored regardless of
        # which scheme_type is currently selected. Nothing is silently
        # cleared just because a field is hidden in the UI for this
        # scheme type (Section 21 of Phase B spec — no accidental
        # data loss on scheme type change).
        "employer_name":  (form.get("employer_name") or "").strip() or None,
        "uan_number":      (form.get("uan_number") or "").strip() or None,
        "basic_salary":    _parse_float(form.get("basic_salary")),
        "employee_contribution_pct": _parse_float(form.get("employee_contribution_pct")),
        "employer_contribution_pct": _parse_float(form.get("employer_contribution_pct")),
        "target_retirement_year":    _parse_int(form.get("target_retirement_year")),

        "extension_opted": form.get("extension_opted") == "yes",

        "girl_child_name": (form.get("girl_child_name") or "").strip() or None,
        "girl_child_dob":  _parse_date(form.get("girl_child_dob")),

        "pran_number": (form.get("pran_number") or "").strip() or None,
        "tier":        (form.get("tier") or "").strip() or None,
    }
    return fields


def create_scheme(db, user_id, form):
    """
    Create a new retirement scheme for the given user.
    Returns (scheme, None) on success, (None, error_message) on failure.
    Assumes the form has already passed validators.validate_scheme().
    """
    fields = _scheme_fields_from_form(form)
    scheme = RetirementScheme(user_id=user_id, **fields)
    db.session.add(scheme)
    db.session.flush()  # get scheme.id before committing

    timeline = RetirementTimeline(
        scheme_id=scheme.id,
        user_id=user_id,
        event_type=RetirementTimelineEvent.CREATED,
        description=f"Scheme created ({scheme.display_type})",
    )
    db.session.add(timeline)
    db.session.commit()
    return scheme, None


def update_scheme(db, scheme, user_id, form):
    """
    Update an existing scheme, scoped to the owning user.
    Returns (scheme, None) on success, (None, error_message) on failure.
    """
    if scheme.user_id != user_id:
        return None, "You do not have permission to edit this scheme."

    old_balance = scheme.current_balance
    fields = _scheme_fields_from_form(form)
    for key, value in fields.items():
        setattr(scheme, key, value)
    scheme.updated_at = datetime.utcnow()

    balance_changed = fields["current_balance"] != old_balance
    event_type = (RetirementTimelineEvent.BALANCE_UPDATED if balance_changed
                  else RetirementTimelineEvent.UPDATED)
    description = (f"Balance updated to ₹{fields['current_balance']:,.0f}"
                    if balance_changed else "Scheme details updated")

    timeline = RetirementTimeline(
        scheme_id=scheme.id, user_id=user_id,
        event_type=event_type, description=description,
    )
    db.session.add(timeline)
    db.session.commit()
    return scheme, None


def archive_scheme(db, scheme, user_id):
    """Soft-delete a scheme — move to archive. Returns (success, error)."""
    if scheme.user_id != user_id:
        return False, "You do not have permission to archive this scheme."
    if scheme.is_archived:
        return False, "Scheme is already archived."

    scheme.is_archived = True
    scheme.archived_at = datetime.utcnow()

    timeline = RetirementTimeline(
        scheme_id=scheme.id, user_id=user_id,
        event_type=RetirementTimelineEvent.ARCHIVED,
        description="Scheme archived",
    )
    db.session.add(timeline)
    db.session.commit()
    return True, None


def restore_scheme(db, scheme, user_id):
    """Restore an archived scheme. Returns (success, error)."""
    if scheme.user_id != user_id:
        return False, "You do not have permission to restore this scheme."
    if not scheme.is_archived:
        return False, "Scheme is not archived."

    scheme.is_archived = False
    scheme.archived_at = None

    timeline = RetirementTimeline(
        scheme_id=scheme.id, user_id=user_id,
        event_type=RetirementTimelineEvent.RESTORED,
        description="Scheme restored",
    )
    db.session.add(timeline)
    db.session.commit()
    return True, None


class RetirementStatisticsService:
    """
    Computes dashboard summary numbers for one user.
    Never fabricates values — returns 0 / empty when there is no
    underlying data (Section 20 of spec: no fake statistics).
    """

    def __init__(self, user_id):
        self.user_id = user_id

    def _active_schemes_query(self):
        return RetirementScheme.query.filter_by(
            user_id=self.user_id, is_archived=False)

    def active_count(self):
        return self._active_schemes_query().count()

    def total_corpus(self):
        """Sum of current_balance across all active schemes."""
        schemes = self._active_schemes_query().all()
        return sum(s.current_balance or 0 for s in schemes)

    def current_fy_contributions(self):
        """Sum of contributions recorded in the current Indian FY."""
        start, end = financial_year_bounds(current_financial_year())
        total = (
            RetirementContribution.query
            .join(RetirementScheme,
                  RetirementContribution.scheme_id == RetirementScheme.id)
            .filter(
                RetirementScheme.user_id == self.user_id,
                RetirementContribution.contribution_date >= start,
                RetirementContribution.contribution_date <= end,
            )
            .with_entities(func.sum(RetirementContribution.amount))
            .scalar()
        )
        return total or 0

    def recent_schemes(self, limit=10):
        return (self._active_schemes_query()
                .order_by(RetirementScheme.created_at.desc())
                .limit(limit)
                .all())

    def upcoming_dates_count(self):
        """
        Count of active schemes whose maturity/target date is 'due' or
        'due_soon' (per compute_maturity_info below). Never fabricated —
        schemes with insufficient data to calculate a date are excluded,
        not guessed at (Section 29 of the Phase C spec).
        """
        count = 0
        for s in self._active_schemes_query().all():
            info = compute_maturity_info(s)
            if not info.get("available"):
                continue
            status = info.get("status")
            if status in ("due", "due_soon"):
                count += 1
        return count

    def category_breakdown(self):
        """
        Per-scheme-type summary: count, total current balance, and
        current-FY contributions — computed from real data, never
        fabricated (Section 16 of Phase D spec). Only includes scheme
        types the user actually has at least one active scheme for;
        the dashboard decides how to present the rest as "0 Schemes".
        """
        start, end = financial_year_bounds(current_financial_year())
        schemes = self._active_schemes_query().all()

        by_type = {}
        for s in schemes:
            by_type.setdefault(s.scheme_type, []).append(s)

        breakdown = []
        for scheme_type, group in by_type.items():
            scheme_ids = [s.id for s in group]
            total_balance = sum(s.current_balance or 0 for s in group)
            fy_total = (RetirementContribution.query
                        .filter(RetirementContribution.scheme_id.in_(scheme_ids),
                               RetirementContribution.contribution_date >= start,
                               RetirementContribution.contribution_date <= end)
                        .with_entities(func.sum(RetirementContribution.amount))
                        .scalar()) or 0
            breakdown.append({
                "scheme_type":              scheme_type,
                "count":                    len(group),
                "total_balance":            total_balance,
                "current_fy_contributions": fy_total,
            })

        # Alphabetical by scheme type, same ordering discipline as
        # SchemeType.ALL (Section 15 of spec).
        breakdown.sort(key=lambda b: b["scheme_type"])
        return breakdown

    def upcoming_milestones(self, limit=5):
        """
        The nearest maturity/target-retirement milestones across all
        active schemes, soonest first. Only includes schemes with
        enough data to actually calculate a date (Section 35 of spec).
        """
        from datetime import date as _date

        items = []
        for s in self._active_schemes_query().all():
            info = compute_maturity_info(s)
            if not info.get("available"):
                continue

            if info["kind"] == "maturity":
                sort_date = info["date"]
            elif info["kind"] == "ssy":
                sort_date = info["maturity_date"]
            elif info["kind"] == "target_year":
                sort_date = _date(info["year"], 1, 1)
            else:
                continue

            items.append({"scheme": s, "info": info, "sort_date": sort_date})

        items.sort(key=lambda i: i["sort_date"])
        return items[:limit]

    def summary_dict(self):
        """Everything the dashboard needs, in one call."""
        return {
            "total_corpus":             self.total_corpus(),
            "current_fy_contributions": self.current_fy_contributions(),
            "active_schemes":           self.active_count(),
            "upcoming_dates":           self.upcoming_dates_count(),
            "schemes":                  self.recent_schemes(),
            "category_breakdown":       self.category_breakdown(),
            "upcoming_milestones":      self.upcoming_milestones(),
        }


def get_scheme_with_related(scheme_id, user_id):
    """
    Fetch a scheme and its timeline for the detail page, scoped to
    the owning user. Returns None if not found or not owned.
    """
    scheme = RetirementScheme.query.filter_by(
        id=scheme_id, user_id=user_id).first()
    if not scheme:
        return None
    return {
        "scheme":   scheme,
        "timeline": scheme.timeline.limit(20).all(),
    }


# ── Form Parsing Helpers (Phase C) ────────────────────────────────────────────

def _parse_date_c(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_float_c(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _add_years(d, years):
    """Add N years to a date, safely handling 29 Feb on non-leap targets."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years)


# ── Contribution History (Phase C, Part 1) ───────────────────────────────────

def add_contribution(db, scheme, user_id, form):
    """Record a new contribution. Does NOT touch current_balance — a
    contribution is a historical transaction, current_balance is the
    user's latest known account value (Section 15 of Phase C spec)."""
    if scheme.user_id != user_id:
        return None, "You do not have permission to modify this scheme."

    c_date = _parse_date_c(form.get("contribution_date"))
    amount = _parse_float_c(form.get("amount"))
    if not c_date or amount is None or amount <= 0:
        return None, "Invalid contribution data."

    contribution = RetirementContribution(
        scheme_id=scheme.id, user_id=user_id,
        contribution_date=c_date, amount=amount,
        note=(form.get("note") or "").strip() or None,
    )
    db.session.add(contribution)

    timeline = RetirementTimeline(
        scheme_id=scheme.id, user_id=user_id,
        event_type=RetirementTimelineEvent.CONTRIBUTION_ADDED,
        description=f"Contribution of ₹{amount:,.0f} recorded",
    )
    db.session.add(timeline)
    db.session.commit()
    return contribution, None


def update_contribution(db, contribution, user_id, form):
    """Edit an existing contribution. Ownership checked via user_id
    stored directly on the contribution row."""
    if contribution.user_id != user_id:
        return None, "You do not have permission to modify this contribution."

    c_date = _parse_date_c(form.get("contribution_date"))
    amount = _parse_float_c(form.get("amount"))
    if not c_date or amount is None or amount <= 0:
        return None, "Invalid contribution data."

    contribution.contribution_date = c_date
    contribution.amount = amount
    contribution.note = (form.get("note") or "").strip() or None
    db.session.commit()
    return contribution, None


def delete_contribution(db, contribution, user_id):
    if contribution.user_id != user_id:
        return False, "You do not have permission to delete this contribution."
    db.session.delete(contribution)
    db.session.commit()
    return True, None


def get_contributions_for_scheme(scheme_id, fy_start_year=None):
    """Contributions for a scheme, newest first. Optionally filtered
    to one Financial Year."""
    q = RetirementContribution.query.filter_by(scheme_id=scheme_id)
    if fy_start_year is not None:
        start, end = financial_year_bounds(fy_start_year)
        q = q.filter(RetirementContribution.contribution_date >= start,
                     RetirementContribution.contribution_date <= end)
    return q.order_by(RetirementContribution.contribution_date.desc()).all()


def available_financial_years(scheme_id):
    """FY start years that actually have contributions, newest first —
    generated dynamically from real data, never hard-coded (Section 13
    of Phase C spec)."""
    rows = (RetirementContribution.query
            .filter_by(scheme_id=scheme_id)
            .with_entities(RetirementContribution.contribution_date)
            .all())
    years = {(d.year if d.month >= 4 else d.year - 1) for (d,) in rows}
    return sorted(years, reverse=True)


def contribution_summary(scheme_id):
    """Current-FY total, all-time total, and count — computed directly
    from RetirementContribution rows, never from current_balance,
    interest, or projections (Section 11/12 of spec)."""
    start, end = financial_year_bounds(current_financial_year())

    total_recorded = (RetirementContribution.query
                       .filter_by(scheme_id=scheme_id)
                       .with_entities(func.sum(RetirementContribution.amount))
                       .scalar()) or 0

    current_fy_total = (RetirementContribution.query
                        .filter_by(scheme_id=scheme_id)
                        .filter(RetirementContribution.contribution_date >= start,
                               RetirementContribution.contribution_date <= end)
                        .with_entities(func.sum(RetirementContribution.amount))
                        .scalar()) or 0

    count = RetirementContribution.query.filter_by(scheme_id=scheme_id).count()

    return {
        "current_fy_total": current_fy_total,
        "total_recorded":   total_recorded,
        "count":             count,
    }


# ── Balance Snapshots (Phase C, Part 3) ───────────────────────────────────────

def update_balance(db, scheme, user_id, form):
    """
    Record a new balance: creates a RetirementBalanceSnapshot (preserving
    history) AND updates the scheme's current_balance/balance_updated_at.
    The previous balance is never destroyed — it lives on in the
    snapshot history (Section 17 of spec).
    """
    if scheme.user_id != user_id:
        return None, "You do not have permission to modify this scheme."

    new_balance  = _parse_float_c(form.get("new_balance"))
    balance_date = _parse_date_c(form.get("balance_date"))
    note = (form.get("balance_note") or "").strip() or None

    if new_balance is None or new_balance < 0:
        return None, "Please enter a valid, non-negative balance."
    if not balance_date:
        return None, "Please enter a valid balance date."

    snapshot = RetirementBalanceSnapshot(
        scheme_id=scheme.id, balance=new_balance,
        balance_date=balance_date, note=note,
    )
    db.session.add(snapshot)

    scheme.current_balance    = new_balance
    scheme.balance_updated_at = balance_date
    scheme.updated_at         = datetime.utcnow()

    timeline = RetirementTimeline(
        scheme_id=scheme.id, user_id=user_id,
        event_type=RetirementTimelineEvent.BALANCE_UPDATED,
        description=f"Balance updated to ₹{new_balance:,.0f}",
    )
    db.session.add(timeline)
    db.session.commit()
    return snapshot, None


def get_balance_history(scheme_id):
    return (RetirementBalanceSnapshot.query
            .filter_by(scheme_id=scheme_id)
            .order_by(RetirementBalanceSnapshot.balance_date.desc())
            .all())


# ── Nominees (Phase C, Part 4) ─────────────────────────────────────────────────

def add_nominee(db, scheme, user_id, form):
    """
    Add a nominee. Percentage validation rejects any total that would
    exceed 100% across the scheme's nominees, but does NOT require
    hitting exactly 100% on every single add — that would make it
    impossible to add nominees one at a time. See the note in the
    chat response accompanying this phase for the full reasoning.
    """
    if scheme.user_id != user_id:
        return None, "You do not have permission to modify this scheme."

    name = (form.get("name") or "").strip()
    relationship = (form.get("relationship") or "").strip()
    pct = _parse_float_c(form.get("percentage"))

    if not name or not relationship:
        return None, "Nominee name and relationship are required."
    if pct is not None and (pct < 0 or pct > 100):
        return None, "Percentage must be between 0 and 100."
    if pct is not None:
        existing_total = sum(n.percentage or 0 for n in scheme.nominees.all())
        if existing_total + pct > 100.001:
            return None, (f"Total nominee allocation would exceed 100% "
                          f"(currently {existing_total:g}% allocated).")

    nominee = RetirementSchemeNominee(
        scheme_id=scheme.id, user_id=user_id,
        name=name, relationship=relationship, percentage=pct,
        contact=(form.get("contact") or "").strip() or None,
        notes=(form.get("notes") or "").strip() or None,
    )
    db.session.add(nominee)
    db.session.commit()
    return nominee, None


def update_nominee(db, nominee, user_id, form):
    if nominee.user_id != user_id:
        return None, "You do not have permission to modify this nominee."

    name = (form.get("name") or "").strip()
    relationship = (form.get("relationship") or "").strip()
    pct = _parse_float_c(form.get("percentage"))

    if not name or not relationship:
        return None, "Nominee name and relationship are required."
    if pct is not None and (pct < 0 or pct > 100):
        return None, "Percentage must be between 0 and 100."
    if pct is not None:
        others_total = sum(n.percentage or 0 for n in nominee.scheme.nominees.all()
                           if n.id != nominee.id)
        if others_total + pct > 100.001:
            return None, (f"Total nominee allocation would exceed 100% "
                          f"(other nominees already total {others_total:g}%).")

    nominee.name = name
    nominee.relationship = relationship
    nominee.percentage = pct
    nominee.contact = (form.get("contact") or "").strip() or None
    nominee.notes = (form.get("notes") or "").strip() or None
    db.session.commit()
    return nominee, None


def delete_nominee(db, nominee, user_id):
    if nominee.user_id != user_id:
        return False, "You do not have permission to delete this nominee."
    db.session.delete(nominee)
    db.session.commit()
    return True, None


# ── Maturity / Target-Retirement Calculations (Phase C, Part 5) ──────────────

def _date_status(days):
    """
    Classifies a date's distance from today. Never a negative countdown
    for a passed date — shows 'reached' instead (Section 28 of spec).
    """
    if days < 0:
        return "reached"
    if days <= 30:
        return "due"
    if days <= 365:
        return "due_soon"
    return "future"


def compute_maturity_info(scheme):
    """
    Returns a dict describing maturity/target-retirement info for a
    scheme. `available=False` (with an explanatory `message`) when
    required source data is missing — never guessed at.

    All values are informational/assumption-based, never presented as
    guaranteed outcomes (Section 36 of spec — no financial advice).
    """
    today = date.today()

    if scheme.scheme_type == SchemeType.PPF:
        if not scheme.opening_date:
            return {"available": False,
                    "message": "Add an opening date to calculate PPF maturity."}
        years = 15 + (5 if scheme.extension_opted else 0)
        maturity_date = _add_years(scheme.opening_date, years)
        days = (maturity_date - today).days
        return {
            "available": True, "kind": "maturity",
            "label": "Maturity Date", "date": maturity_date,
            "status": _date_status(days), "days": days,
        }

    if scheme.scheme_type == SchemeType.SSY:
        if not scheme.opening_date or not scheme.girl_child_dob:
            return {"available": False,
                    "message": "Add the opening date and the girl child's date "
                               "of birth to calculate SSY milestones."}
        contribution_end     = _add_years(scheme.opening_date, 15)
        partial_withdrawal   = _add_years(scheme.girl_child_dob, 18)
        maturity_date        = _add_years(scheme.girl_child_dob, 21)
        return {
            "available": True, "kind": "ssy",
            "contribution_period_end": contribution_end,
            "partial_withdrawal_date": partial_withdrawal,
            "maturity_date":           maturity_date,
            "status": _date_status((maturity_date - today).days),
        }

    if scheme.scheme_type in (SchemeType.EPF, SchemeType.VPF,
                              SchemeType.NPS, SchemeType.SUPERANNUATION):
        if not scheme.target_retirement_year:
            return {"available": False,
                    "message": "Add a target retirement year to see your "
                               "retirement timeline."}
        years_remaining = scheme.target_retirement_year - today.year
        status = ("reached" if years_remaining <= 0
                  else "due_soon" if years_remaining <= 1
                  else "future")
        return {
            "available": True, "kind": "target_year",
            "label": "Target Retirement",
            "year": scheme.target_retirement_year,
            "years_remaining": years_remaining,
            "status": status,
        }

    return {"available": False, "message": None}


# ── Documents (Phase D) ────────────────────────────────────────────────────────

def get_documents_for_scheme(scheme_id):
    return (RetirementDocument.query
            .filter_by(scheme_id=scheme_id)
            .order_by(RetirementDocument.uploaded_at.desc())
            .all())


def save_document_metadata(db, scheme, user_id, doc_type, original_name,
                            stored_name, file_path, file_size, notes=None):
    """Persist a document's metadata after the file itself has already
    been saved to local disk by utils.save_document_file()."""
    doc = RetirementDocument(
        scheme_id=scheme.id, user_id=user_id, doc_type=doc_type,
        original_name=original_name, stored_name=stored_name,
        file_path=file_path, file_size=file_size, notes=notes,
    )
    db.session.add(doc)

    timeline = RetirementTimeline(
        scheme_id=scheme.id, user_id=user_id,
        event_type=RetirementTimelineEvent.DOCUMENT_UPLOADED,
        description=f"Document uploaded: {original_name}",
    )
    db.session.add(timeline)
    db.session.commit()
    return doc


def delete_document(db, doc, user_id):
    """
    Delete document metadata (and its file, handled by the route before
    calling this). Ownership must be checked by the caller before this
    is reached — this function trusts the caller already verified
    doc.user_id == user_id, matching the pattern used elsewhere in
    this file.
    """
    if doc.user_id != user_id:
        return False, "You do not have permission to delete this document."
    db.session.delete(doc)
    db.session.commit()
    return True, None
