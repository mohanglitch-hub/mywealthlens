"""
International Investing Centre — Validators
===============================================
Pure functions, no DB/network access — return a list of error strings
(empty list = valid), matching the convention used across every other
module's validators.py.
"""
from datetime import datetime

from international_centre.models import (
    InternationalAssetType, InternationalTxnType, RemittancePurpose,
)
from fx_rates import SUPPORTED_CURRENCIES
from wealth.timezone_utils import today_ist


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def validate_holding(data):
    """data: flat dict with keys asset_type, name, ticker (optional),
    country (optional), broker_or_institution (optional),
    account_number_masked (optional), native_currency, quantity
    (optional), avg_cost_native (optional), current_value_native
    (required for non-ticker-based types)."""
    errors = []

    asset_type = (data.get("asset_type") or "").strip()
    if asset_type not in InternationalAssetType.ALL:
        errors.append("Please select a valid asset type.")
        asset_type = None

    name = (data.get("name") or "").strip()
    if not name:
        errors.append("Please enter a name for this holding.")
    elif len(name) > 200:
        errors.append("Name must be under 200 characters.")

    currency = (data.get("native_currency") or "").strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        errors.append("Please select a valid currency.")

    is_ticker_based = asset_type in InternationalAssetType.TICKER_BASED if asset_type else False

    if is_ticker_based:
        ticker = (data.get("ticker") or "").strip()
        if not ticker:
            errors.append("Please enter a ticker symbol for this holding.")

        qty_raw = (data.get("quantity") or "").strip()
        try:
            quantity = float(qty_raw)
            if quantity <= 0:
                errors.append("Quantity must be greater than zero.")
        except ValueError:
            errors.append("Please enter a valid quantity.")

        cost_raw = (data.get("avg_cost_native") or "").strip()
        try:
            avg_cost = float(cost_raw)
            if avg_cost < 0:
                errors.append("Average cost cannot be negative.")
        except ValueError:
            errors.append("Please enter a valid average cost per unit.")
    else:
        value_raw = (data.get("current_value_native") or "").strip()
        try:
            value = float(value_raw)
            if value < 0:
                errors.append("Current value cannot be negative.")
        except ValueError:
            errors.append("Please enter a valid current value.")

    account_masked = (data.get("account_number_masked") or "").strip()
    if account_masked and len(account_masked) > 20 and not account_masked.lower().startswith("x"):
        # Not a hard block — just a nudge toward the "last 4 digits"
        # convention documented in models.py, since this field is easy
        # to accidentally paste a full account number into.
        errors.append("Please enter only the last few digits of the account number, not the full number.")

    return errors


def validate_transaction(data):
    """data: flat dict with keys date, txn_type, quantity (optional),
    price_native (optional), amount_native."""
    errors = []

    txn_type = (data.get("txn_type") or "").strip().upper()
    if txn_type not in InternationalTxnType.ALL:
        errors.append("Please select a valid transaction type.")

    date_str = (data.get("date") or "").strip()
    parsed_date = _parse_date(date_str)
    if not parsed_date:
        errors.append("Please enter a valid date.")
    elif parsed_date > today_ist():
        errors.append("Transaction date cannot be in the future.")

    amount_raw = (data.get("amount_native") or "").strip()
    try:
        amount = float(amount_raw)
        if amount <= 0:
            errors.append("Amount must be greater than zero.")
    except ValueError:
        errors.append("Please enter a valid amount.")

    if txn_type in (InternationalTxnType.BUY, InternationalTxnType.SELL):
        qty_raw = (data.get("quantity") or "").strip()
        try:
            quantity = float(qty_raw)
            if quantity <= 0:
                errors.append("Quantity must be greater than zero for a buy/sell transaction.")
        except ValueError:
            errors.append("Please enter a valid quantity for a buy/sell transaction.")

    return errors


def validate_remittance(data):
    """data: flat dict with keys date, amount_inr, purpose,
    remitting_bank (optional)."""
    errors = []

    date_str = (data.get("date") or "").strip()
    parsed_date = _parse_date(date_str)
    if not parsed_date:
        errors.append("Please enter a valid date.")
    elif parsed_date > today_ist():
        errors.append("Remittance date cannot be in the future.")

    amount_raw = (data.get("amount_inr") or "").strip()
    try:
        amount = float(amount_raw)
        if amount <= 0:
            errors.append("Amount must be greater than zero.")
    except ValueError:
        errors.append("Please enter a valid amount.")

    purpose = (data.get("purpose") or "").strip()
    if purpose not in RemittancePurpose.ALL:
        errors.append("Please select a valid purpose.")

    return errors
