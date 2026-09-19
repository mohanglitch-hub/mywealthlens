"""
Stock XIRR — Tradebook rebuild
=================================
Same idea as cas_xirr.py, but for stocks: a broker tradebook has just
two transaction types that matter (BUY, SELL — no switches, no
reinvestment, no scheme-level tax rows), so the cash-flow rule is much
simpler than the mutual fund one:

  outflow (negative): BUY
  inflow  (positive): SELL

Both cash-flow lists end with the position's CURRENT VALUE as of today
as the final (positive) cash flow, exactly like cas_xirr.py — "if
you'd bought/sold at these times and got the current value out today,
what annualised rate does that imply."
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
    """transactions: iterable of objects/dicts with .date/.txn_type/.amount
    (BUY or SELL). Returns [(date, signed_amount), ...], skipping any row
    with a missing date/amount/type or an unrecognised type."""
    flows = []
    for t in transactions:
        d = _as_date(getattr(t, "date", None) if not isinstance(t, dict) else t.get("date"))
        ttype = getattr(t, "txn_type", None) if not isinstance(t, dict) else t.get("txn_type")
        amount = getattr(t, "amount", None) if not isinstance(t, dict) else t.get("amount")
        if d is None or amount is None or ttype is None:
            continue
        ttype = str(ttype).upper()
        if ttype == "BUY":
            flows.append((d, -abs(float(amount))))
        elif ttype == "SELL":
            flows.append((d, abs(float(amount))))
        # else: unrecognised type — skip rather than guess the sign
    return flows


def stock_xirr(transactions, current_value, asof=None):
    """Per-stock XIRR from a list of BUY/SELL transactions plus the
    position's current value. Returns None if there isn't enough data
    (fewer than 2 distinct-sign cash flows)."""
    flows = _cashflows_from_transactions(transactions)
    return _solve_xirr(flows, current_value, asof)


def portfolio_stock_xirr(transactions, current_value, asof=None):
    """Aggregate XIRR across every stock's transactions. No switches/
    reinvestment concept for equities, so this is the same
    classification as stock_xirr() — kept as a separate function only
    to mirror cas_xirr.py's scheme_xirr/portfolio_xirr split and make
    call sites read consistently across both modules."""
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
