"""
Wealth — Services
====================
Phase A: minimal statistics service for the dashboard skeleton.
Phase B: full Asset CRUD, archive/restore/delete, and server-side
         filtered/sorted listing.

Routes should always call into this layer rather than querying or
mutating models directly, matching the pattern already established
in insurance_centre and retirement_centre.
"""

from datetime import datetime

from sqlalchemy import or_

from .models import (
    WealthAsset, WealthLiability, WealthStatus, SourceType,
    WealthAssetCategory, OwnershipType,
)


# ── Form Parsing Helpers ──────────────────────────────────────────────────────

def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _asset_fields_from_form(form):
    """
    Build a dict of WealthAsset field values from a submitted form.
    Shared by create_asset and update_asset so Add and Edit can never
    drift apart (Section 31 of spec — one source of truth for the
    shared form).
    """
    return {
        "name":        (form.get("name") or "").strip(),
        "category":    (form.get("category") or "").strip(),
        "asset_type":  (form.get("asset_type") or "").strip() or None,
        "description": (form.get("description") or "").strip() or None,

        "current_value": _parse_float(form.get("current_value")) or 0,
        "value_as_of":   _parse_date(form.get("value_as_of")),

        "ownership_type":       form.get("ownership_type") or "Sole",
        "ownership_percentage": _parse_float(form.get("ownership_percentage")),

        "source_type":                (form.get("source_type") or "Self Acquired"),
        "original_owner":              (form.get("original_owner") or "").strip() or None,
        "original_owner_relationship": (form.get("original_owner_relationship") or "").strip() or None,
        "date_received":               _parse_date(form.get("date_received")),

        "acquisition_date":  _parse_date(form.get("acquisition_date")),
        "acquisition_value": _parse_float(form.get("acquisition_value")),

        # Category-specific — always parsed and stored regardless of
        # which category is currently selected, so switching category
        # in the UI never silently discards previously entered data
        # (same principle established in retirement_centre's form).
        "property_type":    (form.get("property_type") or "").strip() or None,
        "property_address": (form.get("property_address") or "").strip() or None,
        "city":              (form.get("city") or "").strip() or None,
        "state":             (form.get("state") or "").strip() or None,
        "area":              _parse_float(form.get("area")),
        "area_unit":         (form.get("area_unit") or "").strip() or None,

        "metal_type":  (form.get("metal_type") or "").strip() or None,
        "weight":       _parse_float(form.get("weight")),
        "weight_unit":  (form.get("weight_unit") or "").strip() or None,

        "vehicle_type":         (form.get("vehicle_type") or "").strip() or None,
        "registration_number":  (form.get("registration_number") or "").strip() or None,

        "institution":       (form.get("institution") or "").strip() or None,
        "account_reference": (form.get("account_reference") or "").strip() or None,
        "deposit_type":       (form.get("deposit_type") or "").strip() or None,
        "interest_rate":       _parse_float(form.get("interest_rate")),
        "maturity_date":        _parse_date(form.get("maturity_date")),
        "investment_type":      (form.get("investment_type") or "").strip() or None,

        "status": form.get("status") or WealthStatus.ACTIVE,
        "notes":  (form.get("notes") or "").strip() or None,
    }


# ── Asset CRUD (Phase B) ──────────────────────────────────────────────────────

def create_asset(db, user_id, form):
    """Create a new Wealth asset. Assumes the form already passed
    validators.validate_wealth_asset()."""
    fields = _asset_fields_from_form(form)
    if fields["ownership_percentage"] is None:
        fields["ownership_percentage"] = 100.0

    asset = WealthAsset(user_id=user_id, **fields)
    db.session.add(asset)
    db.session.commit()
    return asset, None


def update_asset(db, asset, user_id, form):
    """Update an existing asset, scoped to the owning user."""
    if asset.user_id != user_id:
        return None, "You do not have permission to edit this asset."

    fields = _asset_fields_from_form(form)
    if fields["ownership_percentage"] is None:
        fields["ownership_percentage"] = 100.0

    for key, value in fields.items():
        setattr(asset, key, value)
    asset.updated_at = datetime.utcnow()

    db.session.commit()
    return asset, None


def archive_asset(db, asset, user_id):
    """Soft-delete — move to archive. Returns (success, error)."""
    if asset.user_id != user_id:
        return False, "You do not have permission to archive this asset."
    if asset.is_archived:
        return False, "Asset is already archived."

    asset.is_archived = True
    asset.status = WealthStatus.ARCHIVED
    asset.archived_at = datetime.utcnow()
    db.session.commit()
    return True, None


def restore_asset(db, asset, user_id):
    """Restore an archived asset. Returns (success, error)."""
    if asset.user_id != user_id:
        return False, "You do not have permission to restore this asset."
    if not asset.is_archived:
        return False, "Asset is not archived."

    asset.is_archived = False
    asset.status = WealthStatus.ACTIVE
    asset.archived_at = None
    db.session.commit()
    return True, None


def delete_asset_permanent(db, asset, user_id):
    """
    Permanently delete an asset. Only allowed if already archived
    (Section 14/28 of spec — Active -> Archive -> Delete, never a
    direct Active -> Delete path).
    """
    if asset.user_id != user_id:
        return False, "You do not have permission to delete this asset."
    if not asset.is_archived:
        return False, "Only archived assets can be permanently deleted."

    db.session.delete(asset)
    db.session.commit()
    return True, None


def get_asset_or_none(asset_id, user_id):
    """Fetch a single asset, scoped to the owning user. None if not
    found or not owned — routes turn this into a 404."""
    return WealthAsset.query.filter_by(id=asset_id, user_id=user_id).first()


def get_assets_for_listing(user_id, q=None, category=None, status_filter="active",
                           ownership=None, source=None, sort_by="newest"):
    """
    Server-side filtered and sorted asset listing (Section 15/16 of
    spec — filtering and sorting happen in the database query, not
    by loading everything into memory and sorting in Python).
    """
    query = WealthAsset.query.filter_by(user_id=user_id)
    query = query.filter_by(is_archived=(status_filter == "archived"))

    if category:
        query = query.filter(WealthAsset.category == category)
    if ownership:
        query = query.filter(WealthAsset.ownership_type == ownership)
    if source:
        query = query.filter(WealthAsset.source_type == source)

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            WealthAsset.name.ilike(like),
            WealthAsset.asset_type.ilike(like),
            WealthAsset.category.ilike(like),
            WealthAsset.institution.ilike(like),
            WealthAsset.description.ilike(like),
        ))

    sort_map = {
        "newest":     WealthAsset.created_at.desc(),
        "oldest":     WealthAsset.created_at.asc(),
        "value_high": WealthAsset.current_value.desc(),
        "value_low":  WealthAsset.current_value.asc(),
        "name_az":    WealthAsset.name.asc(),
        "name_za":    WealthAsset.name.desc(),
    }
    query = query.order_by(sort_map.get(sort_by, WealthAsset.created_at.desc()))

    return query.all()


# ── Dashboard Statistics ──────────────────────────────────────────────────────

class WealthStatisticsService:
    """
    Computes dashboard summary numbers for one user. Never fabricates
    values — returns 0 when there is no underlying data (Section 6
    of Phase A spec, still true in Phase B).
    """

    def __init__(self, user_id):
        self.user_id = user_id

    def _active_assets_query(self):
        return WealthAsset.query.filter_by(
            user_id=self.user_id, is_archived=False)

    def _archived_assets_query(self):
        return WealthAsset.query.filter_by(
            user_id=self.user_id, is_archived=True)

    def _active_liabilities_query(self):
        return WealthLiability.query.filter_by(
            user_id=self.user_id, is_archived=False)

    def total_assets(self):
        return sum(a.current_value or 0
                  for a in self._active_assets_query().all())

    def total_liabilities(self):
        return sum(l.outstanding_amount or 0
                  for l in self._active_liabilities_query().all())

    def net_worth(self):
        """
        Phase A/B — a straightforward Total Assets minus Total
        Liabilities. Section 20 of the Phase A spec explicitly defers
        ownership-adjusted values to a later Net Worth phase — this
        stays deliberately simple.
        """
        return self.total_assets() - self.total_liabilities()

    def asset_count(self):
        return self._active_assets_query().count()

    def archived_asset_count(self):
        return self._archived_assets_query().count()

    def liability_count(self):
        return self._active_liabilities_query().count()

    def inherited_family_asset_count(self):
        """Count of active assets classified as inherited/family-
        related (Section 3 of Phase B spec)."""
        return sum(1 for a in self._active_assets_query().all()
                  if a.is_family_or_inherited)

    def category_breakdown(self):
        """
        Per-category totals for active assets — now includes the
        attributable (ownership-adjusted) total alongside the gross
        total (Section 16 of Phase D spec). This EXTENDS the existing
        Phase B method rather than duplicating it — the dashboard's
        existing loop only reads .category/.total and is unaffected
        by the new .attributable key.
        """
        assets = self._active_assets_query().all()
        totals = {}
        for a in assets:
            if a.category not in totals:
                totals[a.category] = {"total": 0, "attributable": 0}
            totals[a.category]["total"] += (a.current_value or 0)
            totals[a.category]["attributable"] += a.attributable_value
        breakdown = [{"category": c, "total": v["total"], "attributable": v["attributable"]}
                    for c, v in totals.items()]
        breakdown.sort(key=lambda b: b["total"], reverse=True)
        return breakdown

    def recent_assets(self, limit=5):
        """Most recently added/updated active assets (Section 23 of
        spec)."""
        return (self._active_assets_query()
                .order_by(WealthAsset.updated_at.desc())
                .limit(limit)
                .all())

    def archived_liability_count(self):
        return WealthLiability.query.filter_by(
            user_id=self.user_id, is_archived=True).count()

    def attributable_liabilities_total(self):
        """Section 42 of Phase C spec — sum of ownership-adjusted
        liability amounts, shown ALONGSIDE (never replacing) the raw
        total outstanding."""
        return sum(l.attributable_liability
                  for l in self._active_liabilities_query().all())

    def attributable_assets_total(self):
        """
        Sum of ownership-adjusted asset values — the asset-side
        counterpart to attributable_liabilities_total() above.
        Missing before Phase D; this is the one genuinely new
        aggregate the audit found absent.
        """
        return sum(a.attributable_value
                  for a in self._active_assets_query().all())

    def attributable_net_worth(self):
        """
        My Attributable Net Worth = My Attributable Assets − My
        Attributable Liabilities (Section 6 of Phase D spec) — the
        PRIMARY ownership-aware Net Worth figure, distinct from the
        gross net_worth() above. Neither replaces the other; both are
        shown.
        """
        return self.attributable_assets_total() - self.attributable_liabilities_total()

    def has_any_wealth_data(self):
        """
        True if the user has ANY active asset or liability. Used to
        distinguish a genuine 'no data yet' state from a real,
        calculated ₹0 net worth (Section 21 of spec — these are NOT
        the same thing and must not be displayed the same way).
        """
        return self.asset_count() > 0 or self.liability_count() > 0

    # ── Family & Inherited Wealth (Phase E) ──────────────────────
    # Qualifying sources: Inherited, Gifted/Transferred, Family
    # Owned, Joint Family Asset. Self Acquired and Other are
    # deliberately excluded — this list is the single source of
    # truth, used identically by every method below and by
    # WealthAsset.is_family_or_inherited (fixed alongside this phase
    # to match — see models.py).

    _QUALIFYING_FAMILY_SOURCES = (
        SourceType.INHERITED, SourceType.GIFTED,
        SourceType.FAMILY_OWNED, SourceType.JOINT_FAMILY,
    )

    def _qualifying_family_assets_query(self):
        return self._active_assets_query().filter(
            WealthAsset.source_type.in_(self._QUALIFYING_FAMILY_SOURCES))

    def family_asset_count(self):
        return self._qualifying_family_assets_query().count()

    def inherited_asset_count(self):
        return self._active_assets_query().filter(
            WealthAsset.source_type == SourceType.INHERITED).count()

    def family_owned_asset_count(self):
        return self._active_assets_query().filter(
            WealthAsset.source_type == SourceType.FAMILY_OWNED).count()

    def gifted_asset_count(self):
        return self._active_assets_query().filter(
            WealthAsset.source_type == SourceType.GIFTED).count()

    def total_family_asset_value(self):
        """Total value of qualifying family/inherited assets — never
        the attributable value (Section 12: these are distinct)."""
        return sum(a.current_value or 0
                  for a in self._qualifying_family_assets_query().all())

    def attributable_family_asset_value(self):
        """Ownership-adjusted sum — reuses WealthAsset.attributable_value,
        the exact same calculation Net Worth uses (Section 25/37:
        one ownership formula, never a second one)."""
        return sum(a.attributable_value
                  for a in self._qualifying_family_assets_query().all())

    def family_asset_category_breakdown(self):
        assets = self._qualifying_family_assets_query().all()
        totals = {}
        for a in assets:
            if a.category not in totals:
                totals[a.category] = {"total": 0, "attributable": 0}
            totals[a.category]["total"] += (a.current_value or 0)
            totals[a.category]["attributable"] += a.attributable_value
        breakdown = [{"category": c, "total": v["total"], "attributable": v["attributable"]}
                    for c, v in totals.items()]
        breakdown.sort(key=lambda b: b["total"], reverse=True)
        return breakdown

    def get_family_assets(self, source=None, category=None, ownership=None,
                          q=None, status_filter="active"):
        """
        Listing query for the Family & Inherited Wealth page.
        Database-level filtering throughout (Section 61). When no
        specific `source` filter is chosen, defaults to all
        qualifying family sources — never Self Acquired/Other.
        """
        query = WealthAsset.query.filter_by(user_id=self.user_id)
        query = query.filter_by(is_archived=(status_filter == "archived"))

        if source:
            query = query.filter(WealthAsset.source_type == source)
        else:
            query = query.filter(WealthAsset.source_type.in_(self._QUALIFYING_FAMILY_SOURCES))

        if category:
            query = query.filter(WealthAsset.category == category)
        if ownership:
            query = query.filter(WealthAsset.ownership_type == ownership)

        if q:
            like = f"%{q}%"
            query = query.filter(or_(
                WealthAsset.name.ilike(like),
                WealthAsset.asset_type.ilike(like),
                WealthAsset.category.ilike(like),
                WealthAsset.original_owner.ilike(like),
                WealthAsset.description.ilike(like),
            ))

        return query.order_by(WealthAsset.created_at.desc()).all()

    # ── Family-Related Liabilities (Phase E) ─────────────────────
    # A liability counts as family-related when its liability_type
    # is specifically "Family Loan" — not the whole "Family /
    # Informal Debt" category, which also contains "Friend /
    # Informal Loan" and "Other Informal Debt". See Phase E's final
    # report for the full reasoning: this uses existing structured
    # data, adds zero new fields, and correctly excludes non-family
    # informal debt.

    _FAMILY_LIABILITY_TYPE = "Family Loan"

    def _family_liabilities_query(self):
        return self._active_liabilities_query().filter(
            WealthLiability.liability_type == self._FAMILY_LIABILITY_TYPE)

    def family_liability_count(self):
        return self._family_liabilities_query().count()

    def total_family_liability_value(self):
        return sum(l.outstanding_amount or 0
                  for l in self._family_liabilities_query().all())

    def attributable_family_liability_value(self):
        return sum(l.attributable_liability
                  for l in self._family_liabilities_query().all())

    def liability_category_breakdown(self):
        """
        Per-category outstanding totals for active liabilities — now
        includes the attributable total alongside the gross total
        (Section 17 of Phase D spec), extending the existing Phase C
        method rather than duplicating it.
        """
        liabilities = self._active_liabilities_query().all()
        totals = {}
        for l in liabilities:
            if l.category not in totals:
                totals[l.category] = {"total": 0, "attributable": 0}
            totals[l.category]["total"] += (l.outstanding_amount or 0)
            totals[l.category]["attributable"] += l.attributable_liability
        breakdown = [{"category": c, "total": v["total"], "attributable": v["attributable"]}
                    for c, v in totals.items()]
        breakdown.sort(key=lambda b: b["total"], reverse=True)
        return breakdown

    def recent_liabilities(self, limit=5):
        """Most recently added/updated ACTIVE liabilities only
        (Section 43 of spec — never shows archived ones)."""
        return (self._active_liabilities_query()
                .order_by(WealthLiability.updated_at.desc())
                .limit(limit)
                .all())

    def summary_dict(self):
        """Everything the Wealth dashboard AND the Net Worth page
        need, in one call — this is the single authoritative call
        both pages use, so they can never diverge (Section 24 of
        Phase D spec). Now also includes Family Wealth totals for
        the exact same reason (Section 32 of Phase E spec)."""
        return {
            "total_assets":               self.total_assets(),
            "total_liabilities":          self.total_liabilities(),
            "net_worth":                  self.net_worth(),
            "attributable_assets":        self.attributable_assets_total(),
            "attributable_net_worth":     self.attributable_net_worth(),
            "has_wealth_data":            self.has_any_wealth_data(),
            "asset_count":                self.asset_count(),
            "archived_asset_count":       self.archived_asset_count(),
            "liability_count":            self.liability_count(),
            "archived_liability_count":   self.archived_liability_count(),
            "attributable_liabilities":   self.attributable_liabilities_total(),
            "inherited_family_count":     self.inherited_family_asset_count(),
            "category_breakdown":         self.category_breakdown(),
            "liability_category_breakdown": self.liability_category_breakdown(),
            "recent_assets":              self.recent_assets(),
            "recent_liabilities":         self.recent_liabilities(),
            "family_asset_count":         self.family_asset_count(),
            "total_family_value":         self.total_family_asset_value(),
            "attributable_family_value":  self.attributable_family_asset_value(),
        }


# ── Liability CRUD (Phase C) ──────────────────────────────────────────────────

def _liability_fields_from_form(form):
    """
    Build a dict of WealthLiability field values from a submitted
    form. Shared by create_liability and update_liability so Add and
    Edit can never drift apart — same discipline as
    _asset_fields_from_form above (Section 66 of spec: single source
    of truth for the shared form).
    """
    return {
        "name":            (form.get("name") or "").strip(),
        "category":        (form.get("category") or "").strip(),
        "liability_type":  (form.get("liability_type") or "").strip() or None,
        "description":     (form.get("description") or "").strip() or None,

        "lender":            (form.get("lender") or "").strip() or None,
        "account_reference": (form.get("account_reference") or "").strip() or None,

        "original_amount":    _parse_float(form.get("original_amount")) or 0,
        "outstanding_amount": _parse_float(form.get("outstanding_amount")) or 0,
        "interest_rate":       _parse_float(form.get("interest_rate")),

        "ownership_type":       form.get("ownership_type") or "Sole",
        "ownership_percentage": _parse_float(form.get("ownership_percentage")),

        "start_date":         _parse_date(form.get("start_date")),
        "expected_end_date":  _parse_date(form.get("expected_end_date")),

        "status": form.get("status") or WealthStatus.ACTIVE,
        "notes":  (form.get("notes") or "").strip() or None,
    }


def create_liability(db, user_id, form):
    """Create a new Wealth liability. Assumes the form already passed
    validators.validate_wealth_liability()."""
    fields = _liability_fields_from_form(form)
    if fields["ownership_percentage"] is None:
        fields["ownership_percentage"] = 100.0

    liability = WealthLiability(user_id=user_id, **fields)
    db.session.add(liability)
    db.session.commit()
    return liability, None


def update_liability(db, liability, user_id, form):
    """Update an existing liability, scoped to the owning user."""
    if liability.user_id != user_id:
        return None, "You do not have permission to edit this liability."

    fields = _liability_fields_from_form(form)
    if fields["ownership_percentage"] is None:
        fields["ownership_percentage"] = 100.0

    for key, value in fields.items():
        setattr(liability, key, value)
    liability.updated_at = datetime.utcnow()

    db.session.commit()
    return liability, None


def archive_liability(db, liability, user_id):
    """Soft-delete — move to archive. Returns (success, error)."""
    if liability.user_id != user_id:
        return False, "You do not have permission to archive this liability."
    if liability.is_archived:
        return False, "Liability is already archived."

    liability.is_archived = True
    liability.status = WealthStatus.ARCHIVED
    liability.archived_at = datetime.utcnow()
    db.session.commit()
    return True, None


def restore_liability(db, liability, user_id):
    """Restore an archived liability. Returns (success, error)."""
    if liability.user_id != user_id:
        return False, "You do not have permission to restore this liability."
    if not liability.is_archived:
        return False, "Liability is not archived."

    liability.is_archived = False
    liability.status = WealthStatus.ACTIVE
    liability.archived_at = None
    db.session.commit()
    return True, None


def delete_liability_permanent(db, liability, user_id):
    """
    Permanently delete a liability. Only allowed if already archived
    (Section 28/31 of spec — Active -> Archive -> Delete, never a
    direct Active -> Delete path).
    """
    if liability.user_id != user_id:
        return False, "You do not have permission to delete this liability."
    if not liability.is_archived:
        return False, "Only archived liabilities can be permanently deleted."

    db.session.delete(liability)
    db.session.commit()
    return True, None


def get_liability_or_none(liability_id, user_id):
    """Fetch a single liability, scoped to the owning user. None if
    not found or not owned — routes turn this into a 404."""
    return WealthLiability.query.filter_by(id=liability_id, user_id=user_id).first()


def get_liabilities_for_listing(user_id, q=None, category=None, status_filter="active",
                                ownership=None, sort_by="newest"):
    """
    Server-side filtered and sorted liability listing (Section 32/34
    of spec — database-level, not Python-level sorting/filtering).

    Note: search deliberately does NOT include account_reference
    (Section 15/32 — loan/card reference numbers are sensitive and
    should not be searchable/matchable text, even though a search
    term happening to match one would only reveal that a record
    exists, not the reference itself).
    """
    query = WealthLiability.query.filter_by(user_id=user_id)
    query = query.filter_by(is_archived=(status_filter == "archived"))

    if category:
        query = query.filter(WealthLiability.category == category)
    if ownership:
        query = query.filter(WealthLiability.ownership_type == ownership)

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            WealthLiability.name.ilike(like),
            WealthLiability.liability_type.ilike(like),
            WealthLiability.category.ilike(like),
            WealthLiability.lender.ilike(like),
            WealthLiability.description.ilike(like),
        ))

    sort_map = {
        "newest":          WealthLiability.created_at.desc(),
        "oldest":          WealthLiability.created_at.asc(),
        "outstanding_high": WealthLiability.outstanding_amount.desc(),
        "outstanding_low":  WealthLiability.outstanding_amount.asc(),
        "original_high":    WealthLiability.original_amount.desc(),
        "original_low":     WealthLiability.original_amount.asc(),
        "name_az":          WealthLiability.name.asc(),
        "name_za":          WealthLiability.name.desc(),
    }
    query = query.order_by(sort_map.get(sort_by, WealthLiability.created_at.desc()))

    return query.all()
