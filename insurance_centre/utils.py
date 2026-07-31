"""
Insurance Centre — Utilities
==============================
Shared helpers used across services and routes.
"""

import os
import uuid
from datetime import date, timedelta
from flask import current_app


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
    """Format a date object for display. Returns '—' if None."""
    if not d:
        return "—"
    return d.strftime(fmt)


def annual_premium(amount, frequency):
    """Convert any premium frequency to annual equivalent."""
    from .models import PremiumFrequency
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
