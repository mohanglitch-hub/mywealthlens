"""
Retirement Centre — Routes
=============================
Phase A: dashboard skeleton.
Phase B: Add / Edit / Detail / Archive / Restore for schemes.
Phase C: Contribution history, balance snapshots, nominees, and
         maturity/target-retirement calculations.

Documents are not yet implemented — that belongs to a later phase.
"""

from datetime import date

from flask import (
    render_template, redirect, url_for, request, flash, abort, send_file,
)
from flask_login import login_required, current_user

from . import retirement_bp
from . import services
from . import validators
from .models import (
    RetirementScheme, RetirementContribution, RetirementSchemeNominee,
    RetirementDocument, SchemeType, SchemeStatus, GrowthMethod,
    ContributionPreference, NPSTier, NomineeRelation, RetirementDocumentType,
    scheme_type_to_category, category_to_slug,
)
from .utils import (
    format_inr, format_date, financial_year_bounds, mask_account_number,
    save_document_file, delete_document_file, secure_file_path,
    is_previewable, get_preview_mimetype, current_financial_year,
)


def _db():
    from models import db
    return db


# ── Private Helpers (user-isolation, mirrors insurance_centre pattern) ───────

def _get_scheme_or_404(scheme_id):
    """Get a scheme — must belong to the current user. 404 otherwise."""
    return RetirementScheme.query.filter_by(
        id=scheme_id, user_id=current_user.id
    ).first_or_404()


def _get_contribution_or_404(contribution_id):
    """Get a contribution — must belong to the current user via user_id
    stored directly on the row (checked at write time, verified here too)."""
    return RetirementContribution.query.filter_by(
        id=contribution_id, user_id=current_user.id
    ).first_or_404()


def _get_nominee_or_404(nominee_id):
    """Get a nominee — must belong to the current user."""
    return RetirementSchemeNominee.query.filter_by(
        id=nominee_id, user_id=current_user.id
    ).first_or_404()


def _get_document_or_404(doc_id):
    """Get a document — must belong to the current user."""
    return RetirementDocument.query.filter_by(
        id=doc_id, user_id=current_user.id
    ).first_or_404()


def _scheme_to_values(scheme):
    """
    Convert a RetirementScheme object into the same flat string-keyed
    dict shape the form submits, so the template only ever has ONE
    data shape to pre-populate from — whether it's a fresh GET-edit,
    or a POST that failed validation and needs to redisplay what the
    user typed (Section 20 of Phase B spec — nothing should mysteriously
    go blank).
    """
    if scheme is None:
        return {}

    def _d(d):
        return d.isoformat() if d else ""

    return {
        "scheme_type":    scheme.scheme_type or "",
        "custom_type":    scheme.custom_type or "",
        "institution":    scheme.institution or "",
        "account_number": scheme.account_number or "",
        "opening_date":   _d(scheme.opening_date),

        "current_balance":    scheme.current_balance if scheme.current_balance is not None else "",
        "balance_updated_at": _d(scheme.balance_updated_at),

        "growth_method":             scheme.growth_method or "",
        "rate_or_return_assumption": scheme.rate_or_return_assumption
                                      if scheme.rate_or_return_assumption is not None else "",

        "status": scheme.status or "",
        "notes":  scheme.notes or "",

        "contribution_preference": scheme.contribution_preference or "",

        "employer_name": scheme.employer_name or "",
        "uan_number":     scheme.uan_number or "",
        "basic_salary":   scheme.basic_salary if scheme.basic_salary is not None else "",
        "employee_contribution_pct": scheme.employee_contribution_pct
                                      if scheme.employee_contribution_pct is not None else "",
        "employer_contribution_pct": scheme.employer_contribution_pct
                                      if scheme.employer_contribution_pct is not None else "",
        "target_retirement_year": scheme.target_retirement_year
                                   if scheme.target_retirement_year is not None else "",

        "extension_opted": "yes" if scheme.extension_opted else "",

        "girl_child_name": scheme.girl_child_name or "",
        "girl_child_dob":  _d(scheme.girl_child_dob),

        "pran_number": scheme.pran_number or "",
        "tier":        scheme.tier or "",
    }


# ── Dashboard ─────────────────────────────────────────────────────────────────

@retirement_bp.route("/")
@login_required
def dashboard():
    """
    Retirement Centre dashboard.

    Shows real totals (possibly zero), a scheme-type breakdown, and
    upcoming maturity/target-retirement milestones — never fabricated
    data (Section 20 of the Phase A spec, still true here).
    """
    stats = services.RetirementStatisticsService(current_user.id)
    data  = stats.summary_dict()

    # Attach each scheme's maturity info for the row display, mirroring
    # the existing app-wide pattern of attaching display-only computed
    # attributes before rendering (see insurance_centre's renewal_badge
    # usage in its own dashboard route).
    for s in data["schemes"]:
        s._maturity = services.compute_maturity_info(s)

    # Compact "Next: X maturity — YYYY" label for the Upcoming
    # Milestones card, built from the already-sorted milestone list.
    next_milestone_label = None
    if data["upcoming_milestones"]:
        m = data["upcoming_milestones"][0]
        kind = m["info"]["kind"]
        year = m["sort_date"].year
        verb = "target retirement" if kind == "target_year" else "maturity"
        next_milestone_label = f"{m['scheme'].display_type} {verb} — {year}"
    data["next_milestone_label"] = next_milestone_label

    # Document Vault stat card (5th card) — real count, no fabrication.
    vault = services.vault_summary(current_user.id)
    data["documents_count"] = vault["total"]

    # Recent Activity feed — last 7 events across all schemes.
    data["recent_activity"] = services.recent_activity(current_user.id, limit=5)

    return render_template(
        "retirement_centre/dashboard.html",
        data=data,
        scheme_types=SchemeType.ALL,
        format_inr=format_inr,
        format_date=format_date,
        mask_account_number=mask_account_number,
        now_date=date.today(),
    )


# ── Add / Edit Scheme (shared form) ───────────────────────────────────────────

def _form_template_context(is_edit, scheme, values, back_category=None):
    """
    Shared kwargs for rendering scheme_form.html in either mode.
    back_category (a display category name, or None) drives the
    breadcrumb and Cancel button destination — so arriving from a
    category page keeps you in that category's context instead of
    always dropping you back at the main dashboard.
    """
    from .models import category_to_slug

    return dict(
        is_edit=is_edit,
        scheme=scheme,
        values=values,
        scheme_types=SchemeType.DISPLAY_OPTIONS,
        growth_methods=GrowthMethod.ALL,
        growth_method_labels=GrowthMethod.LABELS,
        statuses=SchemeStatus.ALL,
        contribution_preferences=ContributionPreference.ALL,
        nps_tiers=NPSTier.ALL,
        field_groups=SchemeType.FIELD_GROUPS,
        back_category=back_category,
        back_slug=category_to_slug(back_category) if back_category else None,
    )


@retirement_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_scheme():
    """Add a new retirement scheme. Supports ?type=PPF to pre-select
    a scheme type when arriving from a category card — the same
    query param also drives the breadcrumb/Cancel back-context, so
    Cancel returns to that category page instead of the dashboard."""
    preset_type = request.args.get("type", "")
    back_category = (scheme_type_to_category(preset_type)
                     if preset_type in SchemeType.ALL else None)

    if request.method == "POST":
        form = request.form.to_dict()
        errors = validators.validate_scheme(form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "retirement_centre/scheme_form.html",
                **_form_template_context(False, None, form, back_category)
            )

        scheme, error = services.create_scheme(_db(), current_user.id, form)
        if error:
            flash(error, "error")
            return redirect(url_for("retirement_centre.add_scheme"))

        flash("Retirement scheme added successfully.", "success")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=scheme.id))

    initial_values = {"scheme_type": preset_type} if preset_type in SchemeType.ALL else {}

    return render_template(
        "retirement_centre/scheme_form.html",
        **_form_template_context(False, None, initial_values, back_category)
    )


@retirement_bp.route("/scheme/<int:scheme_id>/edit", methods=["GET", "POST"])
@login_required
def edit_scheme(scheme_id):
    """Edit an existing scheme (active or archived) — reuses the add
    form. Category context is always derivable from the scheme's own
    type, so this never needs a query param to know where "back" is."""
    scheme = _get_scheme_or_404(scheme_id)
    back_category = scheme_type_to_category(scheme.scheme_type)

    if request.method == "POST":
        form = request.form.to_dict()
        errors = validators.validate_scheme(form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "retirement_centre/scheme_form.html",
                **_form_template_context(True, scheme, form, back_category)
            )

        updated, error = services.update_scheme(
            _db(), scheme, current_user.id, form)
        if error:
            flash(error, "error")
            return redirect(url_for("retirement_centre.scheme_detail",
                                    scheme_id=scheme_id))

        flash("Retirement scheme updated successfully.", "success")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=scheme_id))

    return render_template(
        "retirement_centre/scheme_form.html",
        **_form_template_context(True, scheme, _scheme_to_values(scheme), back_category)
    )


# ── Detail Page ────────────────────────────────────────────────────────────────

@retirement_bp.route("/scheme/<int:scheme_id>")
@login_required
def scheme_detail(scheme_id):
    """
    Full detail view for a single scheme: overview, balance & growth,
    contribution history (Financial Year filter, defaulting to the
    current FY, plus an optional grouping view), nominees, and
    maturity/target-retirement information.
    """
    data = services.get_scheme_with_related(scheme_id, current_user.id)
    if not data:
        abort(404)
    scheme = data["scheme"]

    # FY filter: defaults to the CURRENT financial year unless the
    # user explicitly picks "All Years" (fy=all) or a specific past
    # year (fy=<year>).
    fy_arg = request.args.get("fy", "")
    if fy_arg == "all":
        fy_filter = None
    elif fy_arg.isdigit():
        fy_filter = int(fy_arg)
    else:
        fy_filter = current_financial_year()

    group_by = request.args.get("group_by", "")

    contributions   = services.get_contributions_for_scheme(scheme_id, fy_filter)
    grouped_contributions = services.group_contributions(contributions, group_by)
    available_years = services.available_financial_years(scheme_id)
    fy_options = [
        {"year": y, "label": f"FY {y}-{str(y + 1)[-2:]}"}
        for y in available_years
    ]
    # Ensure the current FY is always selectable, even with 0
    # contributions yet — it's the default view, so it must appear.
    if current_financial_year() not in available_years:
        cy = current_financial_year()
        fy_options.insert(0, {"year": cy, "label": f"FY {cy}-{str(cy + 1)[-2:]}"})
    summary        = services.contribution_summary(scheme_id)
    nominees        = scheme.nominees.order_by(
        RetirementSchemeNominee.created_at.asc()).all()
    documents       = services.get_documents_for_scheme(scheme_id)
    maturity        = services.compute_maturity_info(scheme)
    category        = scheme_type_to_category(scheme.scheme_type)
    category_slug   = category_to_slug(category) if category else None

    return render_template(
        "retirement_centre/scheme_detail.html",
        scheme=scheme,
        category=category,
        category_slug=category_slug,
        timeline=data["timeline"],
        contributions=contributions,
        grouped_contributions=grouped_contributions,
        group_by=group_by,
        fy_options=fy_options,
        fy_filter=fy_filter,
        fy_arg=fy_arg,
        summary=summary,
        nominees=nominees,
        nominee_relations=NomineeRelation.ALL,
        documents=documents,
        doc_types=RetirementDocumentType.ALL,
        maturity=maturity,
        format_inr=format_inr,
        format_date=format_date,
    )


# ── Contribution History (Phase C) ────────────────────────────────────────────

@retirement_bp.route("/scheme/<int:scheme_id>/contributions/add", methods=["POST"])
@login_required
def add_contribution(scheme_id):
    scheme = _get_scheme_or_404(scheme_id)
    form = request.form.to_dict()
    errors = validators.validate_contribution(form)
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        _, error = services.add_contribution(_db(), scheme, current_user.id, form)
        flash(error, "error") if error else flash("Contribution added.", "success")
    return redirect(url_for("retirement_centre.scheme_detail", scheme_id=scheme_id))


@retirement_bp.route("/contributions/<int:contribution_id>/edit", methods=["POST"])
@login_required
def edit_contribution(contribution_id):
    contribution = _get_contribution_or_404(contribution_id)
    scheme_id = contribution.scheme_id
    form = request.form.to_dict()
    errors = validators.validate_contribution(form)
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        _, error = services.update_contribution(
            _db(), contribution, current_user.id, form)
        flash(error, "error") if error else flash("Contribution updated.", "success")
    return redirect(url_for("retirement_centre.scheme_detail", scheme_id=scheme_id))


@retirement_bp.route("/contributions/<int:contribution_id>/delete", methods=["POST"])
@login_required
def delete_contribution(contribution_id):
    contribution = _get_contribution_or_404(contribution_id)
    scheme_id = contribution.scheme_id
    success, error = services.delete_contribution(
        _db(), contribution, current_user.id)
    flash(error, "error") if error else flash("Contribution deleted.", "success")
    return redirect(url_for("retirement_centre.scheme_detail", scheme_id=scheme_id))


# ── Nominees (Phase C) ────────────────────────────────────────────────────────

@retirement_bp.route("/scheme/<int:scheme_id>/nominees/add", methods=["POST"])
@login_required
def add_nominee(scheme_id):
    scheme = _get_scheme_or_404(scheme_id)
    form = request.form.to_dict()
    errors = validators.validate_nominee(form)
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        _, error = services.add_nominee(_db(), scheme, current_user.id, form)
        flash(error, "error") if error else flash("Nominee added.", "success")
    return redirect(url_for("retirement_centre.scheme_detail", scheme_id=scheme_id))


@retirement_bp.route("/nominees/<int:nominee_id>/edit", methods=["POST"])
@login_required
def edit_nominee(nominee_id):
    nominee = _get_nominee_or_404(nominee_id)
    scheme_id = nominee.scheme_id
    form = request.form.to_dict()
    errors = validators.validate_nominee(form)
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        _, error = services.update_nominee(_db(), nominee, current_user.id, form)
        flash(error, "error") if error else flash("Nominee updated.", "success")
    return redirect(url_for("retirement_centre.scheme_detail", scheme_id=scheme_id))


@retirement_bp.route("/nominees/<int:nominee_id>/delete", methods=["POST"])
@login_required
def delete_nominee(nominee_id):
    nominee = _get_nominee_or_404(nominee_id)
    scheme_id = nominee.scheme_id
    success, error = services.delete_nominee(_db(), nominee, current_user.id)
    flash(error, "error") if error else flash("Nominee deleted.", "success")
    return redirect(url_for("retirement_centre.scheme_detail", scheme_id=scheme_id))


# ── Documents (Phase D) ────────────────────────────────────────────────────────

@retirement_bp.route("/scheme/<int:scheme_id>/documents/upload", methods=["POST"])
@login_required
def upload_document(scheme_id):
    """Upload a document to a scheme. Validates ownership and file."""
    scheme   = _get_scheme_or_404(scheme_id)
    file     = request.files.get("document")
    doc_type = request.form.get("doc_type", "").strip()
    title    = request.form.get("doc_title", "").strip() or None
    notes    = request.form.get("doc_notes", "").strip() or None

    errors = validators.validate_document(file, doc_type)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=scheme_id))

    try:
        stored_name, file_path, file_size = save_document_file(file, scheme_id)
    except OSError as e:
        flash(f"File could not be saved: {e}", "error")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=scheme_id))

    services.save_document_metadata(
        _db(), scheme, current_user.id,
        doc_type=doc_type, original_name=file.filename,
        stored_name=stored_name, file_path=file_path,
        file_size=file_size, notes=notes, title=title,
    )
    flash("Document uploaded successfully!", "success")
    return redirect(url_for("retirement_centre.scheme_detail",
                            scheme_id=scheme_id))


@retirement_bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    """Delete document — removes file and metadata. Validates ownership.
    Redirects back to wherever the delete was triggered from (scheme
    detail page or the Document Vault) via a 'next' form field."""
    doc = _get_document_or_404(doc_id)
    scheme_id = doc.scheme_id
    next_target = request.form.get("next", "scheme_detail")
    redirect_url = (url_for("retirement_centre.document_vault")
                    if next_target == "vault"
                    else url_for("retirement_centre.scheme_detail", scheme_id=scheme_id))

    if doc.file_path and not secure_file_path(doc.file_path, scheme_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(redirect_url)

    delete_document_file(doc.file_path)
    services.delete_document(_db(), doc, current_user.id)
    flash("Document deleted.", "success")
    return redirect(redirect_url)


@retirement_bp.route("/documents/<int:doc_id>/download")
@login_required
def download_document(doc_id):
    """Download document — validates ownership, preserves original filename."""
    doc = _get_document_or_404(doc_id)

    import os
    if not doc.file_path or not os.path.exists(doc.file_path):
        flash("File not found.", "error")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=doc.scheme_id))
    if not secure_file_path(doc.file_path, doc.scheme_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=doc.scheme_id))

    return send_file(doc.file_path, as_attachment=True,
                     download_name=doc.original_name)


@retirement_bp.route("/documents/<int:doc_id>/preview")
@login_required
def preview_document(doc_id):
    """Preview document inline in browser (PDF and images only)."""
    doc = _get_document_or_404(doc_id)

    import os
    if not doc.file_path or not os.path.exists(doc.file_path):
        flash("File not found.", "error")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=doc.scheme_id))
    if not secure_file_path(doc.file_path, doc.scheme_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=doc.scheme_id))
    if not is_previewable(doc.original_name):
        flash("Preview not available for this file type.", "error")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=doc.scheme_id))

    return send_file(doc.file_path, mimetype=get_preview_mimetype(doc.original_name),
                     as_attachment=False, download_name=doc.original_name)


# ── Document Vault ─────────────────────────────────────────────────────────────

@retirement_bp.route("/documents")
@login_required
def document_vault():
    """
    Document Vault — first-class page listing every document across
    all of the user's retirement schemes, with search and filters.
    """
    from datetime import datetime as _dt
    from .models import RETIREMENT_CATEGORY_ORDER, RetirementDocumentType

    q          = request.args.get("q", "").strip()
    category   = request.args.get("category", "")
    doc_type   = request.args.get("doc_type", "")
    date_from_raw = request.args.get("date_from", "")
    date_to_raw   = request.args.get("date_to", "")

    def _parse(d):
        try:
            return _dt.strptime(d, "%Y-%m-%d")
        except (ValueError, TypeError):
            return None

    documents = services.get_vault_documents(
        current_user.id, q=q or None, category=category or None,
        doc_type=doc_type or None,
        date_from=_parse(date_from_raw), date_to=_parse(date_to_raw),
    )
    summary = services.vault_summary(current_user.id)

    # Schemes for the upload form's scheme dropdown (active schemes only)
    upload_schemes = (RetirementScheme.query
                      .filter_by(user_id=current_user.id, is_archived=False)
                      .order_by(RetirementScheme.scheme_type).all())
    preselect_scheme_id = request.args.get("scheme_id", type=int)

    return render_template(
        "retirement_centre/document_vault.html",
        documents=documents,
        summary=summary,
        categories=RETIREMENT_CATEGORY_ORDER,
        doc_types=RetirementDocumentType.ALL,
        upload_schemes=upload_schemes,
        preselect_scheme_id=preselect_scheme_id,
        q=q, category=category, doc_type=doc_type,
        date_from=date_from_raw, date_to=date_to_raw,
        format_inr=format_inr, format_date=format_date,
    )


@retirement_bp.route("/documents/upload", methods=["POST"])
@login_required
def upload_document_vault():
    """
    Upload a document from the Vault directly — the scheme is chosen
    via a form dropdown rather than implied by the URL (Phase: Document
    Vault, Part 4). Reuses the exact same storage/validation logic as
    the scheme-scoped upload route — no parallel storage system.
    """
    scheme_id = request.form.get("scheme_id", type=int)
    if not scheme_id:
        flash("Please select a retirement scheme for this document.", "error")
        return redirect(url_for("retirement_centre.document_vault"))

    scheme = RetirementScheme.query.filter_by(
        id=scheme_id, user_id=current_user.id).first()
    if not scheme:
        flash("Invalid scheme selected.", "error")
        return redirect(url_for("retirement_centre.document_vault"))

    file     = request.files.get("document")
    doc_type = request.form.get("doc_type", "").strip()
    title    = request.form.get("doc_title", "").strip() or None
    notes    = request.form.get("doc_notes", "").strip() or None

    errors = validators.validate_document(file, doc_type)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("retirement_centre.document_vault"))

    try:
        stored_name, file_path, file_size = save_document_file(file, scheme_id)
    except OSError as e:
        flash(f"File could not be saved: {e}", "error")
        return redirect(url_for("retirement_centre.document_vault"))

    services.save_document_metadata(
        _db(), scheme, current_user.id,
        doc_type=doc_type, original_name=file.filename,
        stored_name=stored_name, file_path=file_path,
        file_size=file_size, notes=notes, title=title,
    )
    flash("Document uploaded successfully!", "success")
    return redirect(url_for("retirement_centre.document_vault"))


# ── PDF Export (Phase D) ──────────────────────────────────────────────────────

@retirement_bp.route("/export/pdf")
@login_required
def export_pdf():
    """Export a complete retirement summary PDF for the current user only."""
    from . import export as export_module
    return export_module.build_retirement_pdf(current_user)


# ── Archive / Restore ─────────────────────────────────────────────────────────

@retirement_bp.route("/scheme/<int:scheme_id>/archive", methods=["POST"])
@login_required
def archive_scheme(scheme_id):
    """Soft-delete — move scheme to archived status."""
    scheme = _get_scheme_or_404(scheme_id)
    success, error = services.archive_scheme(_db(), scheme, current_user.id)
    if error:
        flash(error, "error")
    else:
        flash("Scheme archived. You can restore it any time.", "success")
    return redirect(url_for("retirement_centre.scheme_detail",
                            scheme_id=scheme_id))


@retirement_bp.route("/scheme/<int:scheme_id>/restore", methods=["POST"])
@login_required
def restore_scheme(scheme_id):
    """Restore an archived scheme."""
    scheme = _get_scheme_or_404(scheme_id)
    success, error = services.restore_scheme(_db(), scheme, current_user.id)
    if error:
        flash(error, "error")
    else:
        flash("Scheme restored successfully.", "success")
    return redirect(url_for("retirement_centre.scheme_detail",
                            scheme_id=scheme_id))


@retirement_bp.route("/scheme/<int:scheme_id>/delete", methods=["POST"])
@login_required
def delete_scheme_permanent(scheme_id):
    """
    Permanently delete an ARCHIVED scheme only (Phase F, Section 9).
    Removes associated document files from disk first, then deletes
    the scheme row — cascades to contributions, balance snapshots,
    nominees, documents, and timeline (all set to CASCADE in models.py).
    """
    scheme = _get_scheme_or_404(scheme_id)
    if not scheme.is_archived:
        flash("Only archived schemes can be permanently deleted.", "error")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=scheme_id))

    for doc in scheme.documents.all():
        delete_document_file(doc.file_path)

    _db().session.delete(scheme)
    _db().session.commit()
    flash("Scheme permanently deleted.", "success")
    return redirect(url_for("retirement_centre.manage_schemes", status="archived"))


# ── Category View ──────────────────────────────────────────────────────────────

@retirement_bp.route("/category/<slug>")
@login_required
def category_view(slug):
    """
    Dedicated category page — shows only schemes for one of the 5
    display categories, mirroring insurance_centre's category_view
    route/page exactly. Distinct from Manage Schemes: this is a
    focused single-category view, not a general filterable listing.
    """
    from .models import (RETIREMENT_CATEGORY_GROUPS, slug_to_category,
                         RETIREMENT_CATEGORY_FULL_NAMES)

    category = slug_to_category(slug)
    if category not in RETIREMENT_CATEGORY_GROUPS:
        abort(404)

    allowed_types = RETIREMENT_CATEGORY_GROUPS[category]
    schemes = (RetirementScheme.query
               .filter_by(user_id=current_user.id, is_archived=False)
               .filter(RetirementScheme.scheme_type.in_(allowed_types))
               .order_by(RetirementScheme.created_at.desc())
               .all())

    for s in schemes:
        s._maturity = services.compute_maturity_info(s)
        s._fy_contrib = services.contribution_summary(s.id)["current_fy_total"]

    total_balance = sum(s.current_balance or 0 for s in schemes)

    # Preselect scheme_type for the "+ Add Scheme" button on this page.
    # "Other Retirement Schemes" maps to the CUSTOM type — that's the
    # only value the simplified 5-option Add form dropdown now offers
    # for this category, so preselecting it (not leaving it blank)
    # is correct.
    if category == "EPF / VPF":
        preset_type = "EPF"
    elif category == "Other Retirement Schemes":
        preset_type = "Other (Custom)"
    else:
        preset_type = category

    icon_map = {"EPF / VPF": "🏢", "PPF": "🏦", "NPS": "📈",
               "SSY": "👧", "Other Retirement Schemes": "📋"}

    return render_template(
        "retirement_centre/category_view.html",
        category=category,
        full_name=RETIREMENT_CATEGORY_FULL_NAMES.get(category, category),
        icon=icon_map.get(category, "📋"),
        schemes=schemes,
        total_balance=total_balance,
        preset_type=preset_type,
        format_inr=format_inr,
        format_date=format_date,
        mask_account_number=mask_account_number,
    )


# ── Manage Schemes Listing (Phase F) ──────────────────────────────────────────

@retirement_bp.route("/schemes")
@login_required
def manage_schemes():
    """
    Full scheme listing with search, category filter, status
    (Active/Archived) tabs, and sorting. Also serves as the
    category-filtered view when arriving from a dashboard category
    card click (Phase F, Sections 14 & 15) — one implementation for
    both, per the "reuse existing architecture" instruction.
    """
    from .models import RETIREMENT_CATEGORY_GROUPS, RETIREMENT_CATEGORY_ORDER

    q             = request.args.get("q", "").strip()
    category      = request.args.get("category", "")
    status_filter = request.args.get("status", "active")
    sort_by       = request.args.get("sort", "recent")

    query = RetirementScheme.query.filter_by(user_id=current_user.id)
    query = query.filter_by(is_archived=(status_filter == "archived"))
    schemes = query.all()

    if category and category in RETIREMENT_CATEGORY_GROUPS:
        allowed_types = RETIREMENT_CATEGORY_GROUPS[category]
        schemes = [s for s in schemes if s.scheme_type in allowed_types]

    if q:
        ql = q.lower()
        schemes = [s for s in schemes
                  if ql in (s.display_type or "").lower()
                  or ql in (s.institution or "").lower()
                  or ql in (s.account_number or "").lower()]

    if sort_by == "balance_high":
        schemes.sort(key=lambda s: s.current_balance or 0, reverse=True)
    elif sort_by == "balance_low":
        schemes.sort(key=lambda s: s.current_balance or 0)
    elif sort_by == "name":
        schemes.sort(key=lambda s: s.display_type.lower())
    else:
        schemes.sort(key=lambda s: s.created_at, reverse=True)

    for s in schemes:
        s._maturity = services.compute_maturity_info(s)
        s._fy_contrib = services.contribution_summary(s.id)["current_fy_total"]

    return render_template(
        "retirement_centre/manage_schemes.html",
        schemes=schemes,
        categories=RETIREMENT_CATEGORY_ORDER,
        category=category,
        status_filter=status_filter,
        q=q,
        sort_by=sort_by,
        format_inr=format_inr,
        format_date=format_date,
        mask_account_number=mask_account_number,
    )
