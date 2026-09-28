"""
International XIRR
=====================
Same shape as stock_xirr.py, for International Investing Centre's own
InternationalTransaction rows (BUY/SELL/DIVIDEND, native-currency
amounts):

  outflow (negative): BUY
  inflow  (positive): SELL, DIVIDEND

Unlike the mutual fund CAS import (cas_xirr.py), this module has no
DIVIDEND_REINVEST transaction type — every dividend recorded here is a
genuine cash payout the investor actually received, so it's always a
real inflow, same treatment as a SELL.

Both cash-flow lists end with the position's CURRENT VALUE as of today
as the final (positive) cash flow, exactly like cas_xirr.py/
stock_xirr.py — "if you'd bought/sold/received dividends at these
times and got the current value out today, what annualised rate does
that imply."

Deliberately currency-agnostic: this module doesn't care whether the
amounts passed in are native-currency or USD, as long as every amount
for a given call is in the SAME currency (mixing currencies within one
XIRR call would produce a meaningless rate). services.py always calls
this with either all-native or all-USD figures, never a mix.
"""
from datetime import date as _date, datetime as _dt

from pyxirr import xirr as _pyxirr


def _as_date(d):
    if isinstance(d, _date):
        return d
    if isinstance(d, _dt):
        return d.date()
    try:
        return _dt.strptime(str(d)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _cashflows_from_transactions(transactions):
    """transactions: iterable of objects/dicts with .date/.txn_type/
    .amount_native (or .amount) attributes. Returns [(date, signed_amount), ...],
    skipping any row with a missing date/amount/type or an unrecognised type."""
    flows = []
    for t in transactions:
        is_dict = isinstance(t, dict)
        d = _as_date(t.get("date") if is_dict else getattr(t, "date", None))
        ttype = t.get("txn_type") if is_dict else getattr(t, "txn_type", None)
        amount = t.get("amount_native") if is_dict else getattr(t, "amount_native", None)
        if amount is None:
            amount = t.get("amount") if is_dict else getattr(t, "amount", None)
        if d is None or amount is None or ttype is None:
            continue
        ttype = str(ttype).upper()
        if ttype == "BUY":
            flows.append((d, -abs(float(amount))))
        elif ttype in ("SELL", "DIVIDEND"):
            flows.append((d, abs(float(amount))))
        # else: unrecognised type — skip rather than guess the sign
    return flows


def holding_xirr(transactions, current_value, asof=None):
    """Per-holding XIRR from a list of BUY/SELL/DIVIDEND transactions
    plus the position's current value (same currency as the
    transactions). Returns None if there isn't enough data (fewer than
    2 distinct-sign cash flows)."""
    flows = _cashflows_from_transactions(transactions)
    return _solve_xirr(flows, current_value, asof)


def portfolio_xirr(transactions, current_value, asof=None):
    """Aggregate XIRR across every holding's transactions (all must
    already be in the same currency, typically USD for a cross-holding
    portfolio total — see module docstring). Same classification as
    holding_xirr(); kept as a separate function only to mirror cas_xirr.py/
    stock_xirr.py's scheme/portfolio split and make call sites read
    consistently across the app."""
    flows = _cashflows_from_transactions(transactions)
    return _solve_xirr(flows, current_value, asof)


def _solve_xirr(flows, current_value, asof=None):
    if not flows:
        return None
    asof = asof or _date.today()
    all_flows = list(flows)
    if current_value and current_value > 0:
        all_flows.append((asof, float(current_value)))
    if len(all_flows) < 2:
        return None
    signs = {1 if amt > 0 else -1 for _, amt in all_flows}
    if len(signs) < 2:
        return None
    dates = [d for d, _ in all_flows]
    amounts = [amt for _, amt in all_flows]
    try:
        result = _pyxirr(dates, amounts)
    except Exception:
        return None
    if result is None:
        return None
    return round(result * 100, 2)
