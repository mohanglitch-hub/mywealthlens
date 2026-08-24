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


# ── Template Helpers ────────────────────────────────────────────────────────

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
    """Insurance Centre dashboard — uses InsuranceStatisticsService."""
    stats = services.InsuranceStatisticsService(current_user.id)
    data  = stats.summary_dict()

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

    # Allow pre-selecting category from URL (e.g. from empty category card)
    preset_category = request.args.get("preset_category", "")

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
        preset_category=preset_category,
    )


@insurance_bp.route("/policies")
@login_required
def policy_listing():
    """View all active policies with search, filter and sort."""
    from datetime import date, timedelta

    q          = request.args.get("q", "").strip()
    cat_filter = request.args.get("category", "")
    status_f   = request.args.get("status", "")
    renewal_f  = request.args.get("renewal", "")
    sort_by    = request.args.get("sort", "recently_added")

    # Base query
    if q:
        policies = services.search_policies(current_user.id, q)
    else:
        policies = services.get_all_active_policies_for_listing(current_user.id)

    # Category filter
    if cat_filter:
        policies = [p for p in policies if p.category == cat_filter]

    # Status filter
    if status_f:
        policies = [p for p in policies if p.status == status_f]

    # Renewal filter
    if renewal_f:
        today = date.today()
        if renewal_f == "overdue":
            policies = [p for p in policies
                       if p.renewal_date and p.renewal_date < today]
        elif renewal_f == "30days":
            cutoff = today + timedelta(days=30)
            policies = [p for p in policies
                       if p.renewal_date and today <= p.renewal_date <= cutoff]
        elif renewal_f == "90days":
            cutoff = today + timedelta(days=90)
            policies = [p for p in policies
                       if p.renewal_date and today <= p.renewal_date <= cutoff]

    # Sort
    if sort_by == "az":
        policies.sort(key=lambda p: p.insurer.lower())
    elif sort_by == "za":
        policies.sort(key=lambda p: p.insurer.lower(), reverse=True)
    elif sort_by == "recently_updated":
        policies.sort(key=lambda p: p.updated_at or p.created_at, reverse=True)
    elif sort_by == "renewal_date":
        policies.sort(key=lambda p: p.renewal_date or date.max)
    elif sort_by == "coverage_high":
        policies.sort(key=lambda p: p.sum_assured, reverse=True)
    elif sort_by == "coverage_low":
        policies.sort(key=lambda p: p.sum_assured)
    elif sort_by == "premium_high":
        policies.sort(key=lambda p: p.annual_premium, reverse=True)
    elif sort_by == "premium_low":
        policies.sort(key=lambda p: p.annual_premium)
    else:  # recently_added (default)
        policies.sort(key=lambda p: p.created_at, reverse=True)

    return render_template(
        "insurance_centre/policy_listing.html",
        policies=policies,
        category_icons=_category_icons(),
        format_inr=format_inr,
        format_date=format_date,
        categories=InsuranceCategory.ALL,
        policy_statuses=[s for s in PolicyStatus.ALL if s != "Archived"],
        # Search state — persist selections
        q=q, cat_filter=cat_filter,
        status_f=status_f, renewal_f=renewal_f, sort_by=sort_by,
        result_count=len(policies),
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
    """Edit an existing policy (active or archived) — reuses add_policy form."""
    policy = _get_any_policy_or_404(policy_id)

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
    policy = _get_any_policy_or_404(policy_id)
    if policy.is_archived:
        flash("Policy is already archived.", "info")
        return redirect(url_for("insurance_centre.archive_listing"))

    success, error = services.archive_policy(_db(), policy, current_user.id)
    if error:
        flash(error, "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))

    flash("Policy archived. You can restore it from the Archive.", "success")

    # Redirect back to referring page or policy listing
    referrer = request.form.get("next") or url_for("insurance_centre.policy_listing")
    return redirect(referrer)


@insurance_bp.route("/policy/<int:policy_id>/delete", methods=["POST"])
@login_required
def delete_policy_permanent(policy_id):
    """Permanently delete an archived policy. Only allowed if archived."""
    policy = InsurancePolicy.query.filter_by(
        id=policy_id, user_id=current_user.id).first_or_404()

    if not policy.is_archived:
        flash("Only archived policies can be permanently deleted.", "error")
        return redirect(url_for("insurance_centre.policy_detail",
                                policy_id=policy_id))

    # Delete all related documents' PHYSICAL FILES from disk.
    from .utils import delete_document_file
    for doc in policy.documents.all():
        delete_document_file(doc.file_path)

    # Explicitly null the now-dangling policy_id reference on each
    # document ROW (Section: confirmed real orphan bug — the model's
    # own comment already documented this intent — "SET NULL... stays
    # for audit purposes. Clean up separately" — but this "clean up
    # separately" step was never actually implemented, so the row
    # survived the physical-file deletion above while still pointing
    # at a policy_id that was about to stop existing). Same fix
    # pattern already applied to Wealth's equivalent bug.
    InsuranceDocument.query.filter_by(policy_id=policy.id).update({"policy_id": None})

    # Delete policy (cascades to nominees, members, addons, timeline —
    # documents are intentionally NOT cascaded, per the model's own
    # passive_deletes=True + the explicit cleanup just above).
    _db().session.delete(policy)
    _db().session.commit()

    flash("Policy permanently deleted.", "success")
    return redirect(url_for("insurance_centre.archive_listing"))


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
    new_expiry = request.form.get("new_expiry_date")
    new_expiry = datetime.strptime(new_expiry, "%Y-%m-%d").date() \
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
    """Dedicated category page — shows only policies for that category."""
    from .models import InsuranceCategory

    category = slug_to_category(slug)
    if category not in InsuranceCategory.ALL:
        abort(404)

    policies = services.get_policies_by_category(current_user.id, category)
    total_coverage = sum(p.sum_assured for p in policies)
    icons = _category_icons()

    return render_template(
        "insurance_centre/category_view.html",
        category=category,
        slug=slug,
        icon=icons.get(category, "📋"),
        policies=policies,
        total_coverage=total_coverage,
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
    policy = _get_policy_or_404(nominee.policy_id)
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
    policy = _get_policy_or_404(policy_id)
    file = request.files.get("document")
    doc_type = request.form.get("doc_type", "").strip()
    doc_title = request.form.get("doc_title", "").strip() or None
    notes = request.form.get("doc_notes", "").strip() or None

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
        title         = doc_title,
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
    """Delete document — removes file and metadata. Validates ownership.
    Redirects back to wherever the delete was triggered from (policy
    detail page or the Document Vault) via a 'next' form field."""
    from insurance_centre.models import InsuranceDocument

    doc = InsuranceDocument.query.filter_by(
        id=doc_id, user_id=current_user.id).first_or_404()
    policy_id = doc.policy_id
    next_target = request.form.get("next", "policy")
    redirect_url = (url_for("insurance_centre.document_vault")
                    if next_target == "vault"
                    else url_for("insurance_centre.policy_detail", policy_id=policy_id))

    # Security: verify file is within expected directory
    from .utils import delete_document_file, secure_file_path
    if doc.file_path and not secure_file_path(doc.file_path, policy_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(redirect_url)

    delete_document_file(doc.file_path)
    services.delete_document(_db(), doc, current_user.id)
    flash("Document deleted.", "success")

    return redirect(redirect_url)


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
        as_attachment=False,  # inline — opens in browser
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


# ── Export ────────────────────────────────────────────────────────────────────

@insurance_bp.route("/export/pdf")
@login_required
def export_pdf():
    """Export all active policies to branded PDF."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (SimpleDocTemplate, Paragraph,
                                         Spacer, Table, TableStyle,
                                         HRFlowable, KeepTogether)
        import io
        from datetime import datetime as _dt
    except ImportError:
        flash("reportlab required for PDF export. Run: py -m pip install reportlab", "error")
        return redirect(url_for("insurance_centre.policy_listing"))

    policies = services.get_all_active_policies_for_listing(current_user.id)
    stats    = services.InsuranceStatisticsService(current_user.id)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        topMargin=3.5*cm, bottomMargin=2.5*cm,
        leftMargin=2*cm, rightMargin=2*cm,
        title="MyWealthLens — Insurance Centre",
        author=current_user.name)

    styles  = getSampleStyleSheet()
    ACCENT  = colors.HexColor("#0F766E")
    LIGHT   = colors.HexColor("#F0FDF9")
    BORDER  = colors.HexColor("#E2E8F0")
    DARK    = colors.HexColor("#0F172A")
    MUTED   = colors.HexColor("#64748B")

    brand   = ParagraphStyle("Brand", fontSize=10, textColor=ACCENT,
                              fontName="Helvetica-Bold", spaceAfter=10)
    title   = ParagraphStyle("Title", fontSize=22, textColor=DARK,
                              fontName="Helvetica-Bold", spaceAfter=14, leading=28)
    subtitle= ParagraphStyle("Sub",   fontSize=10, textColor=MUTED,
                              spaceAfter=10, leading=16)
    h1      = ParagraphStyle("H1",    fontSize=12, textColor=ACCENT,
                              fontName="Helvetica-Bold", spaceAfter=6, spaceBefore=8)
    h2      = ParagraphStyle("H2",    fontSize=9,  textColor=DARK,
                              fontName="Helvetica-Bold", spaceAfter=4, spaceBefore=4)
    body    = ParagraphStyle("Body",  fontSize=8,  textColor=DARK,
                              spaceAfter=2, leading=12)
    small   = ParagraphStyle("Small", fontSize=7,  textColor=MUTED, leading=10)
    footer  = ParagraphStyle("Footer",fontSize=7,  textColor=MUTED,
                              alignment=1)

    story = []

    # ── Cover ──
    story.append(Paragraph("MyWealthLens", brand))
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph("Insurance Centre", title))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(f"Policy Summary Report for {current_user.name}", subtitle))
    story.append(Spacer(1, 0.25*cm))
    story.append(Paragraph(f"Generated: {_dt.now().strftime('%d %b %Y, %I:%M %p')}", subtitle))
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", color=ACCENT, thickness=2))
    story.append(Spacer(1, 0.5*cm))

    # ── Summary Stats ──
    summary_rows = [
        ["Total Active Policies", str(stats.active_count()),
         "Total Coverage", f"Rs.{stats.total_coverage():,.0f}"],
        ["Annual Premium", f"Rs.{stats.total_annual_premium():,.0f}",
         "Upcoming Renewals", str(stats.upcoming_renewals_count())],
    ]
    sum_tbl = Table(summary_rows, colWidths=[4.5*cm, 4*cm, 4.5*cm, 4*cm])
    sum_tbl.setStyle(TableStyle([
        ("FONTSIZE",(0,0),(-1,-1),8),
        ("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),
        ("FONTNAME",(2,0),(2,-1),"Helvetica-Bold"),
        ("TEXTCOLOR",(0,0),(0,-1),MUTED),
        ("TEXTCOLOR",(2,0),(2,-1),MUTED),
        ("FONTNAME",(1,0),(1,-1),"Helvetica-Bold"),
        ("FONTNAME",(3,0),(3,-1),"Helvetica-Bold"),
        ("TEXTCOLOR",(1,0),(1,-1),ACCENT),
        ("TEXTCOLOR",(3,0),(3,-1),ACCENT),
        ("FONTSIZE",(1,0),(1,-1),10),
        ("FONTSIZE",(3,0),(3,-1),10),
        ("BACKGROUND",(0,0),(-1,-1),LIGHT),
        ("GRID",(0,0),(-1,-1),0.3,BORDER),
        ("TOPPADDING",(0,0),(-1,-1),6),
        ("BOTTOMPADDING",(0,0),(-1,-1),6),
        ("LEFTPADDING",(0,0),(-1,-1),8),
    ]))
    story.append(sum_tbl)
    story.append(Spacer(1, 0.5*cm))

    # ── Policies ──
    for p in policies:
        block = []
        block.append(HRFlowable(width="100%", color=ACCENT, thickness=0.5))
        block.append(Spacer(1, 0.15*cm))
        block.append(Paragraph(f"{p.display_type} — {p.insurer}", h1))

        # Core details
        rows = [
            ["Category", p.category or "—",
             "Status", p.status or "—"],
            ["Policy No.", p.policy_number or "—",
             "Policy Holder", p.policy_holder or "—"],
            ["Coverage", f"Rs.{p.sum_assured:,.0f}",
             "Annual Premium", f"Rs.{p.annual_premium:,.0f}"],
            ["Premium", f"Rs.{p.premium_amount:,.0f} / {p.premium_frequency}",
             "Start Date", str(p.start_date or "—")],
            ["Renewal Date", str(p.renewal_date or "—"),
             "Maturity/Expiry", str(p.maturity_date or p.expiry_date or "—")],
        ]
        if p.agent_name:
            rows.append(["Agent", p.agent_name, "Agent Contact", p.agent_contact or "—"])
        if p.vehicle_number:
            rows.append(["Vehicle No.", p.vehicle_number, "Claim History", (p.claim_history or "—")[:50]])
        if p.property_name:
            rows.append(["Property", p.property_name, "Property Type", p.property_type or "—"])
        if p.cashless_available:
            rows.append(["Cashless", p.cashless_available.title(), "Policy Type", p.policy_type or "—"])

        tbl = Table(rows, colWidths=[3.5*cm, 5.5*cm, 3.5*cm, 4.5*cm])
        tbl.setStyle(TableStyle([
            ("FONTSIZE",(0,0),(-1,-1),8),
            ("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),
            ("FONTNAME",(2,0),(2,-1),"Helvetica-Bold"),
            ("TEXTCOLOR",(0,0),(0,-1),MUTED),
            ("TEXTCOLOR",(2,0),(2,-1),MUTED),
            ("ROWBACKGROUNDS",(0,0),(-1,-1),[LIGHT, colors.white]),
            ("GRID",(0,0),(-1,-1),0.3,BORDER),
            ("TOPPADDING",(0,0),(-1,-1),4),
            ("BOTTOMPADDING",(0,0),(-1,-1),4),
            ("LEFTPADDING",(0,0),(-1,-1),6),
        ]))
        block.append(tbl)

        # Nominees
        nominees = p.nominees.all()
        if nominees:
            block.append(Paragraph("Nominees", h2))
            for n in nominees:
                pct = f" — {n.percentage}%" if n.percentage else ""
                contact = f" | {n.contact}" if n.contact else ""
                block.append(Paragraph(f"• {n.name} ({n.relationship}){pct}{contact}", body))

        # Health Members
        members = p.members.all()
        if members:
            block.append(Paragraph("Insured Members", h2))
            for m in members:
                age = f", Age {m.age}" if m.age else ""
                block.append(Paragraph(f"• {m.member_name} ({m.relationship}){age}", body))

        # Motor Add-ons
        addons = p.addons.all()
        if addons:
            block.append(Paragraph("Motor Add-ons: " + ", ".join(a.addon_name for a in addons), body))

        # Documents
        docs = p.documents.all()
        if docs:
            block.append(Paragraph("Documents", h2))
            for d in docs:
                block.append(Paragraph(f"• {d.doc_type}: {d.original_name} ({d.file_size_display})", body))

        if p.notes:
            block.append(Paragraph(f"Notes: {p.notes}", small))

        block.append(Spacer(1, 0.3*cm))
        story.append(KeepTogether(block))

    # ── Footer ──
    story.append(Spacer(1, 0.5*cm))
    story.append(HRFlowable(width="100%", color=BORDER, thickness=0.5))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        "Generated locally by MyWealthLens • Personal Use Only • "
        f"{_dt.now().strftime('%d %b %Y')}",
        footer))

    doc.build(story)
    buf.seek(0)
    return send_file(buf, mimetype="application/pdf",
        as_attachment=True,
        download_name=f"insurance_policies_{_dt.now().strftime('%Y%m%d')}.pdf")


# ── Document Vault ─────────────────────────────────────────────────────────────

@insurance_bp.route("/documents")
@login_required
def document_vault():
    """
    Document Vault — first-class page listing every document across
    all of the user's insurance policies, with search and filters.
    Documents are grouped by category then by policy, mirroring the
    same navigation structure as Retirement Centre's Document Vault.
    """
    q        = request.args.get("q", "").strip()
    category = request.args.get("category", "")
    doc_type = request.args.get("doc_type", "")

    documents = services.get_vault_documents(
        current_user.id, q=q or None, category=category or None,
        doc_type=doc_type or None,
    )
    summary = services.vault_summary(current_user.id)

    # Group: category -> policy -> documents, preserving first-seen order.
    grouped = {}
    order = []
    for d in documents:
        policy = d._policy
        cat = policy.category if policy else "Other"
        if cat not in grouped:
            grouped[cat] = {}
            order.append(cat)
        pid = policy.id if policy else 0
        if pid not in grouped[cat]:
            grouped[cat][pid] = {"policy": policy, "documents": []}
        grouped[cat][pid]["documents"].append(d)

    grouped_documents = [
        {"category": cat, "policy_groups": list(grouped[cat].values())}
        for cat in order
    ]

    # Policies for the upload form's dropdown (active policies only)
    upload_policies = (InsurancePolicy.query
                       .filter_by(user_id=current_user.id, is_archived=False)
                       .order_by(InsurancePolicy.category.asc())
                       .all())
    preselect_policy_id = request.args.get("policy_id", type=int)

    return render_template(
        "insurance_centre/document_vault.html",
        documents=documents,
        grouped_documents=grouped_documents,
        summary=summary,
        categories=InsuranceCategory.ALL,
        doc_types=DocumentType.ALL,
        upload_policies=upload_policies,
        preselect_policy_id=preselect_policy_id,
        q=q, category=category, doc_type=doc_type,
        format_date=format_date,
        category_icons=_category_icons(),
    )


@insurance_bp.route("/documents/upload", methods=["POST"])
@login_required
def upload_document_vault():
    """
    Upload a document from the Vault directly — the policy is chosen
    via a form dropdown rather than implied by the URL. Reuses the
    exact same storage/validation logic as the per-policy upload
    route — no parallel storage system.
    """
    policy_id = request.form.get("policy_id", type=int)
    if not policy_id:
        flash("Please select an insurance policy for this document.", "error")
        return redirect(url_for("insurance_centre.document_vault"))

    policy = InsurancePolicy.query.filter_by(
        id=policy_id, user_id=current_user.id, is_archived=False).first()
    if not policy:
        flash("Invalid policy selected.", "error")
        return redirect(url_for("insurance_centre.document_vault"))

    file = request.files.get("document")
    doc_type = request.form.get("doc_type", "").strip()
    doc_title = request.form.get("doc_title", "").strip() or None
    notes = request.form.get("doc_notes", "").strip() or None

    errors = validators.validate_document(file, doc_type)
    if errors:
        flash(errors[0], "error")
        return redirect(url_for("insurance_centre.document_vault"))

    try:
        from .utils import save_document_file
        stored_name, file_path, file_size = save_document_file(file, policy_id)
    except OSError as e:
        flash(f"File could not be saved: {e}", "error")
        return redirect(url_for("insurance_centre.document_vault"))

    services.save_document_metadata(
        _db(), policy, current_user.id,
        doc_type      = doc_type,
        title         = doc_title,
        original_name = file.filename,
        stored_name   = stored_name,
        file_path     = file_path,
        file_size     = file_size,
        notes         = notes,
    )
    flash("Document uploaded successfully!", "success")
    return redirect(url_for("insurance_centre.document_vault"))


# ── Private Helpers ───────────────────────────────────────────────────────────

def _get_policy_or_404(policy_id):
    """Get active policy — must belong to current user."""
    return InsurancePolicy.query.filter_by(
        id=policy_id,
        user_id=current_user.id,
        is_archived=False,
    ).first_or_404()


def _get_any_policy_or_404(policy_id):
    """Get any policy (active or archived) — must belong to current user."""
    return InsurancePolicy.query.filter_by(
        id=policy_id,
        user_id=current_user.id,
    ).first_or_404()


def init_routes(bp):
    """Register all routes onto the blueprint."""
    # All routes are already registered via decorators above
    pass