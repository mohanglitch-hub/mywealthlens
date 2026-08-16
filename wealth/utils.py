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
    Format a number as Indian Rupees (Cr/L notation). Handles
    negative values properly (Section 27/19 of Phase D spec — Net
    Worth can legitimately be negative, and the sign must never be
    hidden or dropped). e.g. -1000000 -> '-₹10.00 L', not '₹-1,000,000'.
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
