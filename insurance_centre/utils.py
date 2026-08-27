"""
Insurance Centre — Utilities
==============================
Shared helpers used across services and routes.
"""

import os
import uuid
import mimetypes
from datetime import date
from flask import current_app
from .models import PremiumFrequency


# ── Document Storage ──────────────────────────────────────────────────────────

def get_document_upload_path(policy_id):
    """
    Returns the local directory path for a policy's documents.
    Creates directory if it doesn't exist.
    Path: instance/documents/insurance/<policy_id>/
    """
    base = os.path.join(
        current_app.instance_path,
        "documents", "insurance", str(policy_id)
    )
    os.makedirs(base, exist_ok=True)
    return base


def generate_stored_filename(original_filename):
    """
    Generate a unique filename for local storage.
    Preserves extension. Uses UUID to prevent collisions.
    e.g. "policy.pdf" → "a1b2c3d4-e5f6-....pdf"
    """
    ext = os.path.splitext(original_filename)[1].lower()
    return f"{uuid.uuid4()}{ext}"


def save_document_file(file, policy_id):
    """
    Save an uploaded file to local storage.
    Returns (stored_name, file_path, file_size) on success.
    Raises OSError on failure.
    """
    upload_dir   = get_document_upload_path(policy_id)
    stored_name  = generate_stored_filename(file.filename)
    file_path    = os.path.join(upload_dir, stored_name)
    file.save(file_path)
    file_size = os.path.getsize(file_path)
    return stored_name, file_path, file_size


def delete_document_file(file_path):
    """
    Delete a document file from local storage.
    Silent if file doesn't exist.
    """
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            return True
    except OSError:
        pass
    return False


# ── Date Helpers ──────────────────────────────────────────────────────────────

def days_until(target_date):
    """Returns days until a date. Negative if past."""
    if not target_date:
        return None
    return (target_date - date.today()).days


def format_date(d, fmt="%d %b %Y"):
    """Format a date or datetime object for display. Returns '—' if None."""
    if not d:
        return "—"
    try:
        # Handle both date and datetime objects
        from datetime import datetime as _dt
        if isinstance(d, _dt):
            return d.strftime(fmt)
        if isinstance(d, date):
            return d.strftime(fmt)
        # Try parsing string
        return _dt.strptime(str(d)[:10], "%Y-%m-%d").strftime(fmt)
    except Exception:
        return str(d)


def annual_premium(amount, frequency):
    """Convert any premium frequency to annual equivalent."""
    multipliers = {
        PremiumFrequency.MONTHLY:     12,
        PremiumFrequency.QUARTERLY:   4,
        PremiumFrequency.HALF_YEARLY: 2,
        PremiumFrequency.YEARLY:      1,
        PremiumFrequency.ONE_TIME:    0,
    }
    return (amount or 0) * multipliers.get(frequency, 1)


# ── Display Helpers ───────────────────────────────────────────────────────────

def format_inr(value):
    """Format a number as Indian Rupees."""
    if value is None:
        return "—"
    if value >= 10_000_000:
        return f"₹{value/10_000_000:.2f} Cr"
    if value >= 100_000:
        return f"₹{value/100_000:.2f} L"
    return f"₹{value:,.0f}"


def renewal_badge(policy):
    """Return (label, colour_key) for renewal status display."""
    status = policy.renewal_status
    badges = {
        "overdue":  ("Overdue",   "down"),
        "due_soon": ("Due Soon",  "warning"),
        "upcoming": ("Upcoming",  "accent"),
        "active":   ("Active",    "up"),
        "unknown":  ("No Date",   "muted"),
    }
    return badges.get(status, ("—", "muted"))


# ── Category URL Slugs ────────────────────────────────────────────────────────

CATEGORY_SLUGS = {
    "life":     "Life Insurance",
    "health":   "Health Insurance",
    "motor":    "Motor Insurance",
    "property": "Property Insurance",
    "general":  "General Insurance",
}

SLUG_FROM_CATEGORY = {v: k for k, v in CATEGORY_SLUGS.items()}


def category_to_slug(category):
    """Convert category name to URL slug. e.g. 'Life Insurance' → 'life'"""
    return SLUG_FROM_CATEGORY.get(category, category.lower().replace(" ", "-"))


def slug_to_category(slug):
    """Convert URL slug to category name. e.g. 'life' → 'Life Insurance'"""
    return CATEGORY_SLUGS.get(slug, slug)


# ── Document Preview ──────────────────────────────────────────────────────────

PREVIEWABLE_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}

def is_previewable(filename):
    """Check if a file can be previewed in browser."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in PREVIEWABLE_EXTENSIONS


def get_preview_mimetype(filename):
    """Return MIME type for inline preview."""
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def secure_file_path(file_path, policy_id):
    """
    Validate that file_path is within the expected policy documents directory.
    Prevents directory traversal attacks.
    """
    expected_base = os.path.join(
        current_app.instance_path, "documents", "insurance", str(policy_id)
    )
    # Resolve both paths to absolute
    real_path     = os.path.realpath(file_path)
    real_base     = os.path.realpath(expected_base)
    return real_path.startswith(real_base)