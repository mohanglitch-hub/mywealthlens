"""
FX Rates — Multi-Currency Wealth Assets/Liabilities (Sep 2026)
==================================================================
Answers the last of the NaviPlan feature-parity gaps: MyWealthLens is
India-focused and every INR-native module (CAS-imported holdings,
Insurance, Retirement's PPF/EPF/NPS/SSY schemes) stays exactly that —
none of those can ever be denominated in anything but INR, so currency
support was added ONLY to Wealth Centre's own manually-entered assets
and liabilities (wealth/models.py: WealthAsset, WealthLiability),
where someone might genuinely record a foreign bank account, foreign
property, or foreign brokerage holding.

Design: current_value / outstanding_amount remain the single
authoritative INR figures that every existing calculation (net worth,
category totals, history charts, exports, snapshots) already reads —
nothing downstream of those two fields needed to change. A non-INR
row additionally stores the amount as entered in its own currency
(foreign_value / foreign_outstanding_amount) plus the exchange rate
and date used to derive the INR figure, purely for transparency and
so it can be re-converted later as rates move.

Rate source: Frankfurter (https://frankfurter.dev) — free, no API
key, backed by the European Central Bank's published reference rates,
with both current and historical-by-date lookups. Same category of
choice as AMFI's NAVAll.txt for mutual funds in price_refresh.py: a
free, public, no-signup data source rather than a paid API needing
a key Mohan would have to go generate and manage.
"""
import json
import urllib.request
import urllib.error
from datetime import date as date_cls


FRANKFURTER_BASE_URL = "https://api.frankfurter.dev/v1"
FX_FETCH_TIMEOUT = 10  # seconds

# Deliberately a short, curated list rather than "every ISO 4217 code"
# — these are the currencies an Indian investor might plausibly hold a
# real foreign asset in (US/UK/EU holdings, Gulf property/bank
# accounts, Singapore/Australia/Canada). Easy to extend later; no
# reason to overwhelm the dropdown with currencies nobody here needs.
SUPPORTED_CURRENCIES = {
    "INR": "Indian Rupee (₹)",
    "USD": "US Dollar ($)",
    "EUR": "Euro (€)",
    "GBP": "British Pound (£)",
    "AED": "UAE Dirham (AED)",
    "SGD": "Singapore Dollar (S$)",
    "AUD": "Australian Dollar (A$)",
    "CAD": "Canadian Dollar (C$)",
}


class FxRateError(Exception):
    """Raised when a rate genuinely couldn't be fetched (network, bad currency, etc.)."""
    pass


def fetch_fx_rate(from_currency, to_currency="INR", on_date=None):
    """
    Returns (rate, actual_date) — how many `to_currency` one unit of
    `from_currency` is worth, and the date that rate is actually
    dated (Frankfurter returns the latest available trading day's
    rate for a requested date that falls on a weekend/holiday, so the
    returned date can differ slightly from the one asked for — that's
    normal, not an error). Raises FxRateError on any failure.
    """
    from_currency = (from_currency or "").upper().strip()
    to_currency = (to_currency or "").upper().strip()

    if from_currency == to_currency:
        return 1.0, on_date or date_cls.today()

    path = on_date.isoformat() if on_date else "latest"
    url = f"{FRANKFURTER_BASE_URL}/{path}?from={from_currency}&to={to_currency}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MyWealthLens/1.0"})
        with urllib.request.urlopen(req, timeout=FX_FETCH_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError) as e:
        raise FxRateError(f"Couldn't reach the exchange rate service: {e}")
    except json.JSONDecodeError:
        raise FxRateError("The exchange rate service returned something unexpected.")

    rates = data.get("rates") or {}
    rate = rates.get(to_currency)
    if rate is None:
        raise FxRateError(f"No exchange rate available for {from_currency} → {to_currency}.")

    actual_date = None
    if data.get("date"):
        try:
            actual_date = date_cls.fromisoformat(data["date"])
        except ValueError:
            actual_date = None

    return float(rate), (actual_date or on_date or date_cls.today())


def refresh_fx_for_wealth_rows(db, user_id=None):
    """
    Re-fetches the current exchange rate for every distinct non-INR
    currency in use, and re-derives current_value/outstanding_amount
    for every WealthAsset/WealthLiability holding it, from its own
    stored foreign_value/foreign_outstanding_amount (which never
    changes here — only the INR-equivalent moves with the rate).

    One rate lookup per distinct currency in use (not one per row),
    same "fetch once, apply many" shape as price_refresh.py's AMFI
    handling. A currency whose rate can't be fetched this run leaves
    every row in that currency untouched and is reported separately —
    never guesses or zeroes out a real recorded value.

    Returns a summary dict: {assets_updated, liabilities_updated,
    currencies_failed: [{currency, error}]}
    """
    from wealth.models import WealthAsset, WealthLiability

    summary = {"assets_updated": 0, "liabilities_updated": 0, "currencies_failed": []}

    asset_query = WealthAsset.query.filter(WealthAsset.currency != "INR")
    liability_query = WealthLiability.query.filter(WealthLiability.currency != "INR")
    if user_id is not None:
        asset_query = asset_query.filter_by(user_id=user_id)
        liability_query = liability_query.filter_by(user_id=user_id)

    assets = asset_query.all()
    liabilities = liability_query.all()

    currencies_in_use = {a.currency for a in assets} | {l.currency for l in liabilities}
    rate_cache = {}
    for currency in currencies_in_use:
        try:
            rate, rate_date = fetch_fx_rate(currency, "INR")
            rate_cache[currency] = (rate, rate_date)
        except FxRateError as e:
            summary["currencies_failed"].append({"currency": currency, "error": str(e)})

    for asset in assets:
        cached = rate_cache.get(asset.currency)
        if not cached or asset.foreign_value is None:
            continue
        rate, rate_date = cached
        asset.current_value = round(asset.foreign_value * rate, 2)
        asset.fx_rate = rate
        asset.fx_rate_date = rate_date
        summary["assets_updated"] += 1

    for liability in liabilities:
        cached = rate_cache.get(liability.currency)
        if not cached or liability.foreign_outstanding_amount is None:
            continue
        rate, rate_date = cached
        liability.outstanding_amount = round(liability.foreign_outstanding_amount * rate, 2)
        liability.fx_rate = rate
        liability.fx_rate_date = rate_date
        summary["liabilities_updated"] += 1

    db.session.commit()
    return summary
