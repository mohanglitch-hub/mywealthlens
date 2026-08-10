"""
Retirement Centre — Utilities
================================
Shared helpers used across services and routes.

Currency/date formatting mirrors insurance_centre/utils.py's approach
to keep display conventions consistent app-wide. Kept self-contained
here rather than cross-imported from insurance_centre, matching this
project's existing per-module utils.py convention.
"""

from datetime import date, datetime as _dt


# ── Display Helpers ───────────────────────────────────────────────────────────

def format_inr(value):
    """Format a number as Indian Rupees (Cr/L notation)."""
    if value is None:
        return "—"
    if value >= 10_000_000:
        return f"₹{value/10_000_000:.2f} Cr"
    if value >= 100_000:
        return f"₹{value/100_000:.2f} L"
    return f"₹{value:,.0f}"


def format_date(d, fmt="%d %b %Y"):
    """Format a date or datetime object for display. Returns '—' if None."""
    if not d:
        return "—"
    try:
        if isinstance(d, _dt):
            return d.strftime(fmt)
        if isinstance(d, date):
            return d.strftime(fmt)
        return _dt.strptime(str(d)[:10], "%Y-%m-%d").strftime(fmt)
    except Exception:
        return str(d)


def days_until(target_date):
    """Returns days until a date. Negative if past. None if no date given."""
    if not target_date:
        return None
    return (target_date - date.today()).days


# ── Financial Year Helpers ────────────────────────────────────────────────────
# Indian Financial Year: 1 April → 31 March.
# This is the single reusable source of truth for FY logic — never
# duplicate this calculation in a template or route (Section 10 of spec).

def financial_year_for_date(d):
    """
    Returns the Indian Financial Year label for a given date.

        01-Apr-2025  ->  "FY 2025-26"
        31-Mar-2026  ->  "FY 2025-26"
        01-Apr-2026  ->  "FY 2026-27"

    Returns None if d is None.
    """
    if not d:
        return None
    start_year = d.year if d.month >= 4 else d.year - 1
    return f"FY {start_year}-{str(start_year + 1)[-2:]}"


def financial_year_bounds(fy_start_year):
    """
    Given a FY start year (e.g. 2025 for FY 2025-26), returns
    (start_date, end_date) as date objects: (1 Apr 2025, 31 Mar 2026).
    """
    return date(fy_start_year, 4, 1), date(fy_start_year + 1, 3, 31)


def current_financial_year():
    """Returns the FY start year containing today's date."""
    today = date.today()
    return today.year if today.month >= 4 else today.year - 1


def previous_financial_year():
    """Returns the FY start year immediately before the current one."""
    return current_financial_year() - 1


# ── Document Storage (Phase D) ────────────────────────────────────────────────
# Mirrors insurance_centre/utils.py's approach — kept as a separate,
# self-contained implementation rather than importing from
# insurance_centre, consistent with this project's per-module
# utils.py convention (established back in Phase A).

import os
import uuid


ALLOWED_DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
MAX_DOCUMENT_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB per file


def get_document_upload_path(scheme_id):
    """
    Local directory for a scheme's documents. Creates it if missing.
    Path: instance/documents/retirement/<scheme_id>/
    """
    from flask import current_app
    base = os.path.join(
        current_app.instance_path,
        "documents", "retirement", str(scheme_id)
    )
    os.makedirs(base, exist_ok=True)
    return base


def generate_stored_filename(original_filename):
    """UUID-based filename — never stores the original name on disk."""
    ext = os.path.splitext(original_filename)[1].lower()
    return f"{uuid.uuid4()}{ext}"


def save_document_file(file, scheme_id):
    """
    Save an uploaded file locally. Returns (stored_name, file_path, file_size).
    Raises OSError on failure.
    """
    upload_dir  = get_document_upload_path(scheme_id)
    stored_name = generate_stored_filename(file.filename)
    file_path   = os.path.join(upload_dir, stored_name)
    file.save(file_path)
    file_size = os.path.getsize(file_path)
    return stored_name, file_path, file_size


def delete_document_file(file_path):
    """Delete a document file from local storage. Silent if missing."""
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            return True
    except OSError:
        pass
    return False


def secure_file_path(file_path, scheme_id):
    """
    Validate that file_path is within the expected scheme documents
    directory. Prevents path-traversal attacks.
    """
    from flask import current_app
    expected_base = os.path.join(
        current_app.instance_path, "documents", "retirement", str(scheme_id)
    )
    real_path = os.path.realpath(file_path)
    real_base = os.path.realpath(expected_base)
    return real_path.startswith(real_base)


PREVIEWABLE_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def is_previewable(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ext in PREVIEWABLE_EXTENSIONS


def get_preview_mimetype(filename):
    ext = os.path.splitext(filename)[1].lower()
    mimetypes = {
        ".pdf":  "application/pdf",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
    }
    return mimetypes.get(ext, "application/octet-stream")
