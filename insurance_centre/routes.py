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
    renewal_badge, format_inr, format_date
)


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
        category_icons={
            "Life Insurance":     "🛡️",
            "Health Insurance":   "🏥",
            "Motor Insurance":    "🚗",
            "Property Insurance": "🏠",
            "General Insurance":  "📋",
        },
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
            _db(), current_user.id, request.form.to_dict()
        )
        if error:
            flash(error, "error")
            return redirect(url_for("insurance_centre.add_policy"))

        flash("Policy added successfully!", "success")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy.id))

    return render_template(
        "insurance_centre/add_policy.html",
        categories=InsuranceCategory.ALL,
        insurance_types=InsuranceType,
        policy_statuses=PolicyStatus.ALL,
        premium_frequencies=PremiumFrequency.ALL,
    )


@insurance_bp.route("/policy/<int:policy_id>")
@login_required
def policy_detail(policy_id):
    """Full detail view for a single policy."""
    policy = _get_policy_or_404(policy_id)

    nominees  = policy.nominees.all()
    members   = policy.members.all()
    addons    = policy.addons.all()
    documents = policy.documents.order_by(
                    InsuranceDocument.uploaded_at.desc()).all()
    timeline  = policy.timeline.limit(20).all()

    return render_template(
        "insurance_centre/policy_detail.html",
        policy=policy,
        nominees=nominees,
        members=members,
        addons=addons,
        documents=documents,
        timeline=timeline,
        nominee_relations=NomineeRelation.ALL,
        member_relations=MemberRelation.ALL,
        motor_addons=MotorAddonType.ALL,
        doc_types=DocumentType.ALL,
        format_inr=format_inr,
        format_date=format_date,
        renewal_badge=renewal_badge,
    )


@insurance_bp.route("/policy/<int:policy_id>/edit", methods=["GET", "POST"])
@login_required
def edit_policy(policy_id):
    """Edit an existing policy."""
    policy = _get_policy_or_404(policy_id)

    if request.method == "POST":
        policy, error = services.update_policy(
            db, policy, current_user.id, request.form.to_dict()
        )
        if error:
            flash(error, "error")
            return redirect(url_for("insurance_centre.edit_policy",
                                    policy_id=policy_id))

        flash("Policy updated successfully!", "success")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))

    return render_template(
        "insurance_centre/edit_policy.html",
        policy=policy,
        categories=InsuranceCategory.ALL,
        insurance_types=InsuranceType,
        policy_statuses=PolicyStatus.ALL,
        premium_frequencies=PremiumFrequency.ALL,
        format_date=format_date,
    )


@insurance_bp.route("/policy/<int:policy_id>/archive", methods=["POST"])
@login_required
def archive_policy(policy_id):
    """Soft-delete a policy."""
    policy = _get_policy_or_404(policy_id)
    success, error = services.archive_policy(_db(), policy, current_user.id)
    if error:
        flash(error, "error")
    else:
        flash("Policy archived. You can restore it anytime.", "success")
    return redirect(url_for("insurance_centre.dashboard"))


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

@insurance_bp.route("/category/<category>")
@login_required
def category_view(category):
    """Placeholder — detailed category view coming in Phase 4."""
    from .models import InsuranceCategory
    if category not in InsuranceCategory.ALL:
        abort(404)
    policies = services.get_policies_by_category(current_user.id, category)
    return render_template(
        "insurance_centre/category_placeholder.html",
        category=category,
        policies=policies,
        category_icons={
            "Life Insurance":     "🛡️",
            "Health Insurance":   "🏥",
            "Motor Insurance":    "🚗",
            "Property Insurance": "🏠",
            "General Insurance":  "📋",
        },
        format_inr=format_inr,
        format_date=format_date,
    )

# ── Archive View ──────────────────────────────────────────────────────────────

@insurance_bp.route("/archive")
@login_required
def archive():
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
    policy = _get_policy_or_404(policy_id)
    file     = request.files.get("document")
    doc_type = request.form.get("doc_type", "")
    notes    = request.form.get("notes", "").strip() or None

    errors = validators.validate_document(file, doc_type)
    if errors:
        flash(errors[0], "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))

    try:
        stored_name, file_path, file_size = save_document_file(
            file, policy_id
        )
    except OSError as e:
        flash(f"File save failed: {e}", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))

    services.save_document_metadata(
        _db(), policy, current_user.id,
        doc_type=doc_type,
        original_name=file.filename,
        stored_name=stored_name,
        file_path=file_path,
        file_size=file_size,
        notes=notes,
    )
    flash("Document uploaded successfully!", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy_id))


@insurance_bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    doc = InsuranceDocument.query.filter_by(
        id=doc_id, user_id=current_user.id).first_or_404()
    policy_id = doc.policy_id
    delete_document_file(doc.file_path)
    services.delete_document(_db(), doc, current_user.id)
    flash("Document deleted.", "success")
    return redirect(url_for("insurance_centre.policy_detail",
                            policy_id=policy_id))


@insurance_bp.route("/documents/<int:doc_id>/download")
@login_required
def download_document(doc_id):
    doc = InsuranceDocument.query.filter_by(
        id=doc_id, user_id=current_user.id).first_or_404()
    import os
    if not os.path.exists(doc.file_path):
        flash("File not found on server.", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=doc.policy_id))
    return send_file(doc.file_path,
                     as_attachment=True,
                     download_name=doc.original_name)


# ── Search ────────────────────────────────────────────────────────────────────

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
