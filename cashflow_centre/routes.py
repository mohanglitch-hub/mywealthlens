"""
Cashflow Centre — Routes
==============================
Thin handlers — all logic lives in services.py. Every query filtered
by current_user.id (IDOR check), matching every other module.
"""
from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from cashflow_centre import cashflow_bp
from cashflow_centre.models import (
    Transaction, Budget, TransactionType, ExpenseCategory, IncomeCategory, PaymentMethod,
)
from cashflow_centre import services
from cashflow_centre.utils import (
    format_inr, format_date, current_month_key, parse_month_key,
    month_label, adjacent_month_key,
)


def _db():
    from models import db
    return db


def _resolve_month():
    """Reads ?month=YYYY-MM, falls back to the current month."""
    parsed = parse_month_key(request.args.get("month"))
    if parsed:
        return parsed
    parsed = parse_month_key(current_month_key())
    return parsed


def _get_transaction_or_404(txn_id):
    from flask import abort
    txn = Transaction.query.filter_by(id=txn_id, user_id=current_user.id).first()
    if not txn:
        abort(404)
    return txn


def _get_budget_or_404(budget_id):
    from flask import abort
    budget = Budget.query.filter_by(id=budget_id, user_id=current_user.id).first()
    if not budget:
        abort(404)
    return budget


# ── Dashboard ─────────────────────────────────────────────────────────────────

@cashflow_bp.route("/")
@login_required
def dashboard():
    year, month = _resolve_month()
    month_key = f"{year:04d}-{month:02d}"

    summary = services.get_month_summary(current_user.id, year, month)
    budgets = services.get_budgets_with_progress(current_user.id, year, month)
    recent  = summary["transactions"][:8]

    return render_template(
        "cashflow_centre/dashboard.html",
        summary=summary, budgets=budgets, recent=recent,
        month_key=month_key, month_display=month_label(year, month),
        prev_month=adjacent_month_key(year, month, -1),
        next_month=adjacent_month_key(year, month, 1),
        format_inr=format_inr, format_date=format_date,
    )


# ── Transactions ──────────────────────────────────────────────────────────────

@cashflow_bp.route("/transactions")
@login_required
def transactions():
    year, month = _resolve_month()
    month_key = f"{year:04d}-{month:02d}"

    category = request.args.get("category", "").strip() or None
    txn_type = request.args.get("type", "").strip() or None

    txns = services.get_transactions(current_user.id, year=year, month=month,
                                     category=category, txn_type=txn_type)
    total = sum(t.amount for t in txns)

    return render_template(
        "cashflow_centre/transactions.html",
        transactions=txns, total=total,
        month_key=month_key, month_display=month_label(year, month),
        prev_month=adjacent_month_key(year, month, -1),
        next_month=adjacent_month_key(year, month, 1),
        expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
        selected_category=category, selected_type=txn_type,
        format_inr=format_inr, format_date=format_date,
    )


@cashflow_bp.route("/transactions/add", methods=["GET", "POST"])
@login_required
def add_transaction():
    if request.method == "POST":
        txn, error = services.create_transaction(_db(), current_user.id, request.form)
        if error:
            flash(error, "error")
            return render_template(
                "cashflow_centre/transaction_form.html", is_edit=False, txn=None,
                expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
                payment_methods=PaymentMethod.ALL, values=request.form,
            )
        flash("Transaction added.", "success")
        return redirect(url_for("cashflow_centre.dashboard", month=txn.month_key))

    from datetime import date
    return render_template(
        "cashflow_centre/transaction_form.html", is_edit=False, txn=None,
        expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
        payment_methods=PaymentMethod.ALL,
        values={"date": date.today().strftime("%Y-%m-%d"), "type": TransactionType.EXPENSE},
    )


@cashflow_bp.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
@login_required
def edit_transaction(txn_id):
    txn = _get_transaction_or_404(txn_id)

    if request.method == "POST":
        updated, error = services.update_transaction(_db(), txn, current_user.id, request.form)
        if error:
            flash(error, "error")
            return render_template(
                "cashflow_centre/transaction_form.html", is_edit=True, txn=txn,
                expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
                payment_methods=PaymentMethod.ALL, values=request.form,
            )
        flash("Transaction updated.", "success")
        return redirect(url_for("cashflow_centre.dashboard", month=updated.month_key))

    values = {
        "date": txn.date.strftime("%Y-%m-%d"), "type": txn.type,
        "category": txn.category, "amount": txn.amount,
        "payment_method": txn.payment_method or "", "description": txn.description or "",
    }
    return render_template(
        "cashflow_centre/transaction_form.html", is_edit=True, txn=txn,
        expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
        payment_methods=PaymentMethod.ALL, values=values,
    )


@cashflow_bp.route("/transactions/<int:txn_id>/delete", methods=["POST"])
@login_required
def delete_transaction(txn_id):
    txn = _get_transaction_or_404(txn_id)
    month_key = txn.month_key
    success, error = services.delete_transaction(_db(), txn, current_user.id)
    flash(error, "error") if error else flash("Transaction deleted.", "success")
    return redirect(url_for("cashflow_centre.dashboard", month=month_key))


# ── Budgets ───────────────────────────────────────────────────────────────────

@cashflow_bp.route("/budgets")
@login_required
def budgets():
    year, month = _resolve_month()
    month_key = f"{year:04d}-{month:02d}"

    rows = services.get_budgets_with_progress(current_user.id, year, month)
    already_budgeted = services.budgeted_categories(current_user.id)
    available_categories = [c for c in ExpenseCategory.ALL if c not in already_budgeted]

    return render_template(
        "cashflow_centre/budgets.html",
        budgets=rows, available_categories=available_categories,
        month_key=month_key, month_display=month_label(year, month),
        format_inr=format_inr,
    )


@cashflow_bp.route("/budgets/save", methods=["POST"])
@login_required
def save_budget():
    budget, error = services.upsert_budget(_db(), current_user.id, request.form)
    flash(error, "error") if error else flash("Budget saved.", "success")
    return redirect(url_for("cashflow_centre.budgets"))


@cashflow_bp.route("/budgets/<int:budget_id>/delete", methods=["POST"])
@login_required
def delete_budget(budget_id):
    budget = _get_budget_or_404(budget_id)
    success, error = services.delete_budget(_db(), budget, current_user.id)
    flash(error, "error") if error else flash("Budget removed.", "success")
    return redirect(url_for("cashflow_centre.budgets"))
