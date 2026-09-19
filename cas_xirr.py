"""
CAS XIRR — Cash-flow classification + XIRR calculation
==========================================================
Upload CAS rebuild (see migrate_add_mf_transactions.py / app.py's
upload_cams()). Turns casparser's raw per-scheme transaction rows
into the (date, cash_flow) pairs XIRR actually needs, and wraps
pyxirr for the calculation itself.

Why a cash-flow CONVENTION is needed, not just "amount":
A CAS transaction row isn't always money moving into/out of the
investor's pocket. SWITCH_IN/OUT move value between two schemes
inside the same portfolio — no external cash changes hands.
DIVIDEND_REINVEST redeploys a payout as more units — again no cash
leaves or enters the investor's bank account. Taxes/charges are
statement noise, not an investment decision. Treating all of these
as real cash flows would silently corrupt XIRR. The rules below are
deliberately conservative and documented here so a future change to
this logic has one place to look, not four.

  PER-SCHEME XIRR (scheme_xirr):
    outflow (negative): PURCHASE, PURCHASE_SIP, SWITCH_IN, SWITCH_IN_MERGER
    inflow  (positive): REDEMPTION, SWITCH_OUT, SWITCH_OUT_MERGER,
                         DIVIDEND_PAYOUT
    excluded (no cash flow): DIVIDEND_REINVEST, STT_TAX, STAMP_DUTY_TAX,
                         TDS_TAX, SEGREGATION, GIFT_IN, GIFT_OUT, MISC,
                         UNKNOWN, REVERSAL
    Switches ARE real cash flows for a single scheme's own XIRR — money
    left/entered THIS scheme, even though it stayed inside the same CAS.

  PORTFOLIO XIRR (portfolio_xirr):
    Same as above EXCEPT switches are excluded entirely — a switch
    between two of the investor's own schemes nets to zero at the
    portfolio level and including it would double-count the same
    rupee as both an outflow and an inflow on the same or a nearby
    date, distorting the result.

  GIFT_IN / GIFT_OUT are excluded from both. A gift-in is units
  received at no cost to this investor; including it as a cash flow
  (with no matching outflow) produces a meaningless or infinite XIRR.
  It still affects the unit balance (and therefore current value),
  just not the return calculation.

Both cash-flow lists end with the position's CURRENT VALUE as of
today, as the final (positive) cash flow — this is what turns a
list of past transactions into a return: "if you'd put this money in
at these times and got the current value out today, what annualised
rate does that imply."
"""

from datetime import date as _date, datetime as _dt

from pyxirr import xirr as _pyxirr

SCHEME_OUTFLOW_TYPES = {"PURCHASE", "PURCHASE_SIP", "SWITCH_IN", "SWITCH_IN_MERGER"}
SCHEME_INFLOW_TYPES = {"REDEMPTION", "SWITCH_OUT", "SWITCH_OUT_MERGER", "DIVIDEND_PAYOUT"}
PORTFOLIO_OUTFLOW_TYPES = {"PURCHASE", "PURCHASE_SIP"}
PORTFOLIO_INFLOW_TYPES = {"REDEMPTION", "DIVIDEND_PAYOUT"}


def _as_date(d):
    if isinstance(d, _date):
        return d
    if isinstance(d, _dt):
        return d.date()
    try:
        return _dt.strptime(str(d)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _cashflows_from_transactions(transactions, outflow_types, inflow_types):
    """transactions: iterable of objects/dicts with .date/.txn_type (or
    .type)/.amount attributes. Returns [(date, signed_amount), ...],
    skipping any row with a missing date/amount or a type outside both
    sets (taxes, reinvest, gifts, etc. — see module docstring)."""
    flows = []
    for t in transactions:
        d = _as_date(getattr(t, "date", None) if not isinstance(t, dict) else t.get("date"))
        ttype = getattr(t, "txn_type", None) if not isinstance(t, dict) else t.get("txn_type")
        if ttype is None:
            ttype = getattr(t, "type", None) if not isinstance(t, dict) else t.get("type")
        amount = getattr(t, "amount", None) if not isinstance(t, dict) else t.get("amount")
        if d is None or amount is None or ttype is None:
            continue
        # Plain str works directly. A raw Enum member (e.g. casparser's
        # TransactionType, a str-subclass Enum) must use .value, NOT
        # str() — str(TransactionType.PURCHASE) is "TransactionType.
        # PURCHASE", not "PURCHASE", which would silently fail every
        # membership check below. We store plain strings in our own
        # MutualFundTransaction rows, but stay defensive here in case
        # this is ever called on casparser's raw objects directly.
        ttype = ttype.value if hasattr(ttype, "value") else str(ttype)
        if ttype in outflow_types:
            flows.append((d, -abs(float(amount))))
        elif ttype in inflow_types:
            flows.append((d, abs(float(amount))))
        # else: excluded by convention — see module docstring
    return flows


def scheme_xirr(transactions, current_value, asof=None):
    """Per-scheme XIRR. Returns None if there isn't enough data (fewer
    than 2 distinct-sign cash flows — pyxirr can't solve for a rate
    from a single flow, e.g. a fund held less than one transaction)."""
    flows = _cashflows_from_transactions(transactions, SCHEME_OUTFLOW_TYPES, SCHEME_INFLOW_TYPES)
    return _solve_xirr(flows, current_value, asof)


def portfolio_xirr(transactions, current_value, asof=None):
    """Aggregate XIRR across every scheme's transactions. Switches are
    excluded here (see module docstring) even though scheme_xirr()
    includes them for a single scheme."""
    flows = _cashflows_from_transactions(transactions, PORTFOLIO_OUTFLOW_TYPES, PORTFOLIO_INFLOW_TYPES)
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
        # All cash flows the same sign (e.g. only redemptions ever
        # recorded, or a holding with zero current value and no
        # inflow) — no rate can explain that, pyxirr would raise.
        return None
    dates = [d for d, _ in all_flows]
    amounts = [amt for _, amt in all_flows]
    try:
        result = _pyxirr(dates, amounts)
    except Exception:
        return None
    if result is None:
        return None
    return round(result * 100, 2)  # as a percentage, matching the rest of the app's display convention
