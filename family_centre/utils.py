"""
Family Centre — Utilities
============================
Kept minimal and self-contained, matching this project's established
per-module utils.py convention (each module keeps its own copy
rather than cross-importing — see insurance_centre/utils.py's own
top-of-file note on this).
"""


def format_inr(value):
    """Format a stored INR value for display, converted to the user's
    chosen global display currency (Sep 2026) — see currency_display.py.
    Name kept for backward compatibility with every template call site."""
    from currency_display import format_money
    return format_money(value)