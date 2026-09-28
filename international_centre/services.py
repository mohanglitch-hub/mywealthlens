"""
International Investing Centre — Services
=============================================
Routes never touch models directly — matching every other module's
convention. Holds:
  - Holding CRUD + archive/restore/delete lifecycle
  - Live price + native->USD conversion refresh (network-touching)
  - Transaction CRUD, with cost-basis/XIRR recompute after every change
  - Remittance (LRS) CRUD + cumulative-limit status
  - Schedule FA calendar-year summary
  - Daily value-snapshot job (peak-value tracking for Schedule FA)

XIRR is always computed from NATIVE-currency transactions against the
holding's own NATIVE-currency current value (never usd_value) — mixing
currencies within one XIRR call produces a meaningless rate (see
international_xirr.py's module docstring). Cross-holding portfolio
XIRR (portfolio_usd_xirr()) is the one place amounts get converted to
USD, and it's an APPROXIMATION: each transaction's native amount is
converted using the holding's current fx_rate_used applied
retroactively, not the true historical rate on that transaction's own
date (which would mean one FX lookup per transaction — not worth the
network cost for a single headline number). Good enough for "roughly
how has my international portfolio done", not for anything precision-
sensitive.
"""
from datetime import datetime, timedelta

from international_centre.models import (
    InternationalHolding, InternationalTransaction, RemittanceRecord,
    InternationalValueSnapshot, InternationalHoldingNominee,
    InternationalAssetType, InternationalTxnType, LRS_ANNUAL_LIMIT_USD,
)
from international_centre.utils import fy_bounds, fy_label, calendar_year_bounds, fetch_ticker_price
from international_xirr import holding_xirr, portfolio_xirr as _portfolio_xirr_calc
from fx_rates import fetch_fx_rate, FxRateError
from wealth.timezone_utils import today_ist


def _db():
    from models import db
    return db


# ── Holdings ────────────────────────────────────────────────────────

def get_holdings(user_id, archived=False):
    return (InternationalHolding.query
            .filter_by(user_id=user_id, archived=archived)
            .order_by(InternationalHolding.name)
            .all())


def _convert_to_usd(holding):
    """Refreshes usd_value/fx_rate_used/fx_rate_date from
    current_value_native. Leaves the previous usd_value untouched if
    the rate can't be fetched right now (network down) — never zeroes
    out or guesses a value that was already recorded."""
    try:
        rate, rate_date = fetch_fx_rate(holding.native_currency, "USD")
        holding.usd_value = round((holding.current_value_native or 0.0) * rate, 2)
        holding.fx_rate_used = rate
        holding.fx_rate_date = rate_date
    except FxRateError:
        pass


def recompute_holding_financials(holding):
    """Pure (no network) recompute of invested_native and xirr from the
    holding's own transactions + current_value_native. Called after
    every transaction add/edit/delete, and again (redundantly but
    harmlessly) after a price/FX refresh. If there are no transactions
    yet, invested_native is left as whatever create_holding() seeded it
    with (quantity*avg_cost for a ticker-based holding, or
    current_value_native itself for a manually-valued one)."""
    txns = holding.transactions
    buys = [t for t in txns if t.txn_type == InternationalTxnType.BUY]
    sells = [t for t in txns if t.txn_type == InternationalTxnType.SELL]
    if buys or sells:
        buys_amt = sum(t.amount_native for t in buys)
        sells_amt = sum(t.amount_native for t in sells)
        holding.invested_native = round(buys_amt - sells_amt, 2)
    holding.xirr = holding_xirr(txns, holding.current_value_native)


def _replace_nominees(db, holding, user_id, multi_data):
    """Wipe-and-rebuild a holding's nominee set from nominee_name[]/
    nominee_relationship[]/nominee_percentage[] array fields — mirrors
    wealth/services.py's create_asset/update_asset heir handling
    exactly, including why: the form's nominee section is authoritative
    on every save, not an incremental add on top of the old set.
    Returns an error string (nothing committed/deleted yet) or None on
    success. Caller must have already flushed `holding` so it has an id."""
    if multi_data is None:
        return None

    names = multi_data.getlist("nominee_name[]")
    rels = multi_data.getlist("nominee_relationship[]")
    pcts = multi_data.getlist("nominee_percentage[]")

    total_pct = 0
    new_rows = []
    for i, name in enumerate(names):
        name = name.strip()
        if not name:
            continue
        pct_raw = pcts[i] if i < len(pcts) else ""
        pct = float(pct_raw) if pct_raw else None
        if pct:
            if pct < 0 or pct > 100:
                return "Nominee percentage must be between 0 and 100."
            total_pct += pct
            if total_pct > 100:
                return f"Total nominee percentage would exceed 100% ({total_pct:.1f}%). Please check nominee shares."
        new_rows.append(InternationalHoldingNominee(
            holding_id=holding.id,
            user_id=user_id,
            name=name,
            relationship=(rels[i].strip() if i < len(rels) and rels[i].strip() else None),
            percentage=pct,
        ))

    # Only touch the table once validation of the whole submitted set
    # has passed — an existing nominee list is never partially cleared
    # on a rejected submission.
    holding.nominees.delete()
    for row in new_rows:
        db.session.add(row)
    return None


def create_holding(user_id, data, multi_data=None):
    db = _db()
    asset_type = data["asset_type"].strip()
    is_ticker_based = asset_type in InternationalAssetType.TICKER_BASED

    holding = InternationalHolding(
        user_id=user_id,
        asset_type=asset_type,
        name=data["name"].strip(),
        ticker=(data.get("ticker") or "").strip().upper() or None,
        country=(data.get("country") or "").strip() or None,
        broker_or_institution=(data.get("broker_or_institution") or "").strip() or None,
        account_number_masked=(data.get("account_number_masked") or "").strip() or None,
        native_currency=data["native_currency"].strip().upper(),
        notes=(data.get("notes") or "").strip() or None,
    )

    if is_ticker_based:
        holding.quantity = float(data["quantity"])
        holding.avg_cost_native = float(data["avg_cost_native"])
        holding.live_price_native = holding.avg_cost_native  # seeded until the first refresh
        holding.current_value_native = round(holding.quantity * holding.avg_cost_native, 2)
        holding.invested_native = holding.current_value_native
    else:
        holding.current_value_native = float(data["current_value_native"])
        holding.invested_native = holding.current_value_native

    db.session.add(holding)
    db.session.flush()  # assigns holding.id, needed for nominees below

    nominee_error = _replace_nominees(db, holding, user_id, multi_data)
    if nominee_error:
        db.session.rollback()
        return None, nominee_error

    _convert_to_usd(holding)
    db.session.commit()
    return holding, None


def update_holding(holding, data, multi_data=None):
    db = _db()
    holding.name = data["name"].strip()
    holding.ticker = (data.get("ticker") or "").strip().upper() or None
    holding.country = (data.get("country") or "").strip() or None
    holding.broker_or_institution = (data.get("broker_or_institution") or "").strip() or None
    holding.account_number_masked = (data.get("account_number_masked") or "").strip() or None
    holding.native_currency = data["native_currency"].strip().upper()
    holding.notes = (data.get("notes") or "").strip() or None

    if holding.is_ticker_based:
        holding.quantity = float(data["quantity"])
        holding.avg_cost_native = float(data["avg_cost_native"])
        effective_price = holding.live_price_native or holding.avg_cost_native
        holding.current_value_native = round(holding.quantity * effective_price, 2)
    else:
        holding.current_value_native = float(data["current_value_native"])

    nominee_error = _replace_nominees(db, holding, holding.user_id, multi_data)
    if nominee_error:
        db.session.rollback()
        return None, nominee_error

    _convert_to_usd(holding)
    recompute_holding_financials(holding)
    db.session.commit()
    return holding, None


def archive_holding(holding):
    holding.archived = True
    _db().session.commit()


def restore_holding(holding):
    holding.archived = False
    _db().session.commit()


def delete_holding_permanently(holding):
    """Only ever called on an already-archived holding (enforced at the
    route level, matching the Archive->Delete Permanently lifecycle
    used across the app). A plain session.delete() — NOT a bulk
    Query.delete() — so the ORM cascade="all, delete-orphan" on
    transactions/snapshots actually fires (see Batch 5's tradebook fix
    in app.py for what goes wrong when a bulk delete is used instead)."""
    db = _db()
    RemittanceRecord.query.filter_by(holding_id=holding.id).update({"holding_id": None})
    db.session.delete(holding)
    db.session.commit()


def refresh_holding(holding):
    """Refreshes a single holding's live price (ticker-based only) and
    USD conversion, then recomputes invested_native/xirr. Does NOT
    commit — callers batch a commit after one or more holdings (see
    refresh_all_holdings()). Never zeroes out a previously-known value
    on a failed network call."""
    if holding.is_ticker_based and holding.ticker:
        price = fetch_ticker_price(holding.ticker)
        if price:
            holding.live_price_native = price
            holding.price_updated_at = datetime.utcnow()
        if holding.quantity is not None:
            effective_price = holding.live_price_native or holding.avg_cost_native or 0
            holding.current_value_native = round(holding.quantity * effective_price, 2)

    _convert_to_usd(holding)
    recompute_holding_financials(holding)


def refresh_all_holdings(user_id=None):
    query = InternationalHolding.query.filter_by(archived=False)
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    holdings = query.all()
    for h in holdings:
        refresh_holding(h)
    _db().session.commit()
    return len(holdings)


def portfolio_totals(user_id):
    """Returns {total_usd, total_invested_usd_approx, holdings_count}
    across every active holding. total_invested_usd_approx uses each
    holding's own current fx_rate_used against its invested_native —
    same approximation caveat as portfolio_usd_xirr() below."""
    holdings = get_holdings(user_id, archived=False)
    total_usd = sum(h.usd_value or 0.0 for h in holdings)
    total_invested_usd = sum(
        (h.invested_native or 0.0) * (h.fx_rate_used or 1.0) for h in holdings
    )
    return {
        "total_usd": round(total_usd, 2),
        "total_invested_usd_approx": round(total_invested_usd, 2),
        "holdings_count": len(holdings),
    }


def portfolio_inr_value(user_id):
    """INR-equivalent of the active portfolio's USD total (Sep 2026) —
    for wiring international holdings into the main dashboard's net
    worth total and NetWorthHistory, which otherwise have no idea this
    module exists. Bridges through currency_display.usd_to_inr(), the
    same USD->INR path format_money_usd() uses. Returns 0.0 (never
    None) when there's nothing to convert or today's USD rate can't be
    fetched — callers add this straight into a running INR total
    without needing a None-check, matching this module's existing
    "never guess, never crash" refresh philosophy."""
    import currency_display
    total_usd = portfolio_totals(user_id)["total_usd"]
    if not total_usd:
        return 0.0
    inr_equiv = currency_display.usd_to_inr(total_usd)
    return inr_equiv if inr_equiv is not None else 0.0


def portfolio_usd_xirr(user_id):
    """Aggregate XIRR across every active holding's transactions,
    approximated into USD (see module docstring)."""
    holdings = get_holdings(user_id, archived=False)
    usd_txns = []
    total_usd_value = 0.0
    for h in holdings:
        rate = h.fx_rate_used or (1.0 if h.native_currency == "USD" else None)
        if rate is None:
            continue  # can't safely approximate this holding's txns into USD -- skip rather than guess
        for t in h.transactions:
            usd_txns.append({"date": t.date, "txn_type": t.txn_type, "amount_native": t.amount_native * rate})
        total_usd_value += h.usd_value or 0.0
    return _portfolio_xirr_calc(usd_txns, total_usd_value)


# ── Transactions ────────────────────────────────────────────────────

def add_transaction(holding, data):
    db = _db()
    txn = InternationalTransaction(
        user_id=holding.user_id,
        holding_id=holding.id,
        date=datetime.strptime(data["date"], "%Y-%m-%d").date(),
        txn_type=data["txn_type"].strip().upper(),
        quantity=float(data["quantity"]) if (data.get("quantity") or "").strip() else None,
        price_native=float(data["price_native"]) if (data.get("price_native") or "").strip() else None,
        amount_native=float(data["amount_native"]),
    )
    db.session.add(txn)
    db.session.flush()
    recompute_holding_financials(holding)
    db.session.commit()
    return txn


def update_transaction(txn, data):
    db = _db()
    txn.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
    txn.txn_type = data["txn_type"].strip().upper()
    txn.quantity = float(data["quantity"]) if (data.get("quantity") or "").strip() else None
    txn.price_native = float(data["price_native"]) if (data.get("price_native") or "").strip() else None
    txn.amount_native = float(data["amount_native"])
    recompute_holding_financials(txn.holding)
    db.session.commit()
    return txn


def delete_transaction(txn):
    db = _db()
    holding = txn.holding
    db.session.delete(txn)
    db.session.flush()
    recompute_holding_financials(holding)
    db.session.commit()


# ── Remittances (LRS) ───────────────────────────────────────────────

def add_remittance(user_id, data):
    db = _db()
    remit_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
    amount_inr = float(data["amount_inr"])
    rate = None
    amount_usd = 0.0
    try:
        rate, _actual_date = fetch_fx_rate("INR", "USD", remit_date)
        amount_usd = round(amount_inr * rate, 2)
    except FxRateError:
        pass  # amount_usd stays 0.0 -- route flashes a warning that LRS tracking is incomplete for this entry until it's refreshed

    remittance = RemittanceRecord(
        user_id=user_id,
        holding_id=int(data["holding_id"]) if (data.get("holding_id") or "").strip() else None,
        date=remit_date,
        amount_inr=amount_inr,
        amount_usd=amount_usd,
        fx_rate_used=rate,
        purpose=data["purpose"].strip(),
        remitting_bank=(data.get("remitting_bank") or "").strip() or None,
        notes=(data.get("notes") or "").strip() or None,
    )
    db.session.add(remittance)
    db.session.commit()
    return remittance


def delete_remittance(remittance):
    db = _db()
    db.session.delete(remittance)
    db.session.commit()


def get_lrs_status(user_id, anchor_date=None):
    """Cumulative LRS remittance status for the Indian financial year
    (1 Apr - 31 Mar) containing `anchor_date` (defaults to today)."""
    anchor_date = anchor_date or today_ist()
    fy_start, fy_end = fy_bounds(anchor_date)
    remittances = (RemittanceRecord.query
                   .filter_by(user_id=user_id)
                   .filter(RemittanceRecord.date >= fy_start, RemittanceRecord.date <= fy_end)
                   .order_by(RemittanceRecord.date.desc())
                   .all())
    total_usd = sum(r.amount_usd for r in remittances)
    remaining_usd = max(0.0, LRS_ANNUAL_LIMIT_USD - total_usd)
    pct_used = min(100.0, round((total_usd / LRS_ANNUAL_LIMIT_USD) * 100, 1)) if LRS_ANNUAL_LIMIT_USD else 0.0

    if total_usd >= LRS_ANNUAL_LIMIT_USD:
        status = "exceeded"
    elif total_usd >= LRS_ANNUAL_LIMIT_USD * 0.8:
        status = "warning"
    else:
        status = "ok"

    return {
        "fy_label": fy_label(anchor_date), "fy_start": fy_start, "fy_end": fy_end,
        "total_usd": round(total_usd, 2), "remaining_usd": round(remaining_usd, 2),
        "limit_usd": LRS_ANNUAL_LIMIT_USD, "pct_used": pct_used, "status": status,
        "remittances": remittances,
    }


# ── Schedule FA ─────────────────────────────────────────────────────

def get_schedule_fa_summary(user_id, calendar_year):
    """Per-holding opening/peak/closing USD value for the given
    CALENDAR year (Schedule FA's own reporting period — see models.py's
    module docstring), plus gross sale proceeds within that year.
    data_complete on a row is a rough signal (a snapshot near both the
    start and end of the year), not a guarantee — a genuinely
    incomplete year (module just started, or the scheduled job hasn't
    been run consistently) falls back to the holding's CURRENT value
    for all three figures, clearly flagged as incomplete rather than
    silently presented as accurate."""
    year_start, year_end = calendar_year_bounds(calendar_year)
    holdings = get_holdings(user_id, archived=False)
    rows = []

    for h in holdings:
        snapshots = (InternationalValueSnapshot.query
                     .filter_by(holding_id=h.id)
                     .filter(InternationalValueSnapshot.date >= year_start,
                             InternationalValueSnapshot.date <= year_end)
                     .order_by(InternationalValueSnapshot.date)
                     .all())
        if snapshots:
            opening = snapshots[0].usd_value
            closing = snapshots[-1].usd_value
            peak = max(s.usd_value for s in snapshots)
            data_complete = (snapshots[0].date <= year_start + timedelta(days=10) and
                              snapshots[-1].date >= year_end - timedelta(days=10))
        else:
            opening = closing = peak = h.usd_value or 0.0
            data_complete = False

        sell_txns_in_year = [t for t in h.transactions
                              if t.txn_type == InternationalTxnType.SELL and year_start <= t.date <= year_end]
        gross_proceeds_usd = 0.0
        if sell_txns_in_year:
            rate = h.fx_rate_used or (1.0 if h.native_currency == "USD" else 1.0)
            gross_proceeds_usd = round(sum(t.amount_native for t in sell_txns_in_year) * rate, 2)

        buy_dates = [t.date for t in h.transactions if t.txn_type == InternationalTxnType.BUY]
        acquisition_date = min(buy_dates) if buy_dates else (h.created_at.date() if h.created_at else None)

        rows.append({
            "holding": h, "country": h.country or "—", "asset_type": h.asset_type,
            "acquisition_date": acquisition_date,
            "opening_usd": round(opening or 0, 2), "peak_usd": round(peak or 0, 2),
            "closing_usd": round(closing or 0, 2), "gross_proceeds_usd": gross_proceeds_usd,
            "data_complete": data_complete,
        })

    return {
        "calendar_year": calendar_year, "rows": rows,
        "any_incomplete": any(not r["data_complete"] for r in rows),
    }


# ── Scheduled snapshot job ──────────────────────────────────────────

def take_daily_snapshot(user_id=None):
    """Records today's USD value for every active holding — run once a
    day via `flask international snapshot` (see cli.py), mirroring
    `flask wealth snapshot`. Idempotent for the same day: the
    UniqueConstraint on (holding_id, date) means re-running today just
    updates today's row rather than erroring or duplicating."""
    db = _db()
    today = today_ist()
    query = InternationalHolding.query.filter_by(archived=False)
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    holdings = query.all()

    count = 0
    for h in holdings:
        existing = InternationalValueSnapshot.query.filter_by(holding_id=h.id, date=today).first()
        if existing:
            existing.usd_value = h.usd_value or 0.0
        else:
            db.session.add(InternationalValueSnapshot(holding_id=h.id, date=today, usd_value=h.usd_value or 0.0))
        count += 1
    db.session.commit()
    return count
