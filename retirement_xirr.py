"""
Retirement Scheme XIRR (Batch 4, Sep 2026)
==========================================
Per-scheme XIRR for Retirement Centre schemes, matching cas_xirr.py's
overall approach for CAS-imported mutual funds/stocks, but working
from RetirementContribution rows instead of casparser transactions.
Kept as its own root-level module rather than cross-imported from
cas_xirr.py or coupled to retirement_centre's package internals,
matching this app's existing convention of each area keeping its own
copy of shared logic rather than reaching across module boundaries --
and it keeps this module duck-typed and testable without a Flask/DB
context, the same way cas_xirr.py is.

Cash-flow convention:
  outflow (negative): every DEPOSIT contribution -- money the user put
                       in, on the date they put it in.
  excluded:            "Interest Credited" rows -- that's growth
                       already reflected in the scheme's
                       current_balance, not a cash flow the user made.
                       Counting it as an inflow too would double-count
                       the same growth (once as excluded principal,
                       once again inside current_balance itself).
  terminal inflow (positive): the scheme's current_balance, as of
                       today (or an explicit asof date) -- "if you'd
                       put this money in at these times, and had this
                       much sitting there today, what annualised rate
                       does that imply."

Returns None (never raises) whenever there isn't enough data for a
real answer: no deposits recorded yet, every deposit on the exact same
date as the valuation with no time elapsed, or pyxirr itself can't
converge on a rate.
"""
from datetime import date as _date, datetime as _dt

from pyxirr import xirr as _pyxirr

DEPOSIT_ENTRY_TYPE = "Deposit"  # matches retirement_centre.models.ContributionEntryType.DEPOSIT's value


def _as_date(d):
    if isinstance(d, _date):
        return d
    if isinstance(d, _dt):
        return d.date()
    try:
        return _dt.strptime(str(d)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def compute_retirement_xirr(contributions, current_balance, asof=None):
    """
    contributions: iterable of RetirementContribution rows (or any
    duck-typed object/dict with .contribution_date/.amount/.entry_type).
    current_balance: the scheme's current balance, used as the final
    positive cash flow.

    Returns XIRR as a percentage (e.g. 8.42), or None if there isn't
    enough data to compute one (fewer than 2 distinct-sign cash flows,
    or pyxirr can't solve for a rate).
    """
    flows = []
    for c in contributions:
        entry_type = getattr(c, "entry_type", None) if not isinstance(c, dict) else c.get("entry_type")
        if entry_type != DEPOSIT_ENTRY_TYPE:
            continue  # interest credits are growth, not a cash flow -- see module docstring
        d = _as_date(getattr(c, "contribution_date", None) if not isinstance(c, dict) else c.get("contribution_date"))
        amount = getattr(c, "amount", None) if not isinstance(c, dict) else c.get("amount")
        if d is None or amount is None:
            continue
        flows.append((d, -abs(float(amount))))

    if not flows:
        return None

    asof = asof or _date.today()
    if current_balance and current_balance > 0:
        flows.append((asof, float(current_balance)))
    if len(flows) < 2:
        return None

    signs = {1 if amt > 0 else -1 for _, amt in flows}
    if len(signs) < 2:
        # Every flow is the same sign (e.g. deposits recorded but a
        # zero/blank current_balance) -- no rate can explain that,
        # pyxirr would raise on this rather than return a number.
        return None

    dates = [d for d, _ in flows]
    amounts = [amt for _, amt in flows]
    try:
        result = _pyxirr(dates, amounts)
    except Exception:
        return None
    if result is None:
        return None
    return round(result * 100, 2)  # as a percentage, matching the rest of the app's display convention
