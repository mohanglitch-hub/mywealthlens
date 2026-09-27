"""
Currency Display — Global Display Currency (Sep 2026)
==========================================================
Answers the "Currency" card under My Account > Appearance, which
previously said "Multi-currency support coming in a future phase."

This is a DISPLAY-layer feature only. Every value in the database
stays in INR exactly as before — CAS-imported holdings, Insurance,
Retirement, and Wealth Centre's own `current_value`/`outstanding_amount`
fields are all still authoritative INR figures (see wealth/models.py's
own, separate per-asset currency support, which converts a foreign
holding TO INR at save time — that's a different, already-shipped
feature and this one doesn't change it). What this module does is
convert an INR figure to the user's chosen display currency at the
moment it's rendered, for display purposes only. Change your display
currency and every number on screen updates; nothing in the database
is touched.

Rate source: the same Frankfurter API used by fx_rates.py, but never
called more than once a day per currency — every lookup goes through
FxRateCache (models.py), refreshed lazily the first time that
currency is needed on a given day, then reused by every user and
every page view for the rest of that day. A request-local cache
(flask.g) avoids even that DB round trip more than once per request.

If a live rate can't be fetched AND there's no cached rate to fall
back on (this session's sandbox can't reach Frankfurter at all, so
this path is exercised constantly here — same situation as AMFI and
the per-asset FX work), every amount falls back to showing INR rather
than showing a wrong or stale number silently. See get_display_context().
"""
from datetime import date, datetime

from flask import g
from flask_login import current_user

import fx_rates


def get_display_currency():
    """The current user's chosen display currency, or INR if not
    logged in, not set, or something about current_user can't be
    read (defensive — this must never be the thing that breaks a
    page render)."""
    try:
        if current_user and current_user.is_authenticated:
            return (current_user.display_currency or "INR").upper().strip() or "INR"
    except Exception:
        pass
    return "INR"


def _get_cached_rate(currency):
    """Returns an INR-per-unit-of-currency rate using FxRateCache,
    refreshing it if today's rate hasn't been fetched yet. Returns
    None only if there's truly no rate available at all (never
    fetched, and today's live fetch also failed) — callers must
    handle that by falling back to INR, never by guessing a rate."""
    from models import db, FxRateCache

    cache = FxRateCache.query.get(currency)
    today = date.today()
    if cache and cache.fetched_at and cache.fetched_at.date() == today:
        return cache.rate

    try:
        rate, rate_date = fx_rates.fetch_fx_rate(currency, "INR")
    except fx_rates.FxRateError:
        # Couldn't refresh today — fall back to yesterday's (or older)
        # cached rate rather than nothing, if one exists. Still better
        # than silently showing INR when the user picked something else.
        return cache.rate if cache else None

    if cache:
        cache.rate = rate
        cache.rate_date = rate_date
        cache.fetched_at = datetime.utcnow()
    else:
        cache = FxRateCache(currency=currency, rate=rate, rate_date=rate_date,
                             fetched_at=datetime.utcnow())
        db.session.add(cache)
    db.session.commit()
    return rate


def get_display_context():
    """
    Returns (currency, symbol, rate, ok):
      currency — the ISO code actually being used for display
      symbol   — its prefix/symbol (fx_rates.CURRENCY_SYMBOLS)
      rate     — INR per 1 unit of `currency`
      ok       — False only when the user picked a non-INR currency
                 but no rate (live or cached) could be found, so this
                 call is silently showing INR instead this one time.
                 Callers that show a currency label to the user should
                 check this and say so rather than pretend it worked.

    Memoized per-request (flask.g) — every format_money() call on a
    page only pays for one cache lookup, not one per amount shown.
    """
    if hasattr(g, "_display_currency_ctx"):
        return g._display_currency_ctx

    currency = get_display_currency()
    if currency == "INR":
        result = ("INR", fx_rates.CURRENCY_SYMBOLS["INR"], 1.0, True)
    else:
        rate = _get_cached_rate(currency)
        if rate is None:
            result = ("INR", fx_rates.CURRENCY_SYMBOLS["INR"], 1.0, False)
        else:
            symbol = fx_rates.CURRENCY_SYMBOLS.get(currency, currency + " ")
            result = (currency, symbol, rate, True)

    g._display_currency_ctx = result
    return result


def to_display(value_inr):
    """Converts a stored INR value into the user's display currency.
    Returns None unchanged (callers use that to show '—')."""
    if value_inr is None:
        return None
    _, _, rate, _ = get_display_context()
    return value_inr / rate if rate else value_inr


def format_money(value_inr):
    """
    The general-purpose formatter — drop-in replacement for every
    module's old format_inr(value), same signature, same Cr/L-style
    abbreviation behavior for large INR-scale numbers, just currency-
    aware now. Non-INR currencies don't have a Cr/L convention, so
    they abbreviate with the more familiar K/M/B instead once the
    converted number is large enough to need it.
    """
    if value_inr is None:
        return "—"
    currency, symbol, rate, _ok = get_display_context()
    converted = value_inr / rate if rate else value_inr
    sign = "-" if converted < 0 else ""
    v = abs(converted)

    if currency == "INR":
        if v >= 10_000_000:
            return f"{sign}{symbol}{v/10_000_000:.2f} Cr"
        if v >= 100_000:
            return f"{sign}{symbol}{v/100_000:.2f} L"
        return f"{sign}{symbol}{v:,.0f}"

    if v >= 1_000_000_000:
        return f"{sign}{symbol}{v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"{sign}{symbol}{v/1_000_000:.2f}M"
    return f"{sign}{symbol}{v:,.2f}"


def format_money_precise(value_inr, decimals=2):
    """
    For small per-unit figures that should never be abbreviated — a
    mutual fund NAV, a share price, an interest rate's rupee value.
    Converts the same way as format_money(), just always shows the
    full number with `decimals` places instead of switching to
    Cr/L/K/M/B notation.
    """
    if value_inr is None:
        return "—"
    _, symbol, rate, _ok = get_display_context()
    converted = value_inr / rate if rate else value_inr
    sign = "-" if converted < 0 else ""
    v = abs(converted)
    return f"{sign}{symbol}{v:,.{decimals}f}"


def format_money_pdf_safe(value_inr):
    """
    Same conversion/abbreviation as format_money(), but for PDF exports
    built with ReportLab's base Helvetica font, which can't render the
    ₹ glyph (not in WinAnsiEncoding) — insurance_centre's PDF export
    already worked around this for INR by spelling it "Rs." instead of
    ₹; this extends that same safe convention to every currency here
    (all of fx_rates.SUPPORTED_CURRENCIES use plain ASCII or WinAnsi-safe
    symbols otherwise, so only the INR case needs the substitution).
    """
    if value_inr is None:
        return "—"
    currency, _symbol, rate, _ok = get_display_context()
    converted = value_inr / rate if rate else value_inr
    sign = "-" if converted < 0 else ""
    v = abs(converted)
    prefix = "Rs." if currency == "INR" else fx_rates.CURRENCY_SYMBOLS.get(currency, currency + " ")

    if currency == "INR":
        if v >= 10_000_000:
            return f"{sign}{prefix}{v/10_000_000:.2f} Cr"
        if v >= 100_000:
            return f"{sign}{prefix}{v/100_000:.2f} L"
        return f"{sign}{prefix}{v:,.0f}"

    if v >= 1_000_000_000:
        return f"{sign}{prefix}{v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"{sign}{prefix}{v/1_000_000:.2f}M"
    return f"{sign}{prefix}{v:,.2f}"


def display_symbol():
    """Just the current display currency's symbol — for templates
    and JS chart configs that need to build their own label."""
    _, symbol, _, _ = get_display_context()
    return symbol


def display_currency_code():
    """Just the current display currency's ISO code."""
    currency, _, _, _ = get_display_context()
    return currency
