"""
International Investing Centre — Utils
==========================================
Own copies of small shared helpers (format_date, fy_bounds/fy_label),
matching this project's established per-module convention rather than
cross-importing another module's copies. currency_display.py IS a
genuinely shared root-level module (used the same way by every module
here — see its own docstring) so it's imported directly, not copied.
"""
from datetime import datetime as _dt, date as _date


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
