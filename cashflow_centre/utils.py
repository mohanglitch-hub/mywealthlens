"""
Cashflow Centre — Utilities
==============================
Currency/date formatting mirrors the other modules' utils.py to keep
display conventions consistent app-wide. Kept self-contained here
rather than cross-imported, matching this project's existing
per-module utils.py convention.
"""

import calendar
from datetime import date, datetime as _dt


# ── Display Helpers ───────────────────────────────────────────────────────────

def format_inr(value):
    """
    Format a number as Indian Rupees (Cr/L notation). Handles negative
    values properly — net cash flow (income - expense) can legitimately
    be negative, and the sign must never be hidden or dropped.
    """
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 10_000_000:
        return f"{sign}₹{value/10_000_000:.2f} Cr"
    if value >= 100_000:
        return f"{sign}₹{value/100_000:.2f} L"
    return f"{sign}₹{value:,.0f}"


def format_date(d, fmt="%d %b %Y"):
    """Format a date or datetime object for display. Returns '—' if None."""
    if not d:
        return "—"
    try:
        if isinstance(d, _dt):
            d = d.date()
        return d.strftime(fmt)
    except (ValueError, AttributeError):
        return "—"


# ── Month helpers ─────────────────────────────────────────────────────────────

def current_month_key():
    """'YYYY-MM' for the current calendar month."""
    return date.today().strftime("%Y-%m")


def parse_month_key(month_key):
    """
    'YYYY-MM' -> (year, month) ints, or None if invalid/missing.
    Used to parse the ?month= query param safely — falls back to the
    current month at the call site rather than raising.
    """
    if not month_key:
        return None
    try:
        year, month = month_key.split("-")
        year, month = int(year), int(month)
        if 1 <= month <= 12:
            return year, month
    except (ValueError, AttributeError):
        pass
    return None


def month_bounds(year, month):
    """(first_day, last_day) date objects for the given year/month."""
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return first_day, last_day


def month_label(year, month):
    """'September 2026' style label for a year/month pair."""
    return date(year, month, 1).strftime("%B %Y")


def adjacent_month_key(year, month, delta):
    """Return the 'YYYY-MM' key for `delta` months before/after (year, month)."""
    total = year * 12 + (month - 1) + delta
    new_year, new_month = divmod(total, 12)
    return f"{new_year:04d}-{new_month + 1:02d}"
