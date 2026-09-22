"""
Cashflow Centre — Routes
==============================
Thin handlers — all logic lives in services.py. Every query filtered
by current_user.id (IDOR check), matching every other module.
"""
from datetime import datetime

from flask import render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from cashflow_centre import cashflow_bp
from cashflow_centre.models import (
    Transaction, Budget, TransactionType, ExpenseCategory, IncomeCategory, PaymentMethod,
)
from cashflow_centre import services
from cashflow_centre.utils import (
    format_inr, format_date, current_month_key, parse_month_key,
    month_label, adjacent_month_key, last_n_months_bounds, fy_bounds, fy_label,
)
from wealth.timezone_utils import today_ist


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


def _safe_next(default):
    """
    Reads a 'next' value from the query string or form body and uses
    it as a redirect target only if it's a local /cashflow/... path
    (never an open redirect). Falls back to `default` otherwise.
    """
    nxt = request.values.get("next")
    if nxt and nxt.startswith("/cashflow/"):
        return nxt
    return default


# ── Dashboard ─────────────────────────────────────────────────────────────────

@cashflow_bp.route("/")
@login_required
def dashboard():
    year, month = _resolve_month()
    month_key = f"{year:04d}-{month:02d}"

    summary = services.get_month_summary(current_user.id, year, month)
    budgets = services.get_budgets_with_progress(current_user.id, year, month)
    recent  = summary["transactions"][:8]
    is_current_month = month_key == current_month_key()
    quick_add = services.get_quick_add_defaults(current_user.id)

    self_url = url_for("cashflow_centre.dashboard", month=month_key)

    return render_template(
        "cashflow_centre/dashboard.html",
        summary=summary, budgets=budgets, recent=recent,
        month_key=month_key, month_display=month_label(year, month),
        prev_month=adjacent_month_key(year, month, -1),
        next_month=adjacent_month_key(year, month, 1),
        is_current_month=is_current_month, self_url=self_url,
        quick_add=quick_add,
        expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
        payment_methods=PaymentMethod.ALL,
        today_ist=today_ist().strftime("%Y-%m-%d"),
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
    range_preset = request.args.get("range", "month").strip() or "month"
    from_str = request.args.get("from", "").strip() or None
    to_str = request.args.get("to", "").strip() or None

    from_date = to_date = None
    range_label = None
    today = today_ist()

    if range_preset == "last3":
        from_date, to_date = last_n_months_bounds(today, 3)
        range_label = f"{format_date(from_date)} – {format_date(to_date)}"
    elif range_preset == "fy":
        from_date, to_date = fy_bounds(today)
        range_label = fy_label(today)
    elif range_preset == "custom" and from_str and to_str:
        try:
            from_date = datetime.strptime(from_str, "%Y-%m-%d").date()
            to_date = datetime.strptime(to_str, "%Y-%m-%d").date()
            range_label = f"{format_date(from_date)} – {format_date(to_date)}"
        except ValueError:
            range_preset = "month"
    else:
        range_preset = "month"

    if from_date and to_date:
        txns = services.get_transactions(current_user.id, from_date=from_date, to_date=to_date,
                                         category=category, txn_type=txn_type)
        range_kwargs = {"range": range_preset}
        if range_preset == "custom":
            range_kwargs.update({"from": from_str, "to": to_str})
        self_url = url_for("cashflow_centre.transactions",
                            category=category or None, type=txn_type or None, **range_kwargs)
        clear_filters_url = url_for("cashflow_centre.transactions", **range_kwargs)
        display_label = range_label
    else:
        txns = services.get_transactions(current_user.id, year=year, month=month,
                                         category=category, txn_type=txn_type)
        self_url = url_for("cashflow_centre.transactions", month=month_key,
                            category=category or None, type=txn_type or None)
        clear_filters_url = url_for("cashflow_centre.transactions", month=month_key)
        display_label = month_label(year, month)

    total_income = sum(t.amount for t in txns if t.type == TransactionType.INCOME)
    total_expense = sum(t.amount for t in txns if t.type == TransactionType.EXPENSE)
    net = total_income - total_expense

    return render_template(
        "cashflow_centre/transactions.html",
        transactions=txns,
        total_income=total_income, total_expense=total_expense, net=net,
        month_key=month_key, month_display=display_label,
        prev_month=adjacent_month_key(year, month, -1),
        next_month=adjacent_month_key(year, month, 1),
        is_current_month=(month_key == current_month_key()),
        range_preset=range_preset, is_range_view=bool(from_date and to_date),
        expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
        selected_category=category, selected_type=txn_type,
        self_url=self_url, clear_filters_url=clear_filters_url,
        today_ist=today.strftime("%Y-%m-%d"),
        format_inr=format_inr, format_date=format_date,
    )


@cashflow_bp.route("/transactions/add", methods=["GET", "POST"])
@login_required
def add_transaction():
    default_return = url_for("cashflow_centre.dashboard")
    if request.method == "POST":
        return_to = _safe_next(default_return)
        txn, error = services.create_transaction(_db(), current_user.id, request.form)
        if error:
            flash(error, "error")
            return render_template(
                "cashflow_centre/transaction_form.html", is_edit=False, txn=None,
                expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
                payment_methods=PaymentMethod.ALL, values=request.form,
                today_ist=today_ist().strftime("%Y-%m-%d"), next=return_to,
            )
        flash("Transaction added.", "success")
        # Prefer returning to wherever the user came from, but if that
        # view is scoped to a different month than the new transaction,
        # show the month the transaction actually landed in instead.
        if return_to == default_return or "month=" not in return_to:
            return redirect(url_for("cashflow_centre.dashboard", month=txn.month_key))
        return redirect(return_to)

    return_to = _safe_next(default_return)
    return render_template(
        "cashflow_centre/transaction_form.html", is_edit=False, txn=None,
        expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
        payment_methods=PaymentMethod.ALL,
        values={"date": today_ist().strftime("%Y-%m-%d"), "type": TransactionType.EXPENSE},
        today_ist=today_ist().strftime("%Y-%m-%d"), next=return_to,
    )


@cashflow_bp.route("/transactions/<int:txn_id>/edit", methods=["GET", "POST"])
@login_required
def edit_transaction(txn_id):
    txn = _get_transaction_or_404(txn_id)
    default_return = url_for("cashflow_centre.dashboard", month=txn.month_key)

    if request.method == "POST":
        return_to = _safe_next(default_return)
        updated, error = services.update_transaction(_db(), txn, current_user.id, request.form)
        if error:
            flash(error, "error")
            return render_template(
                "cashflow_centre/transaction_form.html", is_edit=True, txn=txn,
                expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
                payment_methods=PaymentMethod.ALL, values=request.form,
                today_ist=today_ist().strftime("%Y-%m-%d"), next=return_to,
            )
        flash("Transaction updated.", "success")
        return redirect(return_to)

    return_to = _safe_next(default_return)
    values = {
        "date": txn.date.strftime("%Y-%m-%d"), "type": txn.type,
        "category": txn.category, "amount": txn.amount,
        "payment_method": txn.payment_method or "", "description": txn.description or "",
    }
    return render_template(
        "cashflow_centre/transaction_form.html", is_edit=True, txn=txn,
        expense_categories=ExpenseCategory.ALL, income_categories=IncomeCategory.ALL,
        payment_methods=PaymentMethod.ALL, values=values,
        today_ist=today_ist().strftime("%Y-%m-%d"), next=return_to,
    )


@cashflow_bp.route("/transactions/<int:txn_id>/delete", methods=["POST"])
@login_required
def delete_transaction(txn_id):
    txn = _get_transaction_or_404(txn_id)
    default_return = url_for("cashflow_centre.dashboard", month=txn.month_key)
    return_to = _safe_next(default_return)
    success, error = services.delete_transaction(_db(), txn, current_user.id)
    flash(error, "error") if error else flash("Transaction deleted.", "success")
    return redirect(return_to)


# ── Budgets ───────────────────────────────────────────────────────────────────

@cashflow_bp.route("/budgets")
@login_required
def budgets():
    year, month = _resolve_month()
    month_key = f"{year:04d}-{month:02d}"

    rows = services.get_budgets_with_progress(current_user.id, year, month)
    already_budgeted = services.budgeted_categories(current_user.id, year, month)
    available_categories = [c for c in ExpenseCategory.ALL if c not in already_budgeted]
    has_prev_month_budgets = bool(services.budgeted_categories(
        current_user.id, *parse_month_key(adjacent_month_key(year, month, -1))))

    return render_template(
        "cashflow_centre/budgets.html",
        budgets=rows, available_categories=available_categories,
        month_key=month_key, month_display=month_label(year, month),
        prev_month=adjacent_month_key(year, month, -1),
        next_month=adjacent_month_key(year, month, 1),
        is_current_month=(month_key == current_month_key()),
        has_prev_month_budgets=has_prev_month_budgets,
        format_inr=format_inr,
    )


@cashflow_bp.route("/budgets/save", methods=["POST"])
@login_required
def save_budget():
    year, month = _resolve_month()
    budget, error = services.upsert_budget(_db(), current_user.id, year, month, request.form)
    flash(error, "error") if error else flash("Budget saved.", "success")
    return redirect(url_for("cashflow_centre.budgets", month=f"{year:04d}-{month:02d}"))


@cashflow_bp.route("/budgets/copy-last-month", methods=["POST"])
@login_required
def copy_last_month_budgets():
    year, month = _resolve_month()
    count, error = services.copy_last_month_budgets(_db(), current_user.id, year, month)
    if error:
        flash(error, "error")
    else:
        flash(f"Copied {count} budget{'s' if count != 1 else ''} from last month.", "success")
    return redirect(url_for("cashflow_centre.budgets", month=f"{year:04d}-{month:02d}"))


@cashflow_bp.route("/budgets/<int:budget_id>/apply-future", methods=["POST"])
@login_required
def apply_budget_future(budget_id):
    budget = _get_budget_or_404(budget_id)
    month_key = budget.month_key
    count, error = services.apply_budget_to_future_months(_db(), budget, current_user.id)
    if error:
        flash(error, "error")
    else:
        flash(f"Applied this limit to {count} future month{'s' if count != 1 else ''}.", "success")
    return redirect(url_for("cashflow_centre.budgets", month=month_key))


@cashflow_bp.route("/budgets/<int:budget_id>/delete", methods=["POST"])
@login_required
def delete_budget(budget_id):
    budget = _get_budget_or_404(budget_id)
    month_key = budget.month_key
    success, error = services.delete_budget(_db(), budget, current_user.id)
    flash(error, "error") if error else flash("Budget removed.", "success")
    return redirect(url_for("cashflow_centre.budgets", month=month_key))
