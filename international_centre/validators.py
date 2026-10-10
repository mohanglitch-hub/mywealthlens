"""
International Investing Centre — Validators
===============================================
Pure functions, no DB/network access — return a list of error strings
(empty list = valid), matching the convention used across every other
module's validators.py.
"""
import os
from datetime import datetime

from international_centre.models import (
    InternationalAssetType, InternationalTxnType, RemittancePurpose,
    DocumentType, VestingPlanType,
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

    # Batch 11 — Schedule FA Table A3 fields (all optional, length-checked here
    # because the database columns are bounded).
    for key, limit, label in (("entity_address", 255, "address"), ("entity_zip", 20, "ZIP code"),
                              ("entity_nature", 60, "nature of entity")):
        if len((data.get(key) or "").strip()) > limit:
            errors.append(f"The {label} is too long (maximum {limit} characters).")

    return errors


def _validate_ttbr_override(data, errors):
    """Batch 11 (Oct 2026) — optional per-event SBI TT buying rate
    (INR per 1 unit of the holding's currency) and the date it belongs to.
    Shared by transactions and vesting tranches."""
    raw = (data.get("ttbr_override") or "").strip()
    date_raw = (data.get("ttbr_override_date") or "").strip()
    if raw:
        try:
            rate = float(raw)
            if not (0.001 <= rate <= 5000):
                errors.append("The SBI rate looks wrong; enter rupees per 1 unit of the currency (for example 84.25).")
        except ValueError:
            errors.append("Please enter a valid SBI rate, or leave it blank.")
        if date_raw:
            d = _parse_date(date_raw)
            if not d:
                errors.append("Please enter a valid date for the SBI rate, or leave it blank.")
            elif d > today_ist():
                errors.append("The SBI rate's date cannot be in the future.")
    elif date_raw:
        errors.append("Enter the SBI rate as well, or clear its date.")



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

    # Batch 9.5 (Sep 2026) — optional withholding-tax detail, DIVIDEND only.
    if txn_type == InternationalTxnType.DIVIDEND:
        gross_raw = (data.get("gross_amount_native") or "").strip()
        withheld_raw = (data.get("tax_withheld_native") or "").strip()
        if gross_raw:
            try:
                gross = float(gross_raw)
                if gross <= 0:
                    errors.append("Gross dividend amount must be greater than zero.")
            except ValueError:
                errors.append("Please enter a valid gross dividend amount.")
                gross = None
            if withheld_raw:
                try:
                    withheld = float(withheld_raw)
                    if withheld < 0:
                        errors.append("Tax withheld cannot be negative.")
                    elif gross is not None and withheld > gross:
                        errors.append("Tax withheld cannot exceed the gross dividend amount.")
                except ValueError:
                    errors.append("Please enter a valid tax-withheld amount.")
        elif withheld_raw:
            errors.append("Enter the gross dividend amount before entering tax withheld.")

    _validate_ttbr_override(data, errors)
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


def validate_vesting_tranche(data):
    """data: flat dict with keys plan_type, grant_date (optional),
    vest_date, quantity, fmv_native, purchase_price_native (optional).
    Batch 9.7 (Sep 2026)."""
    errors = []

    plan_type = (data.get("plan_type") or "").strip().upper()
    if plan_type not in VestingPlanType.ALL:
        errors.append("Please select a valid plan type (RSU or ESPP).")

    grant_raw = (data.get("grant_date") or "").strip()
    if grant_raw and not _parse_date(grant_raw):
        errors.append("Please enter a valid grant date.")

    vest_raw = (data.get("vest_date") or "").strip()
    vest_date = _parse_date(vest_raw)
    if not vest_date:
        errors.append("Please enter a valid vest date.")
    elif vest_date > today_ist():
        errors.append("Vest date cannot be in the future.")

    qty_raw = (data.get("quantity") or "").strip()
    try:
        quantity = float(qty_raw)
        if quantity <= 0:
            errors.append("Quantity must be greater than zero.")
    except ValueError:
        errors.append("Please enter a valid quantity.")

    fmv = None
    fmv_raw = (data.get("fmv_native") or "").strip()
    try:
        fmv = float(fmv_raw)
        if fmv <= 0:
            errors.append("Fair market value must be greater than zero.")
    except ValueError:
        errors.append("Please enter a valid fair market value.")

    price_raw = (data.get("purchase_price_native") or "").strip()
    if price_raw:
        try:
            price = float(price_raw)
            if price < 0:
                errors.append("Purchase price cannot be negative.")
            elif fmv is not None and price > fmv:
                errors.append("Purchase price cannot exceed the fair market value.")
        except ValueError:
            errors.append("Please enter a valid purchase price.")

    _validate_ttbr_override(data, errors)
    return errors


def validate_document(file, doc_type):
    """Validate an uploaded document. file: werkzeug FileStorage.
    Same allowed-extensions/size-limit rules as every other module's
    Document Vault (own copy, per convention)."""
    errors = []

    if not file or not file.filename:
        errors.append("No file selected.")
        return errors

    if doc_type not in DocumentType.ALL:
        errors.append(f"Invalid document type: {doc_type}.")

    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx", ".xls", ".xlsx"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        errors.append(
            f"File type '{ext}' not allowed. "
            f"Allowed: PDF, JPG, JPEG, PNG, DOC, DOCX, XLS, XLSX."
        )

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 25 * 1024 * 1024:
        errors.append("File size exceeds 25MB limit.")

    return errors
