"""
Retirement Centre — Validators
=================================
Phase B: Backend validation for scheme create/edit.

Mirrors insurance_centre/validators.py's approach — validation lives
here, never solely in JavaScript (Section 22 of Phase B spec).
Returns a list of error strings; empty list means valid.
"""

from datetime import date, datetime

from .models import SchemeType, GrowthMethod, SchemeStatus, NPSTier


def _parse_date(value):
    """Parse a YYYY-MM-DD string into a date object. Returns None if empty/invalid."""
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


def _parse_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def validate_scheme(form):
    """
    Validate a scheme create/edit form submission.
    `form` is a dict-like object (request.form.to_dict() from the route).
    Returns a list of error strings. Empty list = valid.
    """
    errors = []

    scheme_type = (form.get("scheme_type") or "").strip()
    if not scheme_type:
        errors.append("Please select a scheme type.")
    elif scheme_type not in SchemeType.ALL:
        errors.append("Invalid scheme type selected.")

    # Custom scheme name required only for "Other (Custom)"
    if scheme_type == SchemeType.CUSTOM:
        custom_name = (form.get("custom_type") or "").strip()
        if not custom_name:
            errors.append("Custom Scheme Name is required for a custom scheme.")

    institution = (form.get("institution") or "").strip()
    if not institution:
        errors.append("Institution is required.")

    # ── Opening date ─────────────────────────────────────────────
    opening_date_raw = form.get("opening_date")
    if not opening_date_raw:
        errors.append("Opening date is required.")
    else:
        opening_date = _parse_date(opening_date_raw)
        if not opening_date:
            errors.append("Opening date is invalid.")
        elif opening_date > date.today():
            errors.append("Opening date cannot be in the future.")

    # ── Current balance ──────────────────────────────────────────
    balance_raw = form.get("current_balance")
    if balance_raw is None or str(balance_raw).strip() == "":
        errors.append("Current balance is required — enter 0 if the scheme has no balance yet.")
    else:
        balance = _parse_float(balance_raw)
        if balance is None:
            errors.append("Current balance is invalid.")
        elif balance < 0:
            errors.append("Current balance cannot be negative.")

    # ── Growth method / rate ─────────────────────────────────────
    growth_method = form.get("growth_method") or GrowthMethod.GOVERNMENT_DECLARED
    if growth_method not in GrowthMethod.ALL:
        errors.append("Invalid growth method selected.")

    rate = _parse_float(form.get("rate_or_return_assumption"))
    if rate is not None and (rate < 0 or rate > 100):
        errors.append("Interest rate / return assumption should be between 0 and 100%.")

    # ── Status ───────────────────────────────────────────────────
    status = form.get("status") or SchemeStatus.ACTIVE
    if status not in SchemeStatus.ALL:
        errors.append("Invalid status selected.")

    # ── Contribution percentages (EPF/VPF/NPS) ──────────────────
    employee_pct = _parse_float(form.get("employee_contribution_pct"))
    if employee_pct is not None and (employee_pct < 0 or employee_pct > 100):
        errors.append("Employee contribution % must be between 0 and 100.")

    employer_pct = _parse_float(form.get("employer_contribution_pct"))
    if employer_pct is not None and (employer_pct < 0 or employer_pct > 100):
        errors.append("Employer contribution % must be between 0 and 100.")

    # ── Target retirement year (EPF/VPF/NPS/Superannuation) ─────
    target_year = _parse_int(form.get("target_retirement_year"))
    if target_year is not None:
        current_year = date.today().year
        if target_year < current_year or target_year > current_year + 80:
            errors.append("Target retirement year is out of a reasonable range.")

    # ── SSY: girl child DOB ──────────────────────────────────────
    if scheme_type == SchemeType.SSY:
        dob_raw = form.get("girl_child_dob")
        if not dob_raw:
            errors.append("Girl Child Date of Birth is required for SSY.")
        else:
            dob = _parse_date(dob_raw)
            if not dob:
                errors.append("Girl Child Date of Birth is invalid.")
            elif dob > date.today():
                errors.append("Girl Child Date of Birth cannot be in the future.")

    # ── NPS: PRAN + Tier ─────────────────────────────────────────
    if scheme_type == SchemeType.NPS:
        pran = (form.get("pran_number") or "").strip()
        if not pran:
            errors.append("PRAN is required for NPS.")
        tier = (form.get("tier") or "").strip()
        if not tier:
            errors.append("Tier is required for NPS.")
        elif tier not in NPSTier.ALL:
            errors.append("Invalid NPS Tier selected.")

    return errors


# ── Contribution Validation (Phase C) ─────────────────────────────────────────

def validate_contribution(form):
    """Validate a contribution add/edit submission."""
    errors = []

    date_raw = form.get("contribution_date")
    if not date_raw:
        errors.append("Contribution date is required.")
    elif not _parse_date(date_raw):
        errors.append("Contribution date is invalid.")

    amount = _parse_float(form.get("amount"))
    if amount is None:
        errors.append("Amount is required.")
    elif amount <= 0:
        errors.append("Amount must be greater than zero.")

    from .models import ContributionEntryType
    entry_type = form.get("entry_type")
    if entry_type and entry_type not in ContributionEntryType.ALL:
        errors.append("Invalid entry type selected.")

    return errors


# ── Balance Update Validation (Phase C) ───────────────────────────────────────

def validate_balance_update(form):
    """Validate a balance-update submission (creates a snapshot)."""
    errors = []

    balance = _parse_float(form.get("new_balance"))
    if balance is None:
        errors.append("New balance is required.")
    elif balance < 0:
        errors.append("Balance cannot be negative.")

    date_raw = form.get("balance_date")
    if not date_raw:
        errors.append("Balance date is required.")
    elif not _parse_date(date_raw):
        errors.append("Balance date is invalid.")

    return errors


# ── Nominee Validation (Phase C) ──────────────────────────────────────────────

def validate_nominee(form):
    """Validate a nominee add/edit submission."""
    errors = []

    if not (form.get("name") or "").strip():
        errors.append("Nominee name is required.")
    if not (form.get("relationship") or "").strip():
        errors.append("Nominee relationship is required.")

    pct = _parse_float(form.get("percentage"))
    if pct is not None and (pct < 0 or pct > 100):
        errors.append("Nominee percentage must be between 0 and 100.")

    return errors


# ── Document Validation (Phase D) ─────────────────────────────────────────────

def validate_document(file, doc_type):
    """
    Validate an uploaded document. `file` is the Werkzeug FileStorage
    object from request.files.get(...).
    """
    import os
    from .models import RetirementDocumentType
    from .utils import ALLOWED_DOCUMENT_EXTENSIONS, MAX_DOCUMENT_SIZE_BYTES

    errors = []

    if not file or not file.filename:
        errors.append("Please choose a file to upload.")
        return errors

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_DOCUMENT_EXTENSIONS))
        errors.append(f"Unsupported file type. Allowed: {allowed}")

    if not doc_type or doc_type not in RetirementDocumentType.ALL:
        errors.append("Please select a valid document type.")

    # Determine size without reading the whole file into memory twice
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size > MAX_DOCUMENT_SIZE_BYTES:
        errors.append(f"File is too large (max "
                      f"{MAX_DOCUMENT_SIZE_BYTES // (1024*1024)} MB).")
    if size == 0:
        errors.append("The selected file appears to be empty.")

    return errors
