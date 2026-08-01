"""
Insurance Centre — Routes
==========================
Lightweight route handlers. All business logic is in services.py.
Routes only handle: request parsing, auth, calling services, response.
"""

from flask import (
    render_template, redirect, url_for, request,
    flash, jsonify, send_file, abort, current_app
)
from flask_login import login_required, current_user
from . import insurance_bp
from .models import (
    InsurancePolicy, InsuranceNominee, InsuranceMember,
    InsuranceAddon, InsuranceDocument, InsuranceTimeline,
    InsuranceCategory, InsuranceType, PolicyStatus,
    PremiumFrequency, NomineeRelation, MemberRelation,
    MotorAddonType, DocumentType, TimelineEvent
)
from . import services


def _db():
    from models import db
    return db

from . import validators
from .utils import (
    save_document_file, delete_document_file,
    renewal_badge, format_inr, format_date,
    category_to_slug, slug_to_category
)




# ── Template Helpers ──────────────────────────────────────────────────────────

def _build_types_json():
    """Build JSON of insurance types per category for JS dropdown."""
    import json
    return json.dumps({
        InsuranceCategory.LIFE:     InsuranceType.LIFE,
        InsuranceCategory.HEALTH:   InsuranceType.HEALTH,
        InsuranceCategory.MOTOR:    InsuranceType.MOTOR,
        InsuranceCategory.PROPERTY: InsuranceType.PROPERTY,
        InsuranceCategory.GENERAL:  InsuranceType.GENERAL,
    })


def _category_icons():
    return {
        "Life Insurance":     "🛡️",
        "Health Insurance":   "🏥",
        "Motor Insurance":    "🚗",
        "Property Insurance": "🏠",
        "General Insurance":  "📋",
    }

# ── Dashboard ─────────────────────────────────────────────────────────────────

@insurance_bp.route("/")
@login_required
def dashboard():
    """Insurance Centre dashboard — aggregated overview."""
    data = services.get_dashboard_data(current_user.id)

    # Attach renewal badge to recent policies
    for p in data["recent_policies"]:
        p._badge_label, p._badge_color = renewal_badge(p)

    return render_template(
        "insurance_centre/dashboard.html",
        data=data,
        categories=InsuranceCategory.ALL,
        category_to_slug=category_to_slug,
        category_icons=_category_icons(),
        format_inr=format_inr,
        format_date=format_date,
    )


# ── Policy CRUD ───────────────────────────────────────────────────────────────

@insurance_bp.route("/add", methods=["GET", "POST"])
@login_required
def add_policy():
    """Add a new insurance policy."""
    if request.method == "POST":
        policy, error = services.create_policy(
            _db(), current_user.id,
            request.form.to_dict(),
            multi_data=request.form
        )
        if error:
            flash(error, "error")
            return render_template(
                "insurance_centre/add_policy.html",
                categories=InsuranceCategory.ALL,
                insurance_types_json=_build_types_json(),
                policy_statuses=PolicyStatus.ALL,
                premium_frequencies=PremiumFrequency.ALL,
                nominee_relations=NomineeRelation.ALL,
                member_relations=MemberRelation.ALL,
                motor_addons=MotorAddonType.ALL,
                category_icons=_category_icons(),
            )

        flash("Policy added successfully!", "success")
        return redirect(url_for("insurance_centre.dashboard"))

    return render_template(
        "insurance_centre/add_policy.html",
        categories=InsuranceCategory.ALL,
        insurance_types_json=_build_types_json(),
        policy_statuses=PolicyStatus.ALL,
        premium_frequencies=PremiumFrequency.ALL,
        nominee_relations=NomineeRelation.ALL,
        member_relations=MemberRelation.ALL,
        motor_addons=MotorAddonType.ALL,
        category_icons=_category_icons(),
    )



@insurance_bp.route("/policies")
@login_required
def policy_listing():
    """View all active policies."""
    policies = services.get_all_active_policies_for_listing(current_user.id)
    return render_template(
        "insurance_centre/policy_listing.html",
        policies=policies,
        category_icons=_category_icons(),
        format_inr=format_inr,
        format_date=format_date,
    )

@insurance_bp.route("/policy/<int:policy_id>")
@login_required
def policy_detail(policy_id):
    """Full detail view for a single policy."""
    data = services.get_policy_with_related(policy_id, current_user.id)
    if not data:
        abort(404)

    return render_template(
        "insurance_centre/policy_detail.html",
        policy=data["policy"],
        nominees=data["nominees"],
        members=data["members"],
        addons=data["addons"],
        documents=data["documents"],
        timeline=data["timeline"],
        category_icons=_category_icons(),
        format_inr=format_inr,
        format_date=format_date,
        renewal_badge=renewal_badge,
        doc_types=DocumentType.ALL,
    )

@insurance_bp.route("/policy/<int:policy_id>/edit", methods=["GET", "POST"])
@login_required
def edit_policy(policy_id):
    """Edit an existing policy — reuses add_policy form."""
    policy = _get_policy_or_404(policy_id)

    if request.method == "POST":
        updated, error = services.update_policy(
            _db(), policy, current_user.id,
            request.form.to_dict(),
            multi_data=request.form
        )
        if error:
            flash(error, "error")
            return render_template(
                "insurance_centre/add_policy.html",
                policy=policy,
                is_edit=True,
                categories=InsuranceCategory.ALL,
                insurance_types_json=_build_types_json(),
                policy_statuses=PolicyStatus.ALL,
                premium_frequencies=PremiumFrequency.ALL,
                nominee_relations=NomineeRelation.ALL,
                member_relations=MemberRelation.ALL,
                motor_addons=MotorAddonType.ALL,
                category_icons=_category_icons(),
                existing_nominees=policy.nominees.all(),
                existing_members=policy.members.all(),
                existing_addons=[a.addon_name for a in policy.addons.all()],
            )
        flash("Policy updated successfully!", "success")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))

    return render_template(
        "insurance_centre/add_policy.html",
        policy=policy,
        is_edit=True,
        categories=InsuranceCategory.ALL,
        insurance_types_json=_build_types_json(),
        policy_statuses=PolicyStatus.ALL,
        premium_frequencies=PremiumFrequency.ALL,
        nominee_relations=NomineeRelation.ALL,
        member_relations=MemberRelation.ALL,
        motor_addons=MotorAddonType.ALL,
        category_icons=_category_icons(),
        existing_nominees=policy.nominees.all(),
        existing_members=policy.members.all(),
        existing_addons=[a.addon_name for a in policy.addons.all()],
    )

@insurance_bp.route("/policy/<int:policy_id>/archive", methods=["POST"])
@login_required
def archive_policy(policy_id):
    """Soft-delete — move policy to archive."""
    policy = _get_policy_or_404(policy_id)
    success, error = services.archive_policy(_db(), policy, current_user.id)
    if error:
        flash(error, "error")
    else:
        flash(f"Policy archived. You can restore it from the Archive.", "success")
    return redirect(url_for("insurance_centre.policy_listing"))

@insurance_bp.route("/policy/<int:policy_id>/restore", methods=["POST"])
@login_required
def restore_policy(policy_id):
    """Restore an archived policy."""
    policy = InsurancePolicy.query.filter_by(
        id=policy_id, user_id=current_user.id).first_or_404()
    success, error = services.restore_policy(_db(), policy, current_user.id)
    if error:
        flash(error, "error")
    else:
        flash("Policy restored successfully!", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy_id))

@insurance_bp.route("/policy/<int:policy_id>/renew", methods=["POST"])
@login_required
def renew_policy(policy_id):
    """Record a policy renewal."""
    policy = _get_policy_or_404(policy_id)
    errors = validators.validate_renewal(request.form.to_dict(), policy)
    if errors:
        flash(errors[0], "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))

    from .utils import _parse_date
    from datetime import datetime
    new_renewal = datetime.strptime(
        request.form.get("new_renewal_date"), "%Y-%m-%d").date()
    new_expiry  = request.form.get("new_expiry_date")
    new_expiry  = datetime.strptime(new_expiry, "%Y-%m-%d").date() \
                  if new_expiry else None

    services.renew_policy(_db(), policy, current_user.id,
                          new_renewal, new_expiry)
    flash("Policy renewed successfully!", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy_id))




# ── Category Placeholder ──────────────────────────────────────────────────────

@insurance_bp.route("/category/<slug>")
@login_required
def category_view(slug):
    """Placeholder — detailed category view coming in Phase 4."""
    from .models import InsuranceCategory
    category = slug_to_category(slug)
    if category not in InsuranceCategory.ALL:
        abort(404)
    policies = services.get_policies_by_category(current_user.id, category)
    return render_template(
        "insurance_centre/category_placeholder.html",
        category=category,
        policies=policies,
        category_icons=_category_icons(),
        format_inr=format_inr,
        format_date=format_date,
    )

# ── Archive View ──────────────────────────────────────────────────────────────

@insurance_bp.route("/archive")
@login_required
def archive_listing():
    """View all archived policies."""
    archived = services.get_archived_policies(current_user.id)
    return render_template(
        "insurance_centre/archive_listing.html",
        policies=archived,
        category_icons=_category_icons(),
        format_inr=format_inr,
        format_date=format_date,
    )


@insurance_bp.route("/archive_old")
@login_required
def archive_old():
    """View all archived policies."""
    archived = services.get_archived_policies(current_user.id)
    return render_template(
        "insurance_centre/archive.html",
        policies=archived,
        format_inr=format_inr,
        format_date=format_date,
    )


# ── Nominees ──────────────────────────────────────────────────────────────────

@insurance_bp.route("/policy/<int:policy_id>/nominees/add",
                    methods=["POST"])
@login_required
def add_nominee(policy_id):
    policy = _get_policy_or_404(policy_id)
    nominee, error = services.add_nominee(
        _db(), policy, current_user.id, request.form.to_dict()
    )
    if error:
        flash(error, "error")
    else:
        flash("Nominee added.", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy_id))


@insurance_bp.route("/nominees/<int:nominee_id>/delete",
                    methods=["POST"])
@login_required
def delete_nominee(nominee_id):
    nominee = InsuranceNominee.query.get_or_404(nominee_id)
    policy  = _get_policy_or_404(nominee.policy_id)
    services.remove_nominee(_db(), nominee, current_user.id)
    flash("Nominee removed.", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy.id))


# ── Health Members ────────────────────────────────────────────────────────────

@insurance_bp.route("/policy/<int:policy_id>/members/add",
                    methods=["POST"])
@login_required
def add_member(policy_id):
    policy = _get_policy_or_404(policy_id)
    member, error = services.add_member(
        _db(), policy, current_user.id, request.form.to_dict()
    )
    if error:
        flash(error, "error")
    else:
        flash("Member added.", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy_id))


@insurance_bp.route("/members/<int:member_id>/delete",
                    methods=["POST"])
@login_required
def delete_member(member_id):
    member = InsuranceMember.query.get_or_404(member_id)
    policy = _get_policy_or_404(member.policy_id)
    services.remove_member(_db(), member, current_user.id)
    flash("Member removed.", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy.id))


# ── Motor Add-ons ─────────────────────────────────────────────────────────────

@insurance_bp.route("/policy/<int:policy_id>/addons/update",
                    methods=["POST"])
@login_required
def update_addons(policy_id):
    policy = _get_policy_or_404(policy_id)
    addon_names = request.form.getlist("addons")
    success, error = services.set_addons(
        _db(), policy, current_user.id, addon_names
    )
    if error:
        flash(error, "error")
    else:
        flash("Add-ons updated.", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy_id))


# ── Documents ─────────────────────────────────────────────────────────────────

@insurance_bp.route("/policy/<int:policy_id>/documents/upload",
                    methods=["POST"])
@login_required
def upload_document(policy_id):
    """Upload a document to a policy. Validates ownership and file."""
    policy   = _get_policy_or_404(policy_id)
    file     = request.files.get("document")
    doc_type = request.form.get("doc_type", "").strip()
    notes    = request.form.get("doc_notes", "").strip() or None

    errors = validators.validate_document(file, doc_type)
    if errors:
        flash(errors[0], "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))
    try:
        from .utils import save_document_file
        stored_name, file_path, file_size = save_document_file(file, policy_id)
    except OSError as e:
        flash(f"File could not be saved: {e}", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))

    services.save_document_metadata(
        _db(), policy, current_user.id,
        doc_type      = doc_type,
        original_name = file.filename,
        stored_name   = stored_name,
        file_path     = file_path,
        file_size     = file_size,
        notes         = notes,
    )
    flash("Document uploaded successfully!", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy_id))

@insurance_bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    """Delete document — removes file and metadata. Validates ownership."""
    from insurance_centre.models import InsuranceDocument
    doc = InsuranceDocument.query.filter_by(
        id=doc_id, user_id=current_user.id).first_or_404()
    policy_id = doc.policy_id

    # Security: verify file is within expected directory
    from .utils import delete_document_file, secure_file_path
    if doc.file_path and not secure_file_path(doc.file_path, policy_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))

    delete_document_file(doc.file_path)
    services.delete_document(_db(), doc, current_user.id)
    flash("Document deleted.", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy_id))

@insurance_bp.route("/documents/<int:doc_id>/download")
@login_required
def download_document(doc_id):
    """Download document — validates ownership, preserves original filename."""
    from insurance_centre.models import InsuranceDocument
    doc = InsuranceDocument.query.filter_by(
        id=doc_id, user_id=current_user.id).first_or_404()

    import os
    if not doc.file_path or not os.path.exists(doc.file_path):
        flash("File not found on server.", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=doc.policy_id))

    # Security: verify path is within expected directory
    from .utils import secure_file_path
    if not secure_file_path(doc.file_path, doc.policy_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=doc.policy_id))

    return send_file(
        doc.file_path,
        as_attachment=True,
        download_name=doc.original_name,  # original name, not UUID
    )


@insurance_bp.route("/documents/<int:doc_id>/preview")
@login_required
def preview_document(doc_id):
    """Preview document inline in browser (PDF and images only)."""
    from insurance_centre.models import InsuranceDocument
    doc = InsuranceDocument.query.filter_by(
        id=doc_id, user_id=current_user.id).first_or_404()

    import os
    from .utils import is_previewable, get_preview_mimetype, secure_file_path

    if not doc.file_path or not os.path.exists(doc.file_path):
        flash("File not found on server.", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=doc.policy_id))

    if not secure_file_path(doc.file_path, doc.policy_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=doc.policy_id))

    if not is_previewable(doc.original_name):
        flash("Preview not available for this file type.", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=doc.policy_id))

    mimetype = get_preview_mimetype(doc.original_name)
    return send_file(
        doc.file_path,
        mimetype=mimetype,
        as_attachment=False,    # inline — opens in browser
        download_name=doc.original_name,
    )

@insurance_bp.route("/search")
@login_required
def search():
    query = request.args.get("q", "").strip()
    results = services.search_policies(current_user.id, query) if query else []
    return render_template(
        "insurance_centre/search.html",
        query=query,
        results=results,
        format_inr=format_inr,
        format_date=format_date,
    )


# ── API — types for dynamic dropdown ─────────────────────────────────────────

@insurance_bp.route("/api/types")
@login_required
def api_types():
    """Return insurance types for a given category (used by JS dropdown)."""
    category = request.args.get("category", "")
    types = InsuranceType.for_category(category)
    return jsonify({"types": types})


# ── Private Helpers ───────────────────────────────────────────────────────────

def _get_policy_or_404(policy_id):
    """Get policy — must belong to current user and not be archived."""
    return InsurancePolicy.query.filter_by(
        id=policy_id,
        user_id=current_user.id,
    ).first_or_404()


def init_routes(bp):
    """Register all routes onto the blueprint."""
    # All routes are already registered via decorators above
    pass
