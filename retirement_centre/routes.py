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
)
from .utils import (
    format_inr, format_date, financial_year_bounds,
    save_document_file, delete_document_file, secure_file_path,
    is_previewable, get_preview_mimetype,
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
    data (Section 20 of the Phase A spec, still true in Phase D).
    """
    stats = services.RetirementStatisticsService(current_user.id)
    data  = stats.summary_dict()

    return render_template(
        "retirement_centre/dashboard.html",
        data=data,
        scheme_types=SchemeType.ALL,
        format_inr=format_inr,
        format_date=format_date,
    )


# ── Add / Edit Scheme (shared form) ───────────────────────────────────────────

def _form_template_context(is_edit, scheme, values):
    """Shared kwargs for rendering scheme_form.html in either mode."""
    return dict(
        is_edit=is_edit,
        scheme=scheme,
        values=values,
        scheme_types=SchemeType.ALL,
        growth_methods=GrowthMethod.ALL,
        growth_method_labels=GrowthMethod.LABELS,
        statuses=SchemeStatus.ALL,
        contribution_preferences=ContributionPreference.ALL,
        nps_tiers=NPSTier.ALL,
        field_groups=SchemeType.FIELD_GROUPS,
    )


@retirement_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_scheme():
    """Add a new retirement scheme."""
    if request.method == "POST":
        form = request.form.to_dict()
        errors = validators.validate_scheme(form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "retirement_centre/scheme_form.html",
                **_form_template_context(False, None, form)
            )

        scheme, error = services.create_scheme(_db(), current_user.id, form)
        if error:
            flash(error, "error")
            return redirect(url_for("retirement_centre.add_scheme"))

        flash("Retirement scheme added successfully.", "success")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=scheme.id))

    return render_template(
        "retirement_centre/scheme_form.html",
        **_form_template_context(False, None, {})
    )


@retirement_bp.route("/scheme/<int:scheme_id>/edit", methods=["GET", "POST"])
@login_required
def edit_scheme(scheme_id):
    """Edit an existing scheme (active or archived) — reuses the add form."""
    scheme = _get_scheme_or_404(scheme_id)

    if request.method == "POST":
        form = request.form.to_dict()
        errors = validators.validate_scheme(form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "retirement_centre/scheme_form.html",
                **_form_template_context(True, scheme, form)
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
        **_form_template_context(True, scheme, _scheme_to_values(scheme))
    )


# ── Detail Page ────────────────────────────────────────────────────────────────

@retirement_bp.route("/scheme/<int:scheme_id>")
@login_required
def scheme_detail(scheme_id):
    """
    Full detail view for a single scheme: overview, balance & growth,
    contribution history (with Financial Year filter), balance history,
    nominees, and maturity/target-retirement information.
    """
    data = services.get_scheme_with_related(scheme_id, current_user.id)
    if not data:
        abort(404)
    scheme = data["scheme"]

    fy_arg = request.args.get("fy", "")
    fy_filter = int(fy_arg) if fy_arg.isdigit() else None

    contributions   = services.get_contributions_for_scheme(scheme_id, fy_filter)
    available_years = services.available_financial_years(scheme_id)
    fy_options = [
        {"year": y, "label": f"FY {y}-{str(y + 1)[-2:]}"}
        for y in available_years
    ]
    summary        = services.contribution_summary(scheme_id)
    balance_history = services.get_balance_history(scheme_id)
    nominees        = scheme.nominees.order_by(
        RetirementSchemeNominee.created_at.asc()).all()
    documents       = services.get_documents_for_scheme(scheme_id)
    maturity        = services.compute_maturity_info(scheme)

    return render_template(
        "retirement_centre/scheme_detail.html",
        scheme=scheme,
        timeline=data["timeline"],
        contributions=contributions,
        fy_options=fy_options,
        fy_filter=fy_filter,
        summary=summary,
        balance_history=balance_history,
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


# ── Balance Snapshots (Phase C) ───────────────────────────────────────────────

@retirement_bp.route("/scheme/<int:scheme_id>/balance/update", methods=["POST"])
@login_required
def update_balance(scheme_id):
    scheme = _get_scheme_or_404(scheme_id)
    form = request.form.to_dict()
    errors = validators.validate_balance_update(form)
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        _, error = services.update_balance(_db(), scheme, current_user.id, form)
        flash(error, "error") if error else flash("Balance updated.", "success")
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
        file_size=file_size, notes=notes,
    )
    flash("Document uploaded successfully!", "success")
    return redirect(url_for("retirement_centre.scheme_detail",
                            scheme_id=scheme_id))


@retirement_bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    """Delete document — removes file and metadata. Validates ownership."""
    doc = _get_document_or_404(doc_id)
    scheme_id = doc.scheme_id

    if doc.file_path and not secure_file_path(doc.file_path, scheme_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("retirement_centre.scheme_detail",
                                scheme_id=scheme_id))

    delete_document_file(doc.file_path)
    services.delete_document(_db(), doc, current_user.id)
    flash("Document deleted.", "success")
    return redirect(url_for("retirement_centre.scheme_detail",
                            scheme_id=scheme_id))


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
