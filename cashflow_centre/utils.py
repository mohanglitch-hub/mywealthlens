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

from wealth.timezone_utils import today_ist


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
    """'YYYY-MM' for the current calendar month, IST-aware."""
    return today_ist().strftime("%Y-%m")


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


# ── Date-range presets ────────────────────────────────────────────────────────

def last_n_months_bounds(anchor_date, n):
    """
    (first_day, last_day) covering the current calendar month and the
    (n-1) months before it, e.g. n=3 on 22 Sep 2026 -> (1 Jul 2026, 30 Sep 2026).
    """
    end_year, end_month = anchor_date.year, anchor_date.month
    start_key = adjacent_month_key(end_year, end_month, -(n - 1))
    start_year, start_month = parse_month_key(start_key)
    first_day, _ = month_bounds(start_year, start_month)
    _, last_day = month_bounds(end_year, end_month)
    return first_day, last_day


def fy_bounds(anchor_date):
    """
    (first_day, last_day) of the Indian financial year (1 Apr - 31 Mar)
    containing `anchor_date`.
    """
    if anchor_date.month >= 4:
        start_year = anchor_date.year
    else:
        start_year = anchor_date.year - 1
    first_day = date(start_year, 4, 1)
    last_day = date(start_year + 1, 3, 31)
    return first_day, last_day


def fy_label(anchor_date):
    """'FY 2026-27' style label for the financial year containing `anchor_date`."""
    first_day, _ = fy_bounds(anchor_date)
    return f"FY {first_day.year}-{str(first_day.year + 1)[-2:]}"
