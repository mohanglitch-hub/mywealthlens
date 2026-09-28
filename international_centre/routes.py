"""
International Investing Centre — Routes
===========================================
Thin handlers — all logic lives in services.py. Every query filtered
by current_user.id (IDOR check), matching every other module.

Transaction editing is add/delete only in this first version (no
separate "edit a transaction" form) — a deliberate scope cut to keep
v1 manageable; a mistaken entry is fixed by deleting and re-adding it.
Everything else (holdings, remittances) has full edit support.
"""
from flask import render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from international_centre import international_bp
from international_centre.models import (
    InternationalHolding, InternationalTransaction, RemittanceRecord,
    InternationalAssetType, InternationalTxnType, RemittancePurpose,
)
from international_centre import services
from international_centre.utils import format_date, COUNTRIES
from fx_rates import SUPPORTED_CURRENCIES
from international_centre.validators import validate_holding, validate_transaction, validate_remittance
from wealth.timezone_utils import today_ist
import currency_display


def _get_holding_or_404(holding_id):
    holding = InternationalHolding.query.filter_by(id=holding_id, user_id=current_user.id).first()
    if not holding:
        abort(404)
    return holding


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
                asset_types=InternationalAssetType.ALL,
                ticker_based_types=list(InternationalAssetType.TICKER_BASED),
                currencies=SUPPORTED_CURRENCIES, countries=COUNTRIES,
            )
        holding = services.create_holding(current_user.id, data)
        flash(f'Added "{holding.name}" to your international holdings.', "success")
        return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))

    return render_template(
        "international_centre/holding_form.html", holding=None, data={},
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
    return render_template(
        "international_centre/holding_detail.html", holding=holding, transactions=transactions,
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
                asset_types=InternationalAssetType.ALL,
                ticker_based_types=list(InternationalAssetType.TICKER_BASED),
                currencies=SUPPORTED_CURRENCIES, countries=COUNTRIES,
            )
        services.update_holding(holding, data)
        flash(f'Updated "{holding.name}".', "success")
        return redirect(url_for("international_centre.holding_detail", holding_id=holding.id))

    return render_template(
        "international_centre/holding_form.html", holding=holding, data={},
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
