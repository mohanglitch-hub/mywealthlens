"""
Family Centre — Utilities
============================
Kept minimal and self-contained, matching this project's established
per-module utils.py convention (each module keeps its own copy
rather than cross-importing — see insurance_centre/utils.py's own
top-of-file note on this).
"""


def format_inr(value):
    """Format a number as Indian Rupees. Identical to
    insurance_centre.utils.format_inr / wealth.utils.format_inr —
    copied rather than imported, matching this project's per-module
    convention."""
    if value is None:
        return "—"
    if value >= 10_000_000:
        return f"₹{value/10_000_000:.2f} Cr"
    if value >= 100_000:
        return f"₹{value/100_000:.2f} L"
    return f"₹{value:,.0f}"