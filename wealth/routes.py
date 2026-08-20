"""
Wealth — Routes
==================
Phase A: dashboard skeleton.
Phase B: full Asset CRUD — listing (search/filter/sort), Add, Edit,
         Detail, Archive, Restore, Permanent Delete.
Phase C: full Liability CRUD, same lifecycle pattern.
"""

from datetime import date
import os

from flask import render_template, redirect, url_for, request, flash, abort, send_file
from flask_login import login_required, current_user

from . import wealth_bp
from . import services
from . import validators
from . import history_service
from . import document_service
from .models import (
    WealthAsset, WealthAssetCategory, ASSET_TYPES_BY_CATEGORY,
    FIELD_GROUPS_BY_CATEGORY, OwnershipType, SourceType, WealthStatus,
    AreaUnit, WeightUnit,
    WealthLiability, WealthLiabilityCategory, LIABILITY_TYPES_BY_CATEGORY,
    WealthDocumentCategory, DOCUMENT_TYPES_BY_CATEGORY,
    SnapshotSource,
)
from . import utils
from .utils import format_inr, format_date


def _db():
    from models import db
    return db


def _get_asset_or_404(asset_id):
    """Get an asset — must belong to the current user. 404 otherwise."""
    return WealthAsset.query.filter_by(
        id=asset_id, user_id=current_user.id
    ).first_or_404()


def _get_liability_or_404(liability_id):
    """Get a liability — must belong to the current user. 404 otherwise.
    Mirrors _get_asset_or_404 exactly (Section 39/40 of spec — user
    isolation enforced at the query level, not just hidden in the UI)."""
    return WealthLiability.query.filter_by(
        id=liability_id, user_id=current_user.id
    ).first_or_404()


# ── Dashboard ─────────────────────────────────────────────────────────────────

@wealth_bp.route("/")
@login_required
def dashboard():
    """
    Wealth dashboard. Phase B: now shows real Asset statistics
    (Section 22 of spec) instead of empty placeholders. Liabilities
    and Family & Inherited Wealth stay as honest "coming soon"
    sections — those are still out of scope until later phases.
    """
    stats = services.WealthStatisticsService(current_user.id)
    data  = stats.summary_dict()

    # Section 42/43 of Phase F spec — dashboard's History card
    # consumes the exact same history_service calls as /wealth/history,
    # and is None (hidden entirely) with fewer than 2 snapshots.
    history_trend = history_service.dashboard_trend_summary(current_user.id)

    # Section 32/43 of Phase G spec — same rule for the Document
    # Vault card: consumes document_service.dashboard_summary(), the
    # exact same call the Vault page itself would use for its totals.
    document_summary = document_service.dashboard_summary(current_user.id)

    return render_template(
        "wealth/dashboard.html",
        data=data,
        history_trend=history_trend,
        document_summary=document_summary,
        format_inr=format_inr,
        format_date=format_date,
    )


# ── Asset Listing (Phase B) ───────────────────────────────────────────────────

@wealth_bp.route("/assets")
@login_required
def assets_listing():
    """Assets listing with search, filter, and sort — all applied at
    the database level (Section 15/16 of spec)."""
    q             = request.args.get("q", "").strip()
    category      = request.args.get("category", "")
    status_filter = request.args.get("status", "active")
    ownership     = request.args.get("ownership", "")
    source        = request.args.get("source", "")
    sort_by       = request.args.get("sort", "newest")

    assets = services.get_assets_for_listing(
        current_user.id, q=q or None, category=category or None,
        status_filter=status_filter, ownership=ownership or None,
        source=source or None, sort_by=sort_by,
    )

    stats = services.WealthStatisticsService(current_user.id)
    summary = stats.summary_dict()

    return render_template(
        "wealth/assets_listing.html",
        assets=assets,
        summary=summary,
        categories=WealthAssetCategory.ALL,
        ownership_types=OwnershipType.ALL,
        source_types=SourceType.ALL,
        q=q, category=category, status_filter=status_filter,
        ownership=ownership, source=source, sort_by=sort_by,
        format_inr=format_inr, format_date=format_date,
    )


# ── Add / Edit Asset (shared form) ────────────────────────────────────────────

def _asset_form_context(is_edit, asset, values):
    """Shared kwargs for rendering wealth_asset_form.html in either
    mode — single source of truth so Add and Edit templates can
    never drift apart (Section 5/31 of spec)."""
    return dict(
        is_edit=is_edit,
        asset=asset,
        values=values,
        categories=WealthAssetCategory.ALL,
        asset_types_by_category=ASSET_TYPES_BY_CATEGORY,
        field_groups=FIELD_GROUPS_BY_CATEGORY,
        ownership_types=OwnershipType.ALL,
        source_types=SourceType.ALL,
        statuses=WealthStatus.ALL,
        area_units=AreaUnit.ALL,
        weight_units=WeightUnit.ALL,
    )


def _asset_to_values(asset):
    """Convert a WealthAsset object into the same flat string-keyed
    dict shape the form submits, so Edit mode and a failed-validation
    redisplay share one code path."""
    if asset is None:
        return {}

    def _d(d):
        return d.isoformat() if d else ""

    return {
        "name": asset.name or "", "category": asset.category or "",
        "asset_type": asset.asset_type or "", "description": asset.description or "",
        "current_value": asset.current_value if asset.current_value is not None else "",
        "value_as_of": _d(asset.value_as_of),
        "ownership_type": asset.ownership_type or "",
        "ownership_percentage": asset.ownership_percentage
                                 if asset.ownership_percentage is not None else "",
        "source_type": asset.source_type or "",
        "original_owner": asset.original_owner or "",
        "original_owner_relationship": asset.original_owner_relationship or "",
        "date_received": _d(asset.date_received),
        "acquisition_date": _d(asset.acquisition_date),
        "acquisition_value": asset.acquisition_value
                              if asset.acquisition_value is not None else "",
        "property_type": asset.property_type or "", "property_address": asset.property_address or "",
        "city": asset.city or "", "state": asset.state or "",
        "area": asset.area if asset.area is not None else "", "area_unit": asset.area_unit or "",
        "metal_type": asset.metal_type or "",
        "weight": asset.weight if asset.weight is not None else "", "weight_unit": asset.weight_unit or "",
        "vehicle_type": asset.vehicle_type or "", "registration_number": asset.registration_number or "",
        "institution": asset.institution or "", "account_reference": asset.account_reference or "",
        "deposit_type": asset.deposit_type or "",
        "interest_rate": asset.interest_rate if asset.interest_rate is not None else "",
        "maturity_date": _d(asset.maturity_date), "investment_type": asset.investment_type or "",
        "status": asset.status or "", "notes": asset.notes or "",
    }


@wealth_bp.route("/assets/add", methods=["GET", "POST"])
@login_required
def add_asset():
    """Add a new Wealth asset."""
    if request.method == "POST":
        form = request.form.to_dict()
        errors = validators.validate_wealth_asset(form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "wealth/wealth_asset_form.html",
                **_asset_form_context(False, None, form)
            )

        asset, error = services.create_asset(_db(), current_user.id, form)
        if error:
            flash(error, "error")
            return redirect(url_for("wealth.add_asset"))

        flash("Asset added successfully.", "success")
        return redirect(url_for("wealth.asset_detail", asset_id=asset.id))

    preset_category = request.args.get("category", "")
    initial_values = {"category": preset_category} if preset_category in WealthAssetCategory.ALL else {}

    return render_template(
        "wealth/wealth_asset_form.html",
        **_asset_form_context(False, None, initial_values)
    )


@wealth_bp.route("/assets/<int:asset_id>/edit", methods=["GET", "POST"])
@login_required
def edit_asset(asset_id):
    """Edit an existing asset (active or archived) — reuses the add form."""
    asset = _get_asset_or_404(asset_id)

    if request.method == "POST":
        form = request.form.to_dict()
        errors = validators.validate_wealth_asset(form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "wealth/wealth_asset_form.html",
                **_asset_form_context(True, asset, form)
            )

        updated, error = services.update_asset(_db(), asset, current_user.id, form)
        if error:
            flash(error, "error")
            return redirect(url_for("wealth.asset_detail", asset_id=asset_id))

        flash("Asset updated successfully.", "success")
        return redirect(url_for("wealth.asset_detail", asset_id=asset_id))

    return render_template(
        "wealth/wealth_asset_form.html",
        **_asset_form_context(True, asset, _asset_to_values(asset))
    )


# ── Detail Page ────────────────────────────────────────────────────────────────

@wealth_bp.route("/assets/<int:asset_id>")
@login_required
def asset_detail(asset_id):
    """Detail view for a single asset."""
    asset = WealthAsset.query.filter_by(
        id=asset_id, user_id=current_user.id).first()
    if not asset:
        abort(404)

    documents = document_service.get_documents_for_asset(asset.id, current_user.id)

    return render_template(
        "wealth/asset_detail.html",
        asset=asset,
        documents=documents,
        format_inr=format_inr,
        format_date=format_date,
    )


# ── Archive / Restore / Delete ────────────────────────────────────────────────

@wealth_bp.route("/assets/<int:asset_id>/archive", methods=["POST"])
@login_required
def archive_asset(asset_id):
    asset = _get_asset_or_404(asset_id)
    success, error = services.archive_asset(_db(), asset, current_user.id)
    flash(error, "error") if error else flash("Asset archived. You can restore it any time.", "success")
    next_target = request.form.get("next", "detail")
    if next_target == "listing":
        return redirect(url_for("wealth.assets_listing"))
    return redirect(url_for("wealth.asset_detail", asset_id=asset_id))


@wealth_bp.route("/assets/<int:asset_id>/restore", methods=["POST"])
@login_required
def restore_asset(asset_id):
    asset = _get_asset_or_404(asset_id)
    success, error = services.restore_asset(_db(), asset, current_user.id)
    flash(error, "error") if error else flash("Asset restored successfully.", "success")
    return redirect(url_for("wealth.asset_detail", asset_id=asset_id))


@wealth_bp.route("/assets/<int:asset_id>/delete", methods=["POST"])
@login_required
def delete_asset_permanent(asset_id):
    asset = _get_asset_or_404(asset_id)
    success, error = services.delete_asset_permanent(_db(), asset, current_user.id)
    if error:
        flash(error, "error")
        return redirect(url_for("wealth.asset_detail", asset_id=asset_id))
    flash("Asset permanently deleted.", "success")
    return redirect(url_for("wealth.assets_listing", status="archived"))


# ── Liability Listing (Phase C) ───────────────────────────────────────────────

@wealth_bp.route("/liabilities")
@login_required
def liabilities_listing():
    """Liabilities listing with search, filter, and sort — all applied
    at the database level (Section 32/34 of spec)."""
    q             = request.args.get("q", "").strip()
    category      = request.args.get("category", "")
    status_filter = request.args.get("status", "active")
    ownership     = request.args.get("ownership", "")
    sort_by       = request.args.get("sort", "newest")

    liabilities = services.get_liabilities_for_listing(
        current_user.id, q=q or None, category=category or None,
        status_filter=status_filter, ownership=ownership or None,
        sort_by=sort_by,
    )

    stats = services.WealthStatisticsService(current_user.id)
    summary = stats.summary_dict()

    return render_template(
        "wealth/liabilities_listing.html",
        liabilities=liabilities,
        summary=summary,
        categories=WealthLiabilityCategory.ALL,
        ownership_types=OwnershipType.ALL,
        q=q, category=category, status_filter=status_filter,
        ownership=ownership, sort_by=sort_by,
        format_inr=format_inr, format_date=format_date,
    )


# ── Add / Edit Liability (shared form) ────────────────────────────────────────

def _liability_form_context(is_edit, liability, values):
    """Shared kwargs for rendering wealth_liability_form.html in
    either mode — mirrors _asset_form_context exactly (Section 6/66
    of spec: single source of truth for Add and Edit)."""
    return dict(
        is_edit=is_edit,
        liability=liability,
        values=values,
        categories=WealthLiabilityCategory.ALL,
        liability_types_by_category=LIABILITY_TYPES_BY_CATEGORY,
        ownership_types=OwnershipType.ALL,
        statuses=WealthStatus.ALL,
    )


def _liability_to_values(liability):
    """Convert a WealthLiability object into the same flat
    string-keyed dict shape the form submits, so Edit mode and a
    failed-validation redisplay share one code path."""
    if liability is None:
        return {}

    def _d(d):
        return d.isoformat() if d else ""

    return {
        "name": liability.name or "", "category": liability.category or "",
        "liability_type": liability.liability_type or "",
        "description": liability.description or "",
        "original_amount": liability.original_amount
                            if liability.original_amount is not None else "",
        "outstanding_amount": liability.outstanding_amount
                              if liability.outstanding_amount is not None else "",
        "interest_rate": liability.interest_rate
                         if liability.interest_rate is not None else "",
        "start_date": _d(liability.start_date),
        "expected_end_date": _d(liability.expected_end_date),
        "lender": liability.lender or "",
        "account_reference": liability.account_reference or "",
        "ownership_type": liability.ownership_type or "",
        "ownership_percentage": liability.ownership_percentage
                                if liability.ownership_percentage is not None else "",
        "status": liability.status or "", "notes": liability.notes or "",
    }


@wealth_bp.route("/liabilities/add", methods=["GET", "POST"])
@login_required
def add_liability():
    """Add a new Wealth liability."""
    if request.method == "POST":
        form = request.form.to_dict()
        errors = validators.validate_wealth_liability(form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "wealth/wealth_liability_form.html",
                **_liability_form_context(False, None, form)
            )

        liability, error = services.create_liability(_db(), current_user.id, form)
        if error:
            flash(error, "error")
            return redirect(url_for("wealth.add_liability"))

        flash("Liability added successfully.", "success")
        return redirect(url_for("wealth.liability_detail", liability_id=liability.id))

    preset_category = request.args.get("category", "")
    initial_values = ({"category": preset_category}
                      if preset_category in WealthLiabilityCategory.ALL else {})

    return render_template(
        "wealth/wealth_liability_form.html",
        **_liability_form_context(False, None, initial_values)
    )


@wealth_bp.route("/liabilities/<int:liability_id>/edit", methods=["GET", "POST"])
@login_required
def edit_liability(liability_id):
    """Edit an existing liability (active or archived) — reuses the add form."""
    liability = _get_liability_or_404(liability_id)

    if request.method == "POST":
        form = request.form.to_dict()
        errors = validators.validate_wealth_liability(form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "wealth/wealth_liability_form.html",
                **_liability_form_context(True, liability, form)
            )

        updated, error = services.update_liability(_db(), liability, current_user.id, form)
        if error:
            flash(error, "error")
            return redirect(url_for("wealth.liability_detail", liability_id=liability_id))

        flash("Liability updated successfully.", "success")
        return redirect(url_for("wealth.liability_detail", liability_id=liability_id))

    return render_template(
        "wealth/wealth_liability_form.html",
        **_liability_form_context(True, liability, _liability_to_values(liability))
    )


# ── Detail Page ────────────────────────────────────────────────────────────────

@wealth_bp.route("/liabilities/<int:liability_id>")
@login_required
def liability_detail(liability_id):
    """Detail view for a single liability."""
    liability = WealthLiability.query.filter_by(
        id=liability_id, user_id=current_user.id).first()
    if not liability:
        abort(404)

    documents = document_service.get_documents_for_liability(liability.id, current_user.id)

    return render_template(
        "wealth/liability_detail.html",
        liability=liability,
        documents=documents,
        format_inr=format_inr,
        format_date=format_date,
    )


# ── Archive / Restore / Delete ────────────────────────────────────────────────

@wealth_bp.route("/liabilities/<int:liability_id>/archive", methods=["POST"])
@login_required
def archive_liability(liability_id):
    liability = _get_liability_or_404(liability_id)
    success, error = services.archive_liability(_db(), liability, current_user.id)
    flash(error, "error") if error else flash("Liability archived. You can restore it any time.", "success")
    next_target = request.form.get("next", "detail")
    if next_target == "listing":
        return redirect(url_for("wealth.liabilities_listing"))
    return redirect(url_for("wealth.liability_detail", liability_id=liability_id))


@wealth_bp.route("/liabilities/<int:liability_id>/restore", methods=["POST"])
@login_required
def restore_liability(liability_id):
    liability = _get_liability_or_404(liability_id)
    success, error = services.restore_liability(_db(), liability, current_user.id)
    flash(error, "error") if error else flash("Liability restored successfully.", "success")
    return redirect(url_for("wealth.liability_detail", liability_id=liability_id))


@wealth_bp.route("/liabilities/<int:liability_id>/delete", methods=["POST"])
@login_required
def delete_liability_permanent(liability_id):
    liability = _get_liability_or_404(liability_id)
    success, error = services.delete_liability_permanent(_db(), liability, current_user.id)
    if error:
        flash(error, "error")
        return redirect(url_for("wealth.liability_detail", liability_id=liability_id))
    flash("Liability permanently deleted.", "success")
    return redirect(url_for("wealth.liabilities_listing", status="archived"))


@wealth_bp.route("/net-worth")
@login_required
def net_worth():
    """
    Dedicated Net Worth page — built entirely from the existing
    WealthStatisticsService (Section 25 of Phase D spec: one
    authoritative service, reused here AND by the dashboard, never
    duplicated). The dashboard and this page call the exact same
    summary_dict(), so they can never diverge (Section 24).
    """
    stats = services.WealthStatisticsService(current_user.id)
    data = stats.summary_dict()

    return render_template(
        "wealth/net_worth.html",
        data=data,
        format_inr=format_inr,
    )


# ── Family & Inherited Wealth (Phase E) ───────────────────────────────────────

@wealth_bp.route("/family")
@login_required
def family_wealth():
    """
    Dedicated Family & Inherited Wealth view — a derived analytical
    layer over existing WealthAsset records (Section 2 of spec: no
    duplicate Asset table, no second Net Worth system). Editing still
    happens through the normal Asset detail/edit pages, never here.
    """
    q             = request.args.get("q", "").strip()
    source        = request.args.get("source", "")
    category      = request.args.get("category", "")
    ownership     = request.args.get("ownership", "")
    status_filter = request.args.get("status", "active")

    stats = services.WealthStatisticsService(current_user.id)

    assets = stats.get_family_assets(
        source=source or None, category=category or None,
        ownership=ownership or None, q=q or None, status_filter=status_filter,
    )

    summary = {
        "family_asset_count":         stats.family_asset_count(),
        "inherited_asset_count":      stats.inherited_asset_count(),
        "family_owned_asset_count":   stats.family_owned_asset_count(),
        "gifted_asset_count":         stats.gifted_asset_count(),
        "total_family_value":         stats.total_family_asset_value(),
        "attributable_family_value":  stats.attributable_family_asset_value(),
        "category_breakdown":         stats.family_asset_category_breakdown(),
        "family_liability_count":              stats.family_liability_count(),
        "total_family_liability_value":        stats.total_family_liability_value(),
        "attributable_family_liability_value": stats.attributable_family_liability_value(),
    }

    return render_template(
        "wealth/family_wealth.html",
        assets=assets,
        summary=summary,
        source_types=SourceType.ALL,
        categories=WealthAssetCategory.ALL,
        ownership_types=OwnershipType.ALL,
        q=q, source=source, category=category, ownership=ownership,
        status_filter=status_filter,
        format_inr=format_inr, format_date=format_date,
    )
# ── Wealth History (Phase F) ────────────────────────────────────────────────

def _get_snapshot_or_404(snapshot_id):
    """Get a snapshot — must belong to the current user. 404
    otherwise. Mirrors _get_asset_or_404 / _get_liability_or_404
    exactly (Section 30 — user isolation enforced at the query
    level)."""
    snapshot = history_service.get_snapshot_or_none(snapshot_id, current_user.id)
    if not snapshot:
        abort(404)
    return snapshot


@wealth_bp.route("/history")
@login_required
def wealth_history():
    """
    Wealth History — chronological list + Net Worth chart of the
    user's saved snapshots, with a 3M/6M/1Y/All range filter
    (Section 4/17/18/20). Also shows today's live Wealth position at
    the top for the "create snapshot" action, kept clearly separate
    from historical rows (Section 41).
    """
    range_filter = request.args.get("range", "1y")
    if range_filter not in ("3m", "6m", "1y", "all"):
        range_filter = "1y"

    snapshots = history_service.get_snapshots(current_user.id, range_filter=range_filter)
    total_snapshot_count = history_service.snapshot_count(current_user.id)

    # Section 22/23: change info only makes sense with >=2 snapshots
    # in total — a filtered range showing only 1 of many is still a
    # real "not enough data in view" case, so this checks the total,
    # not just what's in the current range.
    latest = snapshots[0] if snapshots else None
    latest_change = (history_service.snapshot_change(latest, current_user.id)
                     if latest else None)

    current_position = services.WealthStatisticsService(current_user.id).summary_dict()

    return render_template(
        "wealth/history.html",
        snapshots=snapshots,
        total_snapshot_count=total_snapshot_count,
        latest=latest,
        latest_change=latest_change,
        current_position=current_position,
        chart_points=history_service.chart_data(snapshots),
        range_filter=range_filter,
        today=date.today().isoformat(),
        duplicate_date=request.args.get("duplicate_date", ""),
        format_inr=format_inr, format_date=format_date,
    )


@wealth_bp.route("/history/create", methods=["POST"])
@login_required
def create_wealth_snapshot():
    """
    Manual snapshot creation (Section 9). Handles the duplicate-date
    case without silently overwriting anything (Section 10/11) — a
    second submit with confirm_replace=1 is required to actually
    replace an existing snapshot for that date.
    """
    snapshot_date_raw = request.form.get("snapshot_date")
    confirm_replace = request.form.get("confirm_replace") == "1"

    snapshot_date, error = validators.validate_snapshot_date(snapshot_date_raw)
    if error:
        flash(error, "error")
        return redirect(url_for("wealth.wealth_history"))

    snapshot, error, needs_confirmation = history_service.create_snapshot(
        _db(), current_user.id, snapshot_date, confirm_replace=confirm_replace,
        source=SnapshotSource.MANUAL)

    if needs_confirmation:
        # Nothing was written. Redirect back with the pending date so
        # the template can show the "Replace Snapshot?" confirmation
        # (Section 10/11) — no silent duplicate, no data loss either way.
        return redirect(url_for("wealth.wealth_history",
                                range=request.form.get("range", "1y"),
                                duplicate_date=snapshot_date.isoformat()))

    if error:
        flash(error, "error")
        return redirect(url_for("wealth.wealth_history"))

    verb = "replaced" if confirm_replace else "created"
    flash(f"Wealth snapshot {verb} for {format_date(snapshot_date)}.", "success")
    return redirect(url_for("wealth.snapshot_detail", snapshot_id=snapshot.id))


@wealth_bp.route("/history/<int:snapshot_id>")
@login_required
def snapshot_detail(snapshot_id):
    """
    Snapshot detail page (Section 26/27) — displays ONLY the stored
    historical values, never recalculated from today's Assets/
    Liabilities (Section 16/27: historical data must not depend on
    live records).
    """
    snapshot = _get_snapshot_or_404(snapshot_id)
    change = history_service.snapshot_change(snapshot, current_user.id)

    return render_template(
        "wealth/history_detail.html",
        snapshot=snapshot,
        change=change,
        format_inr=format_inr, format_date=format_date,
    )


@wealth_bp.route("/history/<int:snapshot_id>/delete", methods=["POST"])
@login_required
def delete_wealth_snapshot(snapshot_id):
    """
    Permanently delete a single snapshot (Section 28). Never touches
    Assets, Liabilities, or any other snapshot (Section 61/65) —
    history_service.delete_snapshot() only ever removes this one row.
    """
    snapshot = _get_snapshot_or_404(snapshot_id)
    success, error = history_service.delete_snapshot(_db(), snapshot, current_user.id)
    if error:
        flash(error, "error")
        return redirect(url_for("wealth.snapshot_detail", snapshot_id=snapshot_id))
    flash("Wealth snapshot permanently deleted.", "success")
    return redirect(url_for("wealth.wealth_history"))
# ── Wealth Document Vault (Phase G) ─────────────────────────────────────────

def _get_document_or_404(document_id):
    doc = document_service.get_document_or_none(document_id, current_user.id)
    if not doc:
        abort(404)
    return doc


def _document_upload_options(user_id):
    assets = (WealthAsset.query
             .filter_by(user_id=user_id, is_archived=False)
             .order_by(WealthAsset.name.asc()).all())
    liabilities = (WealthLiability.query
                  .filter_by(user_id=user_id, is_archived=False)
                  .order_by(WealthLiability.name.asc()).all())
    return assets, liabilities


@wealth_bp.route("/documents")
@login_required
def documents_vault():
    q             = request.args.get("q", "").strip()
    category      = request.args.get("category", "")
    document_type = request.args.get("document_type", "")
    sort_by       = request.args.get("sort", "newest")

    documents = document_service.get_vault_documents(
        current_user.id, q=q or None, category=category or None,
        document_type=document_type or None, sort_by=sort_by,
    )
    summary = document_service.vault_summary(current_user.id)

    return render_template(
        "wealth/documents_vault.html",
        documents=documents,
        summary=summary,
        categories=WealthDocumentCategory.ALL,
        document_types_by_category=DOCUMENT_TYPES_BY_CATEGORY,
        q=q, category=category, document_type=document_type, sort_by=sort_by,
        format_date=format_date,
    )


@wealth_bp.route("/documents/add", methods=["GET", "POST"])
@login_required
def add_document():
    assets, liabilities = _document_upload_options(current_user.id)

    if request.method == "POST":
        file          = request.files.get("document")
        category      = request.form.get("category", "").strip()
        document_type = request.form.get("document_type", "").strip()
        title         = request.form.get("title", "").strip()
        description   = request.form.get("description", "").strip()
        asset_id      = request.form.get("asset_id", type=int)
        liability_id  = request.form.get("liability_id", type=int)

        errors = validators.validate_document(file, category, document_type)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "wealth/document_form.html", is_edit=False, document=None,
                assets=assets, liabilities=liabilities,
                categories=WealthDocumentCategory.ALL,
                document_types_by_category=DOCUMENT_TYPES_BY_CATEGORY,
                values=request.form, preselect_asset_id=asset_id,
                preselect_liability_id=liability_id,
            )

        try:
            stored_name, file_path, file_size = utils.save_document_file(file, current_user.id)
        except OSError as e:
            flash(f"File could not be saved: {e}", "error")
            return redirect(url_for("wealth.add_document"))

        doc, error = document_service.save_document_metadata(
            _db(), current_user.id,
            category=category, document_type=document_type,
            original_name=file.filename, stored_name=stored_name,
            file_path=file_path, file_size=file_size,
            title=title, description=description,
            asset_id=asset_id, liability_id=liability_id,
        )
        if error:
            utils.delete_document_file(file_path)
            flash(error, "error")
            return redirect(url_for("wealth.add_document"))

        flash("Document uploaded successfully.", "success")
        return redirect(url_for("wealth.document_detail", document_id=doc.id))

    preselect_asset_id     = request.args.get("asset_id", type=int)
    preselect_liability_id = request.args.get("liability_id", type=int)

    return render_template(
        "wealth/document_form.html", is_edit=False, document=None,
        assets=assets, liabilities=liabilities,
        categories=WealthDocumentCategory.ALL,
        document_types_by_category=DOCUMENT_TYPES_BY_CATEGORY,
        values={}, preselect_asset_id=preselect_asset_id,
        preselect_liability_id=preselect_liability_id,
    )


@wealth_bp.route("/documents/<int:document_id>")
@login_required
def document_detail(document_id):
    doc = _get_document_or_404(document_id)

    related_asset = (WealthAsset.query.filter_by(
        id=doc.asset_id, user_id=current_user.id).first() if doc.asset_id else None)
    related_liability = (WealthLiability.query.filter_by(
        id=doc.liability_id, user_id=current_user.id).first() if doc.liability_id else None)

    return render_template(
        "wealth/document_detail.html",
        doc=doc,
        related_asset=related_asset,
        related_liability=related_liability,
        is_previewable=utils.is_previewable(doc.original_name),
        format_date=format_date,
    )


@wealth_bp.route("/documents/<int:document_id>/edit", methods=["GET", "POST"])
@login_required
def edit_document(document_id):
    doc = _get_document_or_404(document_id)
    assets, liabilities = _document_upload_options(current_user.id)

    if request.method == "POST":
        category      = request.form.get("category", "").strip()
        document_type = request.form.get("document_type", "").strip()
        title         = request.form.get("title", "").strip()
        description   = request.form.get("description", "").strip()
        asset_id      = request.form.get("asset_id", type=int)
        liability_id  = request.form.get("liability_id", type=int)

        success, error = document_service.update_document_metadata(
            _db(), doc, current_user.id,
            title=title, description=description,
            category=category, document_type=document_type,
            asset_id=asset_id, liability_id=liability_id,
        )
        if error:
            flash(error, "error")
            return render_template(
                "wealth/document_form.html", is_edit=True, document=doc,
                assets=assets, liabilities=liabilities,
                categories=WealthDocumentCategory.ALL,
                document_types_by_category=DOCUMENT_TYPES_BY_CATEGORY,
                values=request.form,
                preselect_asset_id=asset_id, preselect_liability_id=liability_id,
            )

        flash("Document updated successfully.", "success")
        return redirect(url_for("wealth.document_detail", document_id=doc.id))

    values = {
        "title": doc.title or "", "description": doc.description or "",
        "category": doc.category, "document_type": doc.document_type,
    }
    return render_template(
        "wealth/document_form.html", is_edit=True, document=doc,
        assets=assets, liabilities=liabilities,
        categories=WealthDocumentCategory.ALL,
        document_types_by_category=DOCUMENT_TYPES_BY_CATEGORY,
        values=values,
        preselect_asset_id=doc.asset_id, preselect_liability_id=doc.liability_id,
    )


@wealth_bp.route("/documents/<int:document_id>/download")
@login_required
def download_document(document_id):
    doc = _get_document_or_404(document_id)

    if not doc.file_path or not os.path.exists(doc.file_path):
        flash("Document file is no longer available.", "error")
        return redirect(url_for("wealth.document_detail", document_id=document_id))

    if not utils.secure_file_path(doc.file_path, current_user.id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("wealth.document_detail", document_id=document_id))

    return send_file(
        doc.file_path,
        as_attachment=True,
        download_name=doc.original_name,
    )


@wealth_bp.route("/documents/<int:document_id>/preview")
@login_required
def preview_document(document_id):
    doc = _get_document_or_404(document_id)

    if not doc.file_path or not os.path.exists(doc.file_path):
        flash("Document file is no longer available.", "error")
        return redirect(url_for("wealth.document_detail", document_id=document_id))

    if not utils.secure_file_path(doc.file_path, current_user.id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("wealth.document_detail", document_id=document_id))

    if not utils.is_previewable(doc.original_name):
        flash("Preview is not available for this file type — please download it instead.", "error")
        return redirect(url_for("wealth.document_detail", document_id=document_id))

    return send_file(
        doc.file_path,
        mimetype=utils.get_preview_mimetype(doc.original_name),
        as_attachment=False,
        download_name=doc.original_name,
    )


@wealth_bp.route("/documents/<int:document_id>/delete", methods=["POST"])
@login_required
def delete_document(document_id):
    doc = _get_document_or_404(document_id)

    if doc.file_path and not utils.secure_file_path(doc.file_path, current_user.id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("wealth.document_detail", document_id=document_id))

    utils.delete_document_file(doc.file_path)
    success, error = document_service.delete_document(_db(), doc, current_user.id)
    if error:
        flash(error, "error")
        return redirect(url_for("wealth.document_detail", document_id=document_id))

    flash("Document permanently deleted.", "success")
    return redirect(url_for("wealth.documents_vault"))