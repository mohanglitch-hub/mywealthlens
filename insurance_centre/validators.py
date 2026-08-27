"""
Insurance Centre — Validators
==============================
All validation logic for the Insurance Centre.
Returns lists of error strings. Empty list = valid.
"""

import os
from datetime import date
from .models import (
    InsurancePolicy, InsuranceCategory, InsuranceType,
    PolicyStatus, PremiumFrequency, NomineeRelation,
    MemberRelation, DocumentType
)


# ── Policy Validation ─────────────────────────────────────────────────────────

def validate_policy(data, user_id, existing_policy_id=None):
    """
    Validate policy create/update data.
    Returns list of error strings. Empty = valid.
    """
    errors = []

    # Required fields
    category = data.get("category", "").strip()
    if not category:
        errors.append("Insurance category is required.")
    elif category not in InsuranceCategory.ALL:
        errors.append(f"Invalid category: {category}.")

    insurance_type = data.get("insurance_type", "").strip()
    if not insurance_type:
        errors.append("Insurance type is required.")
    elif category in InsuranceCategory.ALL:
        valid_types = InsuranceType.for_category(category)
        if insurance_type not in valid_types:
            errors.append(f"Invalid type '{insurance_type}' for {category}.")

    # Custom type required when type is Other
    if insurance_type == "Other (Custom)":
        if not data.get("custom_type", "").strip():
            errors.append("Custom type description is required when 'Other' is selected.")

    insurer = data.get("insurer", "").strip()
    if not insurer:
        errors.append("Insurance company name is required.")

    # Numeric validations
    try:
        sum_assured = float(data.get("sum_assured", 0) or 0)
        if sum_assured < 0:
            errors.append("Sum assured cannot be negative.")
    except (ValueError, TypeError):
        errors.append("Sum assured must be a valid number.")

    try:
        premium = float(data.get("premium_amount", 0) or 0)
        if premium < 0:
            errors.append("Premium amount cannot be negative.")
    except (ValueError, TypeError):
        errors.append("Premium amount must be a valid number.")

    # Premium frequency
    frequency = data.get("premium_frequency", PremiumFrequency.YEARLY)
    if frequency not in PremiumFrequency.ALL:
        errors.append(f"Invalid premium frequency: {frequency}.")

    # Status
    status = data.get("status", PolicyStatus.ACTIVE)
    if status not in PolicyStatus.ALL:
        errors.append(f"Invalid status: {status}.")

    # Date validations
    start_date    = _parse_date(data.get("start_date"))
    maturity_date = _parse_date(data.get("maturity_date"))
    renewal_date  = _parse_date(data.get("renewal_date"))
    expiry_date   = _parse_date(data.get("expiry_date"))

    if start_date and maturity_date:
        if maturity_date <= start_date:
            errors.append("Maturity date must be after start date.")

    if start_date and expiry_date:
        if expiry_date < start_date:
            errors.append("Expiry date cannot be before start date.")

    # Confirmed real gap: renewal_date was parsed but never actually
    # validated against anything (found via a dead-code sweep — a
    # `renewal_date` variable that was assigned but never read is
    # exactly the kind of thing that flags a missing check, not just
    # unused code). days_to_renewal (models.py) treats this as a
    # forward-looking date, so the same start-date sanity check
    # already applied to maturity_date/expiry_date belongs here too.
    if start_date and renewal_date:
        if renewal_date < start_date:
            errors.append("Renewal date cannot be before start date.")

    # Duplicate policy number check (same user, different policy)
    policy_number = data.get("policy_number", "").strip()
    if policy_number:
        query = InsurancePolicy.query.filter_by(
            user_id=user_id,
            policy_number=policy_number,
            is_archived=False
        )
        if existing_policy_id:
            query = query.filter(InsurancePolicy.id != existing_policy_id)
        if query.first():
            errors.append(
                f"Policy number '{policy_number}' already exists for your account."
            )

    return errors


# ── Nominee Validation ────────────────────────────────────────────────────────

def validate_nominee(data, policy):
    """
    Validate nominee data.
    Checks nominee % does not push total over 100%.
    """
    errors = []

    name = data.get("name", "").strip()
    if not name:
        errors.append("Nominee name is required.")

    relationship = data.get("relationship", "").strip()
    if not relationship:
        errors.append("Nominee relationship is required.")
    elif relationship not in NomineeRelation.ALL:
        errors.append(f"Invalid relationship: {relationship}.")

    percentage = data.get("percentage")
    if percentage not in (None, "", 0, "0"):
        try:
            pct = float(percentage)
            if pct < 0:
                errors.append("Nominee percentage cannot be negative.")
            elif pct > 100:
                errors.append("Nominee percentage cannot exceed 100%.")
            else:
                current_total = policy.total_nominees_percentage
                if current_total + pct > 100:
                    remaining = 100 - current_total
                    errors.append(
                        f"Total nominee percentage would exceed 100%. "
                        f"Maximum you can assign: {remaining:.1f}%."
                    )
        except (ValueError, TypeError):
            errors.append("Nominee percentage must be a valid number.")

    contact = data.get("contact", "").strip()
    if contact and not contact.replace("+", "").replace("-", "").replace(" ", "").isdigit():
        errors.append("Contact number should contain only digits, +, - or spaces.")

    return errors


# ── Health Member Validation ──────────────────────────────────────────────────

def validate_member(data, policy):
    """Validate health insured member data."""
    errors = []

    if policy.category != InsuranceCategory.HEALTH:
        errors.append("Members can only be added to Health Insurance policies.")
        return errors

    name = data.get("member_name", "").strip()
    if not name:
        errors.append("Member name is required.")

    relationship = data.get("relationship", "").strip()
    if not relationship:
        errors.append("Relationship is required.")
    elif relationship not in MemberRelation.ALL:
        errors.append(f"Invalid relationship: {relationship}.")

    age = data.get("age")
    if age not in (None, "", 0, "0"):
        try:
            age_int = int(age)
            if age_int < 0:
                errors.append("Age cannot be negative.")
            elif age_int > 120:
                errors.append("Age seems invalid (over 120).")
        except (ValueError, TypeError):
            errors.append("Age must be a valid number.")

    return errors


# ── Document Validation ───────────────────────────────────────────────────────

def validate_document(file, doc_type):
    """
    Validate uploaded document.
    file: werkzeug FileStorage object
    """
    errors = []

    if not file or not file.filename:
        errors.append("No file selected.")
        return errors

    if doc_type not in DocumentType.ALL:
        errors.append(f"Invalid document type: {doc_type}.")

    # Allowed extensions
    allowed = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx", ".xls", ".xlsx"}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed:
        errors.append(
            f"File type '{ext}' not allowed. "
            f"Allowed: PDF, JPG, JPEG, PNG, DOC, DOCX, XLS, XLSX."
        )

    # Size check — max 25MB
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 25 * 1024 * 1024:
        errors.append("File size exceeds 25MB limit.")

    return errors


# ── Renewal Validation ────────────────────────────────────────────────────────

def validate_renewal(data, policy):
    """Validate renewal date inputs."""
    errors = []

    new_renewal = _parse_date(data.get("new_renewal_date"))
    if not new_renewal:
        errors.append("New renewal date is required.")
    elif new_renewal <= date.today():
        errors.append("New renewal date must be in the future.")
    elif policy.renewal_date and new_renewal <= policy.renewal_date:
        errors.append(
            "New renewal date must be after the current renewal date "
            f"({policy.renewal_date})."
        )

    return errors


# ── Private Helpers ───────────────────────────────────────────────────────────

def _parse_date(value):
    """Parse date string safely. Returns None if empty or invalid."""
    if not value:
        return None
    try:
        from datetime import datetime
        if isinstance(value, date):
            return value
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None