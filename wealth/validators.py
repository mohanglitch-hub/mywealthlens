"""
Wealth — Validators
======================
Phase B: server-side validation for the Add/Edit Asset form. This is
the single source of truth for validation — never rely on JavaScript
alone (Section 20 of spec).
"""

from datetime import date, datetime

from .timezone_utils import today_ist

from .models import (
    OwnershipType, SourceType, WealthStatus, WealthAssetCategory,
    WealthDocumentCategory, DOCUMENT_TYPES_BY_CATEGORY,
)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def validate_wealth_asset(form):
    """
    Validate a Wealth asset create/edit submission. `form` is a
    dict-like object. Returns a list of error strings — empty list
    means valid.

    Only the fields Section 20 of the spec explicitly requires are
    mandatory: Category, Asset Type, Asset Name, Current Value,
    Ownership Type, Ownership Percentage. Category-specific fields
    (property address, metal weight, etc.) stay optional — a real
    asset may not have every detail filled in yet, and that
    shouldn't block saving it.
    """
    errors = []

    name = (form.get("name") or "").strip()
    if not name:
        errors.append("Asset name is required.")

    category = (form.get("category") or "").strip()
    if not category:
        errors.append("Category is required.")
    elif category not in WealthAssetCategory.ALL:
        errors.append("Invalid category selected.")

    asset_type = (form.get("asset_type") or "").strip()
    if not asset_type:
        errors.append("Asset type is required.")

    current_value = _parse_float(form.get("current_value"))
    if current_value is None:
        errors.append("Current value is required.")
    elif current_value < 0:
        errors.append("Current value cannot be negative.")

    value_as_of_raw = (form.get("value_as_of") or "").strip()
    if value_as_of_raw:
        try:
            parsed_date = datetime.strptime(value_as_of_raw, "%Y-%m-%d").date()
            if parsed_date > today_ist():
                errors.append("Valuation date cannot be in the future.")
        except ValueError:
            errors.append("Please enter a valid valuation date.")
    # Phase L (Section 62/109): this client-visible, field-error-list
    # check is in ADDITION to (not instead of) the authoritative
    # server-side check inside services.update_asset()/create_asset()
    # — this one gives a nicer redisplay-the-form experience matching
    # every other validation error here; that one is what actually
    # protects the data even if this layer were ever bypassed.

    ownership_type = form.get("ownership_type") or OwnershipType.SOLE
    if ownership_type not in OwnershipType.ALL:
        errors.append("Invalid ownership type selected.")

    ownership_pct_raw = form.get("ownership_percentage")
    ownership_pct = _parse_float(ownership_pct_raw)
    if ownership_pct_raw is None or ownership_pct_raw == "":
        errors.append("Ownership percentage is required.")
    elif ownership_pct is None:
        errors.append("Ownership percentage is invalid.")
    elif ownership_pct < 0 or ownership_pct > 100:
        errors.append("Ownership percentage must be between 0 and 100.")

    source_type = form.get("source_type") or SourceType.SELF_ACQUIRED
    if source_type not in SourceType.ALL:
        errors.append("Invalid source type selected.")

    # ── Acquisition value ────────────────────────────────────────
    acquisition_value = _parse_float(form.get("acquisition_value"))
    if acquisition_value is not None and acquisition_value < 0:
        errors.append("Acquisition value cannot be negative.")

    # ── Dates ────────────────────────────────────────────────────
    for field_name, label in [
        ("acquisition_date", "Acquisition date"),
        ("value_as_of",      "Value as of date"),
        ("date_received",    "Date received"),
        ("maturity_date",    "Maturity date"),
    ]:
        raw = form.get(field_name)
        if raw:
            d = _parse_date(raw)
            if not d:
                errors.append(f"{label} is invalid.")
            elif field_name in ("acquisition_date", "date_received") and d > date.today():
                errors.append(f"{label} cannot be in the future.")

    # ── Category-specific numeric fields ─────────────────────────
    area = _parse_float(form.get("area"))
    if area is not None and area < 0:
        errors.append("Area cannot be negative.")

    weight = _parse_float(form.get("weight"))
    if weight is not None and weight < 0:
        errors.append("Weight cannot be negative.")

    interest_rate = _parse_float(form.get("interest_rate"))
    if interest_rate is not None and (interest_rate < 0 or interest_rate > 100):
        errors.append("Interest rate should be between 0 and 100%.")

    status = form.get("status") or WealthStatus.ACTIVE
    if status not in WealthStatus.ALL:
        errors.append("Invalid status selected.")

    return errors


# ── Wealth Snapshot Validation (Phase F) ──────────────────────────────────────

def validate_snapshot_date(snapshot_date_raw):
    """
    Validate a submitted Wealth History snapshot date. Returns
    (date_obj, error_string) — error_string is None when valid.

    Only rule enforced beyond "is it a real date": it cannot be in
    the future (Section 9 of Phase F spec: "validate the snapshot
    date" — a Wealth position can't be recorded for a date that
    hasn't happened yet). Past dates are always allowed (Section 33:
    manual snapshots may be created for any valid date, not just
    month-end).
    """
    if not snapshot_date_raw:
        return None, "Snapshot date is required."

    d = _parse_date(snapshot_date_raw)
    if not d:
        return None, "Snapshot date is invalid."

    if d > date.today():
        return None, "Snapshot date cannot be in the future."

    return d, None


# ── Wealth Document Validation (Phase G) ──────────────────────────────────────

ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx", ".xls", ".xlsx",
}

MAX_DOCUMENT_SIZE_BYTES = 25 * 1024 * 1024  # 25MB


def validate_document(file, category, document_type):
    """
    Validate an uploaded Wealth document. `file` is a werkzeug
    FileStorage object. Returns a list of error strings (empty list
    = valid).
    """
    errors = []

    if not file or not file.filename:
        errors.append("No file selected.")
        return errors

    if category not in WealthDocumentCategory.ALL:
        errors.append("Invalid document category selected.")
    elif document_type not in DOCUMENT_TYPES_BY_CATEGORY.get(category, []):
        errors.append("Invalid document type for the selected category.")

    ext = _file_extension(file.filename)
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        errors.append(
            f"File type '{ext}' not allowed. "
            f"Allowed: PDF, JPG, JPEG, PNG, DOC, DOCX, XLS, XLSX."
        )

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size == 0:
        errors.append("The selected file is empty.")
    elif size > MAX_DOCUMENT_SIZE_BYTES:
        errors.append("File size exceeds the 25MB limit.")

    return errors


def _file_extension(filename):
    import os
    return os.path.splitext(filename)[1].lower()


# ── Liability Validation (Phase C) ────────────────────────────────────────────

def validate_wealth_liability(form):
    """
    Validate a Wealth liability create/edit submission.

    DOCUMENTED VALIDATION BEHAVIOUR (Section 65 requires this be
    explicit, not just implemented):

    - Outstanding Amount <= Original Amount is enforced ONLY when
      Original Amount is provided AND greater than zero. If Original
      Amount is left blank or entered as 0, it's treated as "unknown"
      and this check is skipped entirely — Outstanding Amount alone
      is still required and validated as >= 0 regardless.
    - Maturity/End Date >= Start Date is enforced ONLY when BOTH
      dates are provided. Neither date is required on its own.
    - Interest Rate has no hard business meaning (Section 12: purely
      informational) but is still bounded to a sane 0-100% range to
      catch obvious data-entry mistakes, not to enforce any real
      financial rule.
    """
    from .models import WealthLiabilityCategory

    errors = []

    name = (form.get("name") or "").strip()
    if not name:
        errors.append("Liability name is required.")

    category = (form.get("category") or "").strip()
    if not category:
        errors.append("Category is required.")
    elif category not in WealthLiabilityCategory.ALL:
        errors.append("Invalid category selected.")

    liability_type = (form.get("liability_type") or "").strip()
    if not liability_type:
        errors.append("Liability type is required.")

    original_amount = _parse_float(form.get("original_amount"))
    if original_amount is not None and original_amount < 0:
        errors.append("Original amount cannot be negative.")

    outstanding_amount = _parse_float(form.get("outstanding_amount"))
    if outstanding_amount is None:
        errors.append("Outstanding amount is required.")
    elif outstanding_amount < 0:
        errors.append("Outstanding amount cannot be negative.")

    # Section 11: outstanding <= original, ONLY when original is
    # provided and genuinely positive (not "unknown"/0).
    if (original_amount is not None and original_amount > 0
            and outstanding_amount is not None
            and outstanding_amount > original_amount):
        errors.append("Outstanding amount cannot exceed the original amount.")

    balance_as_of_raw = (form.get("balance_as_of") or "").strip()
    if balance_as_of_raw:
        try:
            parsed_date = datetime.strptime(balance_as_of_raw, "%Y-%m-%d").date()
            if parsed_date > today_ist():
                errors.append("Valuation date cannot be in the future.")
        except ValueError:
            errors.append("Please enter a valid valuation date.")

    interest_rate = _parse_float(form.get("interest_rate"))
    if interest_rate is not None and (interest_rate < 0 or interest_rate > 100):
        errors.append("Interest rate should be between 0 and 100%.")

    ownership_type = form.get("ownership_type") or OwnershipType.SOLE
    if ownership_type not in OwnershipType.ALL:
        errors.append("Invalid ownership type selected.")

    ownership_pct_raw = form.get("ownership_percentage")
    ownership_pct = _parse_float(ownership_pct_raw)
    if ownership_pct_raw is None or ownership_pct_raw == "":
        errors.append("Ownership percentage is required.")
    elif ownership_pct is None:
        errors.append("Ownership percentage is invalid.")
    elif ownership_pct < 0 or ownership_pct > 100:
        errors.append("Ownership percentage must be between 0 and 100.")

    # Section 13: maturity >= start, only if BOTH provided.
    start_date_raw = form.get("start_date")
    end_date_raw = form.get("expected_end_date")
    start_date = _parse_date(start_date_raw) if start_date_raw else None
    end_date = _parse_date(end_date_raw) if end_date_raw else None

    if start_date_raw and not start_date:
        errors.append("Start date is invalid.")
    if end_date_raw and not end_date:
        errors.append("Maturity / end date is invalid.")
    if start_date and end_date and end_date < start_date:
        errors.append("Maturity / end date cannot be before the start date.")

    status = form.get("status") or WealthStatus.ACTIVE
    if status not in WealthStatus.ALL:
        errors.append("Invalid status selected.")

    return errors
