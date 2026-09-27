"""
Wealth — Utilities
=====================
Shared helpers for the Wealth module. Currency/date formatting
mirrors the pattern already used in insurance_centre/utils.py and
retirement_centre/utils.py — kept self-contained here rather than
cross-imported, matching this project's established per-module
utils.py convention.
"""

from datetime import date, datetime as _dt


def format_inr(value):
    """
    Format a stored INR value for display. Despite the name (kept for
    backward compatibility — every template in this module calls
    format_inr(...)), this now converts to the user's chosen global
    display currency (Sep 2026 — My Account > Appearance > Currency)
    before formatting; INR-Cr/L notation is exactly the old behavior
    when the display currency is left as INR. See currency_display.py
    for the actual conversion/formatting logic and why this is a pure
    display-layer change — stored values are never touched.
    """
    from currency_display import format_money
    return format_money(value)


def format_date(d, fmt="%d %b %Y"):
    """Format a date/datetime for display. Returns '—' if None."""
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


# ── Document Storage (Phase G) ────────────────────────────────────────────────

import os
import uuid
from flask import current_app


def get_document_upload_path(user_id):
    """
    Local directory for a user's Wealth documents. Creates it if
    missing. Path: instance/documents/wealth/<user_id>/
    """
    base = os.path.join(
        current_app.instance_path,
        "documents", "wealth", str(user_id)
    )
    os.makedirs(base, exist_ok=True)
    return base


def generate_stored_filename(original_filename):
    """UUID-based stored filename — prevents collisions and never
    exposes the original filename as a server path (Section 15)."""
    ext = os.path.splitext(original_filename)[1].lower()
    return f"{uuid.uuid4()}{ext}"


def save_document_file(file, user_id):
    """
    Save an uploaded file to local storage. Returns
    (stored_name, file_path, file_size) on success. Raises OSError
    on failure.
    """
    upload_dir  = get_document_upload_path(user_id)
    stored_name = generate_stored_filename(file.filename)
    file_path   = os.path.join(upload_dir, stored_name)
    file.save(file_path)
    file_size = os.path.getsize(file_path)
    return stored_name, file_path, file_size


def delete_document_file(file_path):
    """Delete a document file from local storage. Silent/graceful if
    the file is already missing."""
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            return True
    except OSError:
        pass
    return False


def secure_file_path(file_path, user_id):
    """
    Path-traversal guard: confirms the resolved, absolute path really
    is inside this user's own Wealth documents directory before any
    read/serve/delete happens.
    """
    expected_base = os.path.join(
        current_app.instance_path, "documents", "wealth", str(user_id)
    )
    real_path = os.path.realpath(file_path)
    real_base = os.path.realpath(expected_base)
    return real_path.startswith(real_base)


PREVIEWABLE_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}


def is_previewable(filename):
    """Whether a file can be shown inline in the browser."""
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
