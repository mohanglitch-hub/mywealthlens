"""
International Investing Centre — Utils
==========================================
Own copies of small shared helpers (format_date, fy_bounds/fy_label),
matching this project's established per-module convention rather than
cross-importing another module's copies. currency_display.py IS a
genuinely shared root-level module (used the same way by every module
here — see its own docstring) so it's imported directly, not copied.
"""
import os
import uuid
import mimetypes
from datetime import datetime as _dt, date as _date
from flask import current_app


def format_date(d, fmt="%d %b %Y"):
    """Format a date/datetime for display. Returns '—' if None."""
    if not d:
        return "—"
    try:
        if isinstance(d, _dt):
            return d.strftime(fmt)
        return _dt.strptime(str(d)[:10], "%Y-%m-%d").strftime(fmt)
    except Exception:
        return str(d)


def fy_bounds(anchor_date):
    """(first_day, last_day) of the Indian financial year (1 Apr - 31 Mar)
    containing `anchor_date`. Used for LRS's per-financial-year cap."""
    if anchor_date.month >= 4:
        start_year = anchor_date.year
    else:
        start_year = anchor_date.year - 1
    first_day = _date(start_year, 4, 1)
    last_day = _date(start_year + 1, 3, 31)
    return first_day, last_day


def fy_label(anchor_date):
    """'FY 2026-27' style label for the financial year containing `anchor_date`."""
    first_day, _last = fy_bounds(anchor_date)
    return f"FY {first_day.year}-{str(first_day.year + 1)[-2:]}"


def calendar_year_bounds(year):
    """(first_day, last_day) of a plain CALENDAR year — Schedule FA's
    own reporting period (1 Jan - 31 Dec), deliberately NOT the Indian
    financial year fy_bounds() above computes. Two different "years"
    matter in this module for two different reasons — keeping them as
    separate, clearly-named functions avoids ever mixing them up."""
    return _date(year, 1, 1), _date(year, 12, 31)


COUNTRIES = [
    "United States", "United Kingdom", "Singapore", "United Arab Emirates",
    "Australia", "Canada", "Germany", "France", "Netherlands", "Ireland",
    "Switzerland", "Japan", "Hong Kong", "Other",
]


# ── Document Storage (Batch 9.3, Sep 2026) ───────────────────────────
# Mirrors insurance_centre/retirement_centre's utils.py exactly —
# own copy per this project's established per-module convention.

PREVIEWABLE_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def get_document_upload_path(holding_id):
    """Local directory for a holding's documents. Creates it if needed.
    Path: instance/documents/international/<holding_id>/"""
    base = os.path.join(
        current_app.instance_path,
        "documents", "international", str(holding_id)
    )
    os.makedirs(base, exist_ok=True)
    return base


def generate_stored_filename(original_filename):
    """UUID-based stored filename (prevents collisions), extension preserved."""
    ext = os.path.splitext(original_filename)[1].lower()
    return f"{uuid.uuid4()}{ext}"


def save_document_file(file, holding_id):
    """Save an uploaded file to local storage.
    Returns (stored_name, file_path, file_size) on success.
    Raises OSError on failure."""
    upload_dir  = get_document_upload_path(holding_id)
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


def is_previewable(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ext in PREVIEWABLE_EXTENSIONS


def get_preview_mimetype(filename):
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def secure_file_path(file_path, holding_id):
    """Validate file_path is within the expected holding documents
    directory. Prevents directory traversal attacks."""
    expected_base = os.path.join(
        current_app.instance_path, "documents", "international", str(holding_id)
    )
    real_path = os.path.realpath(file_path)
    real_base = os.path.realpath(expected_base)
    return real_path.startswith(real_base)


def fetch_ticker_price(ticker):
    """Live per-unit price for a foreign ticker via yfinance, used AS
    GIVEN (no NSE '.NS' suffix mangling — that's app.py's
    fetch_live_price_by_isin()'s job for Indian stocks, a different
    market). Own copy per this project's convention rather than
    cross-importing app.py's version, which also assumes an Indian
    exchange fallback that doesn't apply here. Returns None on any
    failure (network unreachable, bad ticker, delisted) — callers must
    fall back to the last known price, never zero one out."""
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        price = float(t.fast_info.last_price)
        if price and price > 0:
            return round(price, 2)
    except Exception:
        pass
    return None
