"""
Cashflow Centre — Validators
==============================
Pure functions, no DB/network access — return a list of error strings
(empty list = valid), matching the convention used across every other
module's validators.py.
"""
from datetime import datetime

from cashflow_centre.models import (
    TransactionType, ExpenseCategory, IncomeCategory, PaymentMethod,
)
from wealth.timezone_utils import today_ist


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def validate_transaction(data):
    """
    data: flat dict (request.form.to_dict() or equivalent) with keys
    date, type, category, amount, payment_method (optional),
    description (optional).
    """
    errors = []

    txn_type = (data.get("type") or "").strip()
    if txn_type not in TransactionType.ALL:
        errors.append("Please select whether this is income or an expense.")
        txn_type = None

    category = (data.get("category") or "").strip()
    valid_categories = (ExpenseCategory.ALL if txn_type == TransactionType.EXPENSE
                        else IncomeCategory.ALL if txn_type == TransactionType.INCOME
                        else [])
    if txn_type and category not in valid_categories:
        errors.append("Please select a valid category for this transaction type.")

    date_str = (data.get("date") or "").strip()
    parsed_date = _parse_date(date_str)
    if not parsed_date:
        errors.append("Please enter a valid date.")
    elif parsed_date > today_ist():
        errors.append("Transaction date cannot be in the future.")

    amount_raw = (data.get("amount") or "").strip()
    try:
        amount = float(amount_raw)
        if amount <= 0:
            errors.append("Amount must be greater than zero.")
    except ValueError:
        errors.append("Please enter a valid amount.")

    payment_method = (data.get("payment_method") or "").strip()
    if payment_method and payment_method not in PaymentMethod.ALL:
        errors.append("Please select a valid payment method.")

    return errors


def validate_budget(data):
    """data: flat dict with keys category, monthly_limit."""
    errors = []

    category = (data.get("category") or "").strip()
    if category not in ExpenseCategory.ALL:
        errors.append("Please select a valid expense category.")

    limit_raw = (data.get("monthly_limit") or "").strip()
    try:
        limit = float(limit_raw)
        if limit <= 0:
            errors.append("Monthly limit must be greater than zero.")
    except ValueError:
        errors.append("Please enter a valid monthly limit.")

    return errors


def validate_recurring(data):
    """data: flat dict with keys name, type, category, amount, day_of_month."""
    errors = []

    name = (data.get("name") or "").strip()
    if not name:
        errors.append("Please enter a name for this recurring payment.")
    elif len(name) > 100:
        errors.append("Name must be under 100 characters.")

    txn_type = (data.get("type") or TransactionType.EXPENSE).strip()
    if txn_type not in TransactionType.ALL:
        errors.append("Please select whether this is income or an expense.")
        txn_type = None

    category = (data.get("category") or "").strip()
    valid_categories = (ExpenseCategory.ALL if txn_type == TransactionType.EXPENSE
                        else IncomeCategory.ALL if txn_type == TransactionType.INCOME
                        else [])
    if txn_type and category not in valid_categories:
        errors.append("Please select a valid category.")

    amount_raw = (data.get("amount") or "").strip()
    try:
        amount = float(amount_raw)
        if amount <= 0:
            errors.append("Amount must be greater than zero.")
    except ValueError:
        errors.append("Please enter a valid amount.")

    day_raw = (data.get("day_of_month") or "").strip()
    try:
        day = int(day_raw)
        if not (1 <= day <= 31):
            errors.append("Day of month must be between 1 and 31.")
    except ValueError:
        errors.append("Please enter a valid day of month.")

    return errors
