"""
International Investing Centre — Routes
===========================================
Thin handlers — all logic lives in services.py. Every query filtered
by current_user.id (IDOR check), matching every other module.

Transaction editing (Batch 9.2, Sep 2026) is now a full edit route
(edit_transaction), not just add/delete — an inline edit row on
holding_detail.html, matching the same fix-a-mistake convenience every
other editable thing in the app (holdings, remittances) already had.

Document Vault (Batch 9.3, Sep 2026) — per-holding upload/delete/
download/preview routes plus a standalone module-wide vault page,
mirroring insurance_centre/retirement_centre's Document Vault exactly.
"""
from flask import render_template, request, redirect, url_for, flash, abort, send_file
from flask_login import login_required, current_user

from international_centre import international_bp
from international_centre.models import (
    InternationalHolding, InternationalTransaction, RemittanceRecord,
    InternationalHoldingDocument,
    InternationalAssetType, InternationalTxnType, RemittancePurpose, DocumentType,
)
from international_centre import services
from international_centre.utils import (
    format_date, COUNTRIES,
    save_document_file, delete_document_file, secure_file_path,
    is_previewable, get_preview_mimetype,
)
from fx_rates import SUPPORTED_CURRENCIES
from international_centre.validators import (
    validate_holding, validate_transaction, validate_remittance, validate_document,
)
from wealth.timezone_utils import today_ist
import os
import currency_display


def _get_holding_or_404(holding_id):
    holding = InternationalHolding.query.filter_by(id=holding_id, user_id=current_user.id).first()
    if not holding:
        abort(404)
    return holding


def _get_document_or_404(doc_id):
    doc = (InternationalHoldingDocument.query
           .filter_by(id=doc_id, user_id=current_user.id).first())
    if not doc:
        abort(404)
    return doc


def _db():
    from models import db
    return db


def _get_txn_or_404(txn_id):
    txn = (InternationalTransaction.query
           .filter_by(id=txn_id, user_id=current_user.id).first())
    if not txn:
        abort(404)
    return txn


def _get_remittance_or_404(remit_id):
    remit = RemittanceRecord.query.filter_by(id=remit_id, user_id=current_user.id).first()
    if not remit:
        abort(404)
    return remit


# ── Dashboard ─────────────────────────────────────────────────────────

@international_bp.route("/")
@login_required
def dashboard():
    holdings = services.get_holdings(current_user.id, archived=False)
    totals = services.portfolio_totals(current_user.id)
    portfolio_xirr = services.portfolio_usd_xirr(current_user.id)
    lrs_status = services.get_lrs_status(current_user.id)

    by_type = {}
    for h in holdings:
        by_type.setdefault(h.asset_type, {"count": 0, "usd_value": 0.0})
        by_type[h.asset_type]["count"] += 1
        by_type[h.asset_type]["usd_value"] += h.usd_value or 0.0

    return render_template(
        "international_centre/dashboard.html",
        holdings=holdings, totals=totals, portfolio_xirr=portfolio_xirr,
        lrs_status=lrs_status, by_type=by_type,
        format_money_usd=currency_display.format_money_usd, format_date=format_date,
    )


# ── Holdings ──────────────────────────────────────────────────────────

@international_bp.route("/holdings/add", methods=["GET", "POST"])
@login_required
def add_holding():
    if request.method == "POST":
        data = request.form.to_dict()
        errors = validate_holding(data)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "international_centre/holding_form.html", holding=None, data=data,
                existing_nominees=[],
                asset_types=InternationalAssetType.ALL,
                ticker_based_types=list(InternationalAssetType.TICKER_BASED),
                currencies=SUPPORTED_CURRENCIES, countries=COUNTRIES,
            )
        holding, error = services.create_holding(current_user.id, data, multi_data=request.form)
        if error:
            flash(error, "error")
            return render_template(
                "international_centre/holding_form.html", holding=None, data=data,
                existing_nominees=[],
                asset_types=InternationalAssetType.ALL,
                ticker_based_types=list(InternationalAssetType.TICKER_BASED),
                currencies=SUPPORTED_CURRENCIES, countries=COUNTRIES,
            )
        flash(f'Added "{holding.name}" to your international holdings.', "success")
        return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))

    return render_template(
        "international_centre/holding_form.html", holding=None, data={},
        existing_nominees=[],
        asset_types=InternationalAssetType.ALL,
        ticker_based_types=list(InternationalAssetType.TICKER_BASED),
        currencies=SUPPORTED_CURRENCIES, countries=COUNTRIES,
    )


@international_bp.route("/holdings/<int:holding_id>")
@login_required
def holding_detail(holding_id):
    holding = _get_holding_or_404(holding_id)
    transactions = (InternationalTransaction.query
                     .filter_by(holding_id=holding.id)
                     .order_by(InternationalTransaction.date.desc())
                     .all())
    documents = (InternationalHoldingDocument.query
                 .filter_by(holding_id=holding.id)
                 .order_by(InternationalHoldingDocument.uploaded_at.desc())
                 .all())
    return render_template(
        "international_centre/holding_detail.html", holding=holding, transactions=transactions,
        nominees=holding.nominees.all(),
        documents=documents, doc_types=DocumentType.ALL,
        txn_types=InternationalTxnType.ALL, format_date=format_date,
        format_money_usd=currency_display.format_money_usd,
        today=today_ist().isoformat(),
    )


@international_bp.route("/holdings/<int:holding_id>/edit", methods=["GET", "POST"])
@login_required
def edit_holding(holding_id):
    holding = _get_holding_or_404(holding_id)
    if request.method == "POST":
        data = request.form.to_dict()
        errors = validate_holding(data)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(
                "international_centre/holding_form.html", holding=holding, data=data,
                existing_nominees=holding.nominees.all(),
                asset_types=InternationalAssetType.ALL,
                ticker_based_types=list(InternationalAssetType.TICKER_BASED),
                currencies=SUPPORTED_CURRENCIES, countries=COUNTRIES,
            )
        updated, error = services.update_holding(holding, data, multi_data=request.form)
        if error:
            flash(error, "error")
            return render_template(
                "international_centre/holding_form.html", holding=holding, data=data,
                existing_nominees=holding.nominees.all(),
                asset_types=InternationalAssetType.ALL,
                ticker_based_types=list(InternationalAssetType.TICKER_BASED),
                currencies=SUPPORTED_CURRENCIES, countries=COUNTRIES,
            )
        flash(f'Updated "{updated.name}".', "success")
        return redirect(url_for("international_centre.holding_detail", holding_id=updated.id))

    return render_template(
        "international_centre/holding_form.html", holding=holding, data={},
        existing_nominees=holding.nominees.all(),
        asset_types=InternationalAssetType.ALL,
        ticker_based_types=list(InternationalAssetType.TICKER_BASED),
        currencies=SUPPORTED_CURRENCIES, countries=COUNTRIES,
    )


@international_bp.route("/holdings/<int:holding_id>/refresh", methods=["POST"])
@login_required
def refresh_holding(holding_id):
    holding = _get_holding_or_404(holding_id)
    services.refresh_holding(holding)
    from models import db
    db.session.commit()
    flash(f'Refreshed price/value for "{holding.name}".', "success")
    return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))


@international_bp.route("/refresh-all", methods=["POST"])
@login_required
def refresh_all():
    count = services.refresh_all_holdings(current_user.id)
    flash(f"Refreshed {count} holding(s).", "success")
    return redirect(url_for("international_centre.dashboard"))


@international_bp.route("/holdings/<int:holding_id>/archive", methods=["POST"])
@login_required
def archive_holding(holding_id):
    holding = _get_holding_or_404(holding_id)
    services.archive_holding(holding)
    flash(f'Archived "{holding.name}".', "success")
    return redirect(url_for("international_centre.dashboard"))


@international_bp.route("/holdings/<int:holding_id>/restore", methods=["POST"])
@login_required
def restore_holding(holding_id):
    holding = _get_holding_or_404(holding_id)
    services.restore_holding(holding)
    flash(f'Restored "{holding.name}".', "success")
    return redirect(url_for("international_centre.archived_holdings"))


@international_bp.route("/holdings/<int:holding_id>/delete", methods=["POST"])
@login_required
def delete_holding(holding_id):
    holding = _get_holding_or_404(holding_id)
    if not holding.archived:
        flash("Archive a holding before deleting it permanently.", "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))
    name = holding.name
    # Delete documents' PHYSICAL FILES from disk first (Batch 9.3) — the
    # DB rows themselves cascade via the ORM relationship's
    # cascade="all, delete-orphan" when the holding is deleted below,
    # but nothing removes the actual files on disk unless we do it here,
    # matching insurance_centre's identical pattern for its own vault.
    for doc in holding.documents:
        delete_document_file(doc.file_path)
    services.delete_holding_permanently(holding)
    flash(f'Permanently deleted "{name}".', "success")
    return redirect(url_for("international_centre.archived_holdings"))


@international_bp.route("/archived")
@login_required
def archived_holdings():
    holdings = services.get_holdings(current_user.id, archived=True)
    return render_template(
        "international_centre/archived.html", holdings=holdings,
        format_money_usd=currency_display.format_money_usd,
    )


# ── Transactions ──────────────────────────────────────────────────────

@international_bp.route("/holdings/<int:holding_id>/transactions/add", methods=["POST"])
@login_required
def add_transaction(holding_id):
    holding = _get_holding_or_404(holding_id)
    data = request.form.to_dict()
    errors = validate_transaction(data)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))
    services.add_transaction(holding, data)
    flash("Transaction added.", "success")
    return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))


@international_bp.route("/transactions/<int:txn_id>/edit", methods=["POST"])
@login_required
def edit_transaction(txn_id):
    """Batch 9.2 (Sep 2026) — was add/delete only; services.update_transaction()
    already existed (written alongside add_transaction/delete_transaction
    but never wired to a route). Submits back to holding_detail's inline
    edit row (see holding_detail.html) rather than a separate page."""
    txn = _get_txn_or_404(txn_id)
    holding_id = txn.holding_id
    data = request.form.to_dict()
    errors = validate_transaction(data)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=holding_id))
    services.update_transaction(txn, data)
    flash("Transaction updated.", "success")
    return redirect(url_for("international_centre.holding_detail", holding_id=holding_id))


@international_bp.route("/transactions/<int:txn_id>/delete", methods=["POST"])
@login_required
def delete_transaction(txn_id):
    txn = _get_txn_or_404(txn_id)
    holding_id = txn.holding_id
    services.delete_transaction(txn)
    flash("Transaction deleted.", "success")
    return redirect(url_for("international_centre.holding_detail", holding_id=holding_id))


# ── Remittances (LRS) ───────────────────────────────────────────────

@international_bp.route("/remittances")
@login_required
def remittances():
    lrs_status = services.get_lrs_status(current_user.id)
    holdings = services.get_holdings(current_user.id, archived=False)
    return render_template(
        "international_centre/remittances.html", lrs_status=lrs_status, holdings=holdings,
        purposes=RemittancePurpose.ALL, format_date=format_date,
        today=today_ist().isoformat(),
    )


@international_bp.route("/remittances/add", methods=["POST"])
@login_required
def add_remittance():
    data = request.form.to_dict()
    errors = validate_remittance(data)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("international_centre.remittances"))
    remittance = services.add_remittance(current_user.id, data)
    if remittance.fx_rate_used is None:
        flash("Remittance saved, but today's INR-USD rate couldn't be fetched — its LRS "
              "USD-equivalent is showing as $0 until a rate can be refreshed.", "warning")
    else:
        flash(f"Remittance of ₹{remittance.amount_inr:,.0f} logged (${remittance.amount_usd:,.2f}).", "success")
    return redirect(url_for("international_centre.remittances"))


@international_bp.route("/remittances/<int:remit_id>/delete", methods=["POST"])
@login_required
def delete_remittance(remit_id):
    remit = _get_remittance_or_404(remit_id)
    services.delete_remittance(remit)
    flash("Remittance record deleted.", "success")
    return redirect(url_for("international_centre.remittances"))


# ── Schedule FA ─────────────────────────────────────────────────────

@international_bp.route("/schedule-fa")
@login_required
def schedule_fa():
    year_raw = request.args.get("year")
    try:
        calendar_year = int(year_raw) if year_raw else today_ist().year
    except ValueError:
        calendar_year = today_ist().year

    summary = services.get_schedule_fa_summary(current_user.id, calendar_year)
    return render_template(
        "international_centre/schedule_fa.html", summary=summary,
        format_date=format_date, current_year=today_ist().year,
    )


# ── Documents (Batch 9.3, Sep 2026) ──────────────────────────────────

@international_bp.route("/holdings/<int:holding_id>/documents/upload", methods=["POST"])
@login_required
def upload_document(holding_id):
    """Upload a document to a holding. Validates ownership and file."""
    holding  = _get_holding_or_404(holding_id)
    file     = request.files.get("document")
    doc_type = request.form.get("doc_type", "").strip()
    title    = request.form.get("doc_title", "").strip() or None
    notes    = request.form.get("doc_notes", "").strip() or None
    # Set by client-side encryption JS when a user has an unlocked
    # passphrase session — see models.py's InternationalHoldingDocument
    # docstring: that JS doesn't actually exist anywhere in the repo
    # yet, so these are always empty/false today. Fields kept here for
    # schema/route parity with Wealth/Retirement's identical pattern.
    doc_iv           = request.form.get("iv", "").strip()
    doc_is_encrypted = request.form.get("is_encrypted", "").strip() == "1"

    errors = validate_document(file, doc_type)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))

    try:
        stored_name, file_path, file_size = save_document_file(file, holding.id)
    except OSError as e:
        flash(f"File could not be saved: {e}", "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))

    services.save_document_metadata(
        _db(), holding, current_user.id,
        doc_type=doc_type, original_name=file.filename,
        stored_name=stored_name, file_path=file_path,
        file_size=file_size, notes=notes, title=title,
        iv=doc_iv, is_encrypted=doc_is_encrypted,
    )
    flash("Document uploaded successfully!", "success")
    return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))


@international_bp.route("/documents/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete_document(doc_id):
    """Delete document — removes file and metadata. Validates ownership.
    Redirects back to wherever the delete was triggered from (holding
    detail page or the Document Vault) via a 'next' form field."""
    doc = _get_document_or_404(doc_id)
    holding_id = doc.holding_id
    next_target = request.form.get("next", "holding_detail")
    redirect_url = (url_for("international_centre.document_vault")
                    if next_target == "vault"
                    else url_for("international_centre.holding_detail", holding_id=holding_id))

    if doc.file_path and not secure_file_path(doc.file_path, holding_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(redirect_url)

    delete_document_file(doc.file_path)
    services.delete_document(_db(), doc, current_user.id)
    flash("Document deleted.", "success")
    return redirect(redirect_url)


@international_bp.route("/documents/<int:doc_id>/download")
@login_required
def download_document(doc_id):
    """Download document — validates ownership, preserves original filename."""
    doc = _get_document_or_404(doc_id)

    if not doc.file_path or not os.path.exists(doc.file_path):
        flash("File not found.", "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=doc.holding_id))
    if not secure_file_path(doc.file_path, doc.holding_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=doc.holding_id))

    return send_file(doc.file_path, as_attachment=True, download_name=doc.original_name)


@international_bp.route("/documents/<int:doc_id>/preview")
@login_required
def preview_document(doc_id):
    """Preview document inline in browser (PDF and images only)."""
    doc = _get_document_or_404(doc_id)

    if not doc.file_path or not os.path.exists(doc.file_path):
        flash("File not found.", "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=doc.holding_id))
    if not secure_file_path(doc.file_path, doc.holding_id):
        flash("Invalid file path — operation denied.", "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=doc.holding_id))
    if not is_previewable(doc.original_name):
        flash("Preview not available for this file type.", "error")
        return redirect(url_for("international_centre.holding_detail", holding_id=doc.holding_id))

    return send_file(doc.file_path, mimetype=get_preview_mimetype(doc.original_name),
                      as_attachment=False, download_name=doc.original_name)


@international_bp.route("/documents")
@login_required
def document_vault():
    """Document Vault — first-class page listing every document across
    all of the user's international holdings, with search and filters.
    Grouped by asset type then by holding (this module has no separate
    'category' concept the way Insurance/Retirement do — asset_type is
    the closest analogue)."""
    q          = request.args.get("q", "").strip()
    asset_type = request.args.get("asset_type", "")
    doc_type   = request.args.get("doc_type", "")

    documents = services.get_vault_documents(
        current_user.id, q=q or None, asset_type=asset_type or None,
        doc_type=doc_type or None,
    )
    summary = services.vault_summary(current_user.id)

    # Group: asset_type -> holding -> documents, preserving first-seen order.
    grouped = {}
    order = []
    for d in documents:
        at = d.holding.asset_type
        if at not in grouped:
            grouped[at] = {}
            order.append(at)
        hid = d.holding.id
        if hid not in grouped[at]:
            grouped[at][hid] = {"holding": d.holding, "documents": []}
        grouped[at][hid]["documents"].append(d)

    grouped_documents = [
        {"asset_type": at, "holding_groups": list(grouped[at].values())}
        for at in order
    ]

    upload_holdings = services.get_holdings(current_user.id, archived=False)
    preselect_holding_id = request.args.get("holding_id", type=int)

    return render_template(
        "international_centre/document_vault.html",
        documents=documents,
        grouped_documents=grouped_documents,
        summary=summary,
        asset_types=InternationalAssetType.ALL,
        doc_types=DocumentType.ALL,
        upload_holdings=upload_holdings,
        preselect_holding_id=preselect_holding_id,
        q=q, asset_type=asset_type, doc_type=doc_type,
        format_date=format_date,
    )


@international_bp.route("/documents/upload", methods=["POST"])
@login_required
def upload_document_vault():
    """Upload a document from the Vault directly — the holding is
    chosen via a form dropdown rather than implied by the URL."""
    holding_id_raw = request.form.get("holding_id", "").strip()
    if not holding_id_raw:
        flash("Please select an international holding for this document.", "error")
        return redirect(url_for("international_centre.document_vault"))

    holding = InternationalHolding.query.filter_by(
        id=holding_id_raw, user_id=current_user.id).first()
    if not holding:
        flash("Holding not found.", "error")
        return redirect(url_for("international_centre.document_vault"))

    file     = request.files.get("document")
    doc_type = request.form.get("doc_type", "").strip()
    title    = request.form.get("doc_title", "").strip() or None
    notes    = request.form.get("doc_notes", "").strip() or None
    doc_iv           = request.form.get("iv", "").strip()
    doc_is_encrypted = request.form.get("is_encrypted", "").strip() == "1"

    errors = validate_document(file, doc_type)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("international_centre.document_vault"))

    try:
        stored_name, file_path, file_size = save_document_file(file, holding.id)
    except OSError as e:
        flash(f"File could not be saved: {e}", "error")
        return redirect(url_for("international_centre.document_vault"))

    services.save_document_metadata(
        _db(), holding, current_user.id,
        doc_type=doc_type, original_name=file.filename,
        stored_name=stored_name, file_path=file_path,
        file_size=file_size, notes=notes, title=title,
        iv=doc_iv, is_encrypted=doc_is_encrypted,
    )
    flash("Document uploaded successfully!", "success")
    return redirect(url_for("international_centre.document_vault"))
