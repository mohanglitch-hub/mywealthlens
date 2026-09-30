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

from collections import deque
from datetime import date as _date

from international_centre.models import (
    InternationalHolding, InternationalTransaction, RemittanceRecord,
    InternationalValueSnapshot, InternationalHoldingNominee,
    InternationalHoldingDocument, VestingTranche,
    InternationalAssetType, InternationalTxnType, LRS_ANNUAL_LIMIT_USD,
    TCS_THRESHOLD_INR, TCS_RATE,
)
from international_centre.utils import (
    fy_bounds, fy_label, calendar_year_bounds, fetch_ticker_price, is_long_term,
)
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

def _resolve_dividend_amount(data):
    """Batch 9.5 (Sep 2026) — DIVIDEND only. If Gross Amount / Tax
    Withheld were given, the real cash flow (amount_native, what XIRR
    uses) is DERIVED as gross - withheld rather than taken from the
    form's amount_native field directly, so the two can never
    disagree. Falls back to amount_native exactly as entered when
    gross/withheld are blank (every dividend logged before this batch,
    and any BUY/SELL, take this path unchanged). Returns
    (amount_native, gross_amount_native_or_None, tax_withheld_or_None)."""
    gross_raw = (data.get("gross_amount_native") or "").strip()
    if not gross_raw:
        return float(data["amount_native"]), None, None
    gross = float(gross_raw)
    withheld_raw = (data.get("tax_withheld_native") or "").strip()
    withheld = float(withheld_raw) if withheld_raw else 0.0
    return round(gross - withheld, 2), gross, withheld


def add_transaction(holding, data):
    db = _db()
    txn_type = data["txn_type"].strip().upper()
    if txn_type == InternationalTxnType.DIVIDEND:
        amount_native, gross, withheld = _resolve_dividend_amount(data)
    else:
        amount_native, gross, withheld = float(data["amount_native"]), None, None

    txn = InternationalTransaction(
        user_id=holding.user_id,
        holding_id=holding.id,
        date=datetime.strptime(data["date"], "%Y-%m-%d").date(),
        txn_type=txn_type,
        quantity=float(data["quantity"]) if (data.get("quantity") or "").strip() else None,
        price_native=float(data["price_native"]) if (data.get("price_native") or "").strip() else None,
        amount_native=amount_native,
        gross_amount_native=gross,
        tax_withheld_native=withheld,
    )
    db.session.add(txn)
    db.session.flush()
    recompute_holding_financials(holding)
    db.session.commit()
    return txn


def update_transaction(txn, data):
    db = _db()
    txn_type = data["txn_type"].strip().upper()
    if txn_type == InternationalTxnType.DIVIDEND:
        amount_native, gross, withheld = _resolve_dividend_amount(data)
    else:
        amount_native, gross, withheld = float(data["amount_native"]), None, None

    txn.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
    txn.txn_type = txn_type
    txn.quantity = float(data["quantity"]) if (data.get("quantity") or "").strip() else None
    txn.price_native = float(data["price_native"]) if (data.get("price_native") or "").strip() else None
    txn.amount_native = amount_native
    txn.gross_amount_native = gross
    txn.tax_withheld_native = withheld
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


# ── RSU/ESPP Vesting Tranches (Batch 9.7, Sep 2026) ───────────────────
# CRUD only here -- these don't feed recompute_holding_financials() the
# way BUY/SELL/DIVIDEND transactions do (a vesting event isn't a cash
# flow XIRR should see; Mohan still enters the holding's running
# quantity by hand, same as everywhere else in this module). What they
# DO feed: classify_capital_gains()'s FIFO lot-matching below (as an
# acquisition lot, cost-based at FMV) and get_vesting_perquisite_summary().

def add_vesting_tranche(holding, data):
    db = _db()
    tranche = VestingTranche(
        user_id=holding.user_id,
        holding_id=holding.id,
        plan_type=data["plan_type"].strip().upper(),
        grant_date=(datetime.strptime(data["grant_date"], "%Y-%m-%d").date()
                    if (data.get("grant_date") or "").strip() else None),
        vest_date=datetime.strptime(data["vest_date"], "%Y-%m-%d").date(),
        quantity=float(data["quantity"]),
        fmv_native=float(data["fmv_native"]),
        purchase_price_native=(float(data["purchase_price_native"])
                                if (data.get("purchase_price_native") or "").strip() else None),
        notes=(data.get("notes") or "").strip() or None,
    )
    db.session.add(tranche)
    db.session.commit()
    return tranche


def update_vesting_tranche(tranche, data):
    db = _db()
    tranche.plan_type = data["plan_type"].strip().upper()
    tranche.grant_date = (datetime.strptime(data["grant_date"], "%Y-%m-%d").date()
                           if (data.get("grant_date") or "").strip() else None)
    tranche.vest_date = datetime.strptime(data["vest_date"], "%Y-%m-%d").date()
    tranche.quantity = float(data["quantity"])
    tranche.fmv_native = float(data["fmv_native"])
    tranche.purchase_price_native = (float(data["purchase_price_native"])
                                      if (data.get("purchase_price_native") or "").strip() else None)
    tranche.notes = (data.get("notes") or "").strip() or None
    db.session.commit()
    return tranche


def delete_vesting_tranche(tranche):
    db = _db()
    db.session.delete(tranche)
    db.session.commit()


def get_vesting_perquisite_summary(user_id, fy_start_year):
    """RSU/ESPP vesting tranches and their taxable PERQUISITE value
    -- (FMV_at_vest - price_paid) * quantity -- which Indian tax law
    treats as SALARY income in the FY the shares actually vested (a
    completely separate, earlier event from any capital gain realized
    when those shares are later sold -- see classify_capital_gains(),
    which uses this same FMV as the eventual cost basis). Scoped to the
    Indian FY starting 1 Apr `fy_start_year`, matching TCS/DTAA/capital
    gains' own FY-based reporting. Same 'not a filing document, confirm
    with a CA' honesty as every other tax-adjacent report in this
    module -- the actual perquisite value your employer reports is
    whatever they put on your Form 12BA/Form 16, which may value FMV
    differently (e.g. the closing price on a specific exchange on the
    vest date) than what's entered here."""
    import currency_display
    fy_start, fy_end = fy_bounds(_date(fy_start_year, 4, 1))
    holdings = (InternationalHolding.query
                .filter_by(user_id=user_id, asset_type=InternationalAssetType.RSU_ESPP)
                .all())

    rows = []
    any_missing_rate = False
    for h in holdings:
        tranches = [v for v in h.vesting_tranches if fy_start <= v.vest_date <= fy_end]
        if not tranches:
            continue
        rate = h.fx_rate_used or (1.0 if h.native_currency == "USD" else None)
        for v in tranches:
            if rate is None:
                any_missing_rate = True
                perquisite_inr = None
            else:
                perquisite_inr = currency_display.usd_to_inr(round(v.perquisite_value_native * rate, 2))
            rows.append({
                "holding": h, "tranche": v, "currency": h.native_currency,
                "plan_type": v.plan_type, "vest_date": v.vest_date, "quantity": v.quantity,
                "fmv_native": v.fmv_native, "purchase_price_native": v.purchase_price_native or 0.0,
                "perquisite_native": v.perquisite_value_native, "perquisite_inr": perquisite_inr,
            })

    rows.sort(key=lambda r: r["vest_date"])
    total_perquisite_inr = sum(r["perquisite_inr"] for r in rows if r["perquisite_inr"] is not None)

    return {
        "fy_label": fy_label(_date(fy_start_year, 4, 1)), "fy_start": fy_start, "fy_end": fy_end,
        "rows": rows, "total_perquisite_inr": round(total_perquisite_inr, 2),
        "any_missing_rate": any_missing_rate,
    }


# ── Remittances (LRS) ───────────────────────────────────────────────

def _compute_tcs(user_id, remit_date, amount_inr):
    """Batch 9.4 (Sep 2026). Marginal TCS on the portion of THIS
    remittance that pushes the financial year's running total past
    TCS_THRESHOLD_INR — not a flat 20% of the whole remittance just
    because the FY total is over the threshold. Only the amount ABOVE
    the threshold is taxed at TCS_RATE, matching how TCS actually
    works. `prior_total_inr` sums every OTHER remittance already
    logged for this user in the same FY, regardless of its own date
    relative to this one — same "what's logged so far" semantics as
    get_lrs_status(), and simplest for a user entering remittances out
    of strict chronological order."""
    fy_start, fy_end = fy_bounds(remit_date)
    prior_total_inr = (
        RemittanceRecord.query
        .filter_by(user_id=user_id)
        .filter(RemittanceRecord.date >= fy_start, RemittanceRecord.date <= fy_end)
        .with_entities(RemittanceRecord.amount_inr)
        .all()
    )
    prior_total_inr = sum(r[0] for r in prior_total_inr)
    new_cumulative = prior_total_inr + amount_inr
    if new_cumulative <= TCS_THRESHOLD_INR:
        return 0.0
    taxable_portion = min(amount_inr, new_cumulative - TCS_THRESHOLD_INR)
    return round(taxable_portion * TCS_RATE, 2)


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

    tcs_amount_inr = _compute_tcs(user_id, remit_date, amount_inr)

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
        tcs_amount_inr=tcs_amount_inr,
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
    total_tcs_inr = round(sum(r.tcs_amount_inr or 0.0 for r in remittances), 2)  # Batch 9.4

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
        "remittances": remittances, "total_tcs_inr": total_tcs_inr,
    }


def get_lrs_history(user_id, years=5, anchor_date=None):
    """Per-financial-year LRS usage totals for the last `years` Indian
    financial years INCLUDING the current one, oldest first. Batch 9.8
    (Sep 2026) — unlike get_lrs_status() (a live gauge for a single
    FY), this is a multi-year trend view: each entry is independent and
    always included even if $0 was remitted that year, so a genuinely
    quiet year shows as a gap in the trend rather than being silently
    skipped."""
    anchor_date = anchor_date or today_ist()
    current_fy_start_year = fy_bounds(anchor_date)[0].year

    history = []
    for i in range(years - 1, -1, -1):
        start_year = current_fy_start_year - i
        fy_start, fy_end = fy_bounds(_date(start_year, 4, 1))
        remittances = (RemittanceRecord.query
                       .filter_by(user_id=user_id)
                       .filter(RemittanceRecord.date >= fy_start, RemittanceRecord.date <= fy_end)
                       .all())
        total_usd = sum(r.amount_usd for r in remittances)
        history.append({
            "fy_label": fy_label(_date(start_year, 4, 1)),
            "fy_start": fy_start, "fy_end": fy_end,
            "count": len(remittances),
            "total_usd": round(total_usd, 2),
            "total_inr": round(sum(r.amount_inr for r in remittances), 2),
            "total_tcs_inr": round(sum(r.tcs_amount_inr or 0.0 for r in remittances), 2),
            "pct_used": min(100.0, round((total_usd / LRS_ANNUAL_LIMIT_USD) * 100, 1)) if LRS_ANNUAL_LIMIT_USD else 0.0,
        })
    return history


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
    """Refreshes every active holding's live price/FX (Sep 2026 fix —
    see Batch 9.1 below), THEN records today's USD value — run once a
    day via `flask international snapshot` (see cli.py), mirroring
    `flask wealth snapshot`. Idempotent for the same day: the
    UniqueConstraint on (holding_id, date) means re-running today just
    updates today's row rather than erroring or duplicating.

    Batch 9.1 bug fix: this function used to snapshot whatever
    usd_value a holding ALREADY had, without ever refreshing it first.
    Since this module has no separate scheduled price-refresh job
    (unlike domestic stocks' `flask prices refresh`), a holding whose
    owner never clicked "Refresh" on the dashboard would have the same
    stale value recorded every single day — silently defeating
    Schedule FA's whole reason for existing (a real daily value trail
    to derive the year's true peak from). refresh_all_holdings() is
    called first so every snapshot reflects a genuinely fresh price/FX
    lookup, same as a manual "Refresh All" click would produce."""
    db = _db()
    refresh_all_holdings(user_id)

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


# ── Document Vault (Batch 9.3, Sep 2026) ─────────────────────────────
# Closes the last parity gap flagged when this module first shipped —
# see models.py's InternationalHoldingDocument docstring for the
# iv/is_encrypted schema-parity note and the confirmed encryption-JS
# gap (pre-existing across the whole app, not introduced here).

def save_document_metadata(db, holding, user_id, doc_type, original_name,
                            stored_name, file_path, file_size=None,
                            notes=None, title=None, iv=None, is_encrypted=False):
    """Persist a document's metadata after the file itself has already
    been saved to local disk by utils.save_document_file()."""
    doc = InternationalHoldingDocument(
        holding_id=holding.id, user_id=user_id, doc_type=doc_type,
        title=title, original_name=original_name,
        stored_name=stored_name, file_path=file_path,
        file_size=file_size, notes=notes,
        iv=iv or None, is_encrypted=bool(is_encrypted),
    )
    db.session.add(doc)
    db.session.commit()
    return doc


def delete_document(db, doc, user_id):
    """Remove document metadata. Caller must delete the actual file
    first (see utils.delete_document_file) — matches every other
    module's Document Vault delete_document()."""
    if doc.user_id != user_id:
        return False, "You do not have permission to delete this document."
    db.session.delete(doc)
    db.session.commit()
    return True, None


def get_vault_documents(user_id, q=None, asset_type=None, doc_type=None):
    """All documents across every one of the user's international
    holdings, joined with holding info for display and filtering.
    Always scoped by user_id (IDOR check)."""
    query = (InternationalHoldingDocument.query
             .join(InternationalHolding,
                   InternationalHoldingDocument.holding_id == InternationalHolding.id)
             .filter(InternationalHoldingDocument.user_id == user_id))

    if doc_type:
        query = query.filter(InternationalHoldingDocument.doc_type == doc_type)

    docs = query.order_by(InternationalHoldingDocument.uploaded_at.desc()).all()

    if asset_type:
        docs = [d for d in docs if d.holding.asset_type == asset_type]

    if q:
        ql = q.lower()
        docs = [d for d in docs
                if ql in (d.display_name or "").lower()
                or ql in (d.holding.name or "").lower()
                or ql in (d.doc_type or "").lower()]

    return docs


def vault_summary(user_id):
    """Total document count + per-asset-type counts for the Vault's
    summary cards. Never fabricated — counts real rows only."""
    all_docs = get_vault_documents(user_id)
    by_asset_type = []
    for at in InternationalAssetType.ALL:
        count = sum(1 for d in all_docs if d.holding.asset_type == at)
        if count:
            by_asset_type.append({"asset_type": at, "count": count})

    return {
        "total": len(all_docs),
        "by_asset_type": by_asset_type,
    }


# ── DTAA / Form 67 support (Batch 9.5, Sep 2026) ─────────────────────
# Foreign dividend withholding tax, summarized by INDIAN financial year
# (1 Apr - 31 Mar) — dividend income is offered to tax in the FY it's
# received, unlike Schedule FA's calendar-year reporting period (see
# models.py's module docstring for why those two "years" are kept
# deliberately separate throughout this module).
#
# IMPORTANT scope note: this deliberately stops at "here is your gross
# foreign dividend and tax withheld for the year" — the actual DTAA
# foreign tax credit is capped at the LOWER of (a) tax actually paid
# abroad and (b) the Indian tax payable on that same income, which
# depends on Mohan's full tax computation (slab, other income,
# deductions) that this module has no visibility into. Computing a
# specific "creditable amount" here would be a confident-looking wrong
# number for anyone in the higher slab bracket or with brought-forward
# losses. The report gives the two INPUT figures Form 67 actually asks
# for and stops there — same honesty as Schedule FA's own disclaimer.

def get_dtaa_summary(user_id, fy_start_year):
    """Per-holding gross dividend / tax withheld / net received for the
    Indian FY starting 1 Apr `fy_start_year`, in both native currency
    and an approximate INR equivalent (bridged native -> USD -> INR via
    the holding's own cached fx_rate_used and currency_display's
    usd_to_inr(), same approximation path as portfolio_inr_value() —
    NOT the CBDT-prescribed SBI TT buying rate on each dividend's own
    date, which is what an actual Form 67 filing requires)."""
    import currency_display
    fy_start, fy_end = fy_bounds(_date(fy_start_year, 4, 1))
    holdings = InternationalHolding.query.filter_by(user_id=user_id).all()

    rows = []
    any_missing_rate = False
    for h in holdings:
        divs = [t for t in h.transactions
                if t.txn_type == InternationalTxnType.DIVIDEND and fy_start <= t.date <= fy_end]
        if not divs:
            continue

        gross_native = sum((d.gross_amount_native if d.gross_amount_native is not None else d.amount_native) for d in divs)
        withheld_native = sum((d.tax_withheld_native or 0.0) for d in divs)
        net_native = sum(d.amount_native for d in divs)

        rate = h.fx_rate_used or (1.0 if h.native_currency == "USD" else None)
        if rate is None:
            any_missing_rate = True
            gross_inr = withheld_inr = net_inr = None
        else:
            gross_inr = currency_display.usd_to_inr(round(gross_native * rate, 2))
            withheld_inr = currency_display.usd_to_inr(round(withheld_native * rate, 2))
            net_inr = currency_display.usd_to_inr(round(net_native * rate, 2))

        rows.append({
            "holding": h, "country": h.country or "—", "currency": h.native_currency,
            "gross_native": round(gross_native, 2), "withheld_native": round(withheld_native, 2),
            "net_native": round(net_native, 2),
            "gross_inr": gross_inr, "withheld_inr": withheld_inr, "net_inr": net_inr,
        })

    total_gross_inr = sum(r["gross_inr"] for r in rows if r["gross_inr"] is not None)
    total_withheld_inr = sum(r["withheld_inr"] for r in rows if r["withheld_inr"] is not None)

    return {
        "fy_label": fy_label(_date(fy_start_year, 4, 1)), "fy_start": fy_start, "fy_end": fy_end,
        "rows": rows,
        "total_gross_inr": round(total_gross_inr, 2), "total_withheld_inr": round(total_withheld_inr, 2),
        "any_missing_rate": any_missing_rate,
    }


# ── Capital Gains Classification: LTCG / STCG (Batch 9.6, Sep 2026) ──
# Indian tax rule for foreign (unlisted) equity/funds: LONG-term if
# held for MORE than 24 months, else SHORT-term — a materially
# different tax rule from listed Indian equity's 12-month LTCG
# threshold, so this is genuinely its own classification, not a copy
# of anything domestic stocks already have. FIFO lot matching (first
# lot bought is the first lot sold) — the same convention Indian tax
# law itself uses by default when specific-lot identification isn't
# elected.

def classify_capital_gains(holding):
    """Returns a list of realized-gain records for one holding, oldest
    SELL first, by matching each SELL against the earliest still-open
    acquisition lot(s) (FIFO) — a single SELL that spans more than one
    lot produces one record per lot it draws from, since each lot can
    have its own acquisition date and therefore its own LTCG/STCG
    classification. Ticker-based holdings only (quantity is the whole
    basis for lot-matching, and only ticker-based holdings' BUY/SELL
    transactions and RSU_ESPP holdings' vesting tranches carry a
    quantity — see holding_detail.html).

    Batch 9.7 (Sep 2026): an acquisition lot can now come from either a
    BUY transaction OR a VestingTranche (RSU_ESPP holdings) — a vesting
    event is economically identical to a purchase for FIFO purposes,
    just cost-based at FMV-at-vest rather than a price actually paid
    (see VestingTranche.cost_basis_native's own docstring for why FMV,
    not purchase price, is the correct capital-gains cost basis even
    for a discounted ESPP purchase — the discount was already taxed
    once, as perquisite income). BUY/VEST events on the same date sort
    before a SELL on that date (rank 0 vs. 1) so a same-day acquisition
    is available to match a same-day disposal, matching how a human
    would read the day's activity."""
    if not holding.is_ticker_based:
        return []

    events = []
    for t in holding.transactions:
        if t.txn_type == InternationalTxnType.BUY:
            events.append((t.date, 0, t.id, "BUY", t))
        elif t.txn_type == InternationalTxnType.SELL:
            events.append((t.date, 1, t.id, "SELL", t))
    for v in holding.vesting_tranches:
        events.append((v.vest_date, 0, v.id, "VEST", v))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    lots = deque()
    gains = []

    for event_date, _rank, _id, kind, obj in events:
        if kind == "BUY":
            if obj.quantity and obj.quantity > 0:
                lots.append({"date": obj.date, "qty": obj.quantity, "unit_cost": obj.amount_native / obj.quantity})
        elif kind == "VEST":
            if obj.quantity and obj.quantity > 0:
                lots.append({"date": obj.vest_date, "qty": obj.quantity, "unit_cost": obj.fmv_native})
        elif kind == "SELL":
            t = obj
            if not t.quantity or t.quantity <= 0:
                continue
            qty_to_sell = t.quantity
            unit_proceeds = t.amount_native / t.quantity
            while qty_to_sell > 1e-9 and lots:
                lot = lots[0]
                matched_qty = min(qty_to_sell, lot["qty"])
                long_term = is_long_term(lot["date"], event_date)
                gains.append({
                    "sell_date": event_date, "acquisition_date": lot["date"],
                    "quantity": round(matched_qty, 6),
                    "classification": "LTCG" if long_term else "STCG",
                    "cost_basis_native": round(matched_qty * lot["unit_cost"], 2),
                    "proceeds_native": round(matched_qty * unit_proceeds, 2),
                    "gain_native": round(matched_qty * (unit_proceeds - lot["unit_cost"]), 2),
                })
                lot["qty"] -= matched_qty
                qty_to_sell -= matched_qty
                if lot["qty"] <= 1e-9:
                    lots.popleft()
            # qty_to_sell > 0 here means a SELL for more than was ever
            # acquired through recorded BUY transactions/vesting
            # tranches (e.g. the holding's opening quantity was seeded
            # directly rather than via a BUY) — that unmatched portion
            # has no known cost basis, so it's silently left
            # unclassified rather than guessing a cost basis of zero
            # (which would overstate the gain). Same "never guess"
            # philosophy as the rest of this module.

    return gains


def get_capital_gains_summary(user_id, fy_start_year):
    """LTCG/STCG-classified realized gains across every ticker-based
    holding, for SELLs falling in the Indian FY starting 1 Apr
    `fy_start_year` (capital gains tax follows the year the asset was
    SOLD, not a calendar year). gain_usd on each row is an
    approximation via the holding's own cached fx_rate_used, same
    caveat as portfolio_usd_xirr()."""
    fy_start, fy_end = fy_bounds(_date(fy_start_year, 4, 1))
    holdings = InternationalHolding.query.filter_by(user_id=user_id).all()

    rows = []
    any_missing_rate = False
    for h in holdings:
        gains = [g for g in classify_capital_gains(h) if fy_start <= g["sell_date"] <= fy_end]
        if not gains:
            continue
        rate = h.fx_rate_used or (1.0 if h.native_currency == "USD" else None)
        for g in gains:
            g["holding"] = h
            g["currency"] = h.native_currency
            if rate is None:
                any_missing_rate = True
                g["gain_usd"] = None
            else:
                g["gain_usd"] = round(g["gain_native"] * rate, 2)
        rows.extend(gains)

    rows.sort(key=lambda g: g["sell_date"], reverse=True)
    ltcg_total_usd = sum(g["gain_usd"] for g in rows if g["classification"] == "LTCG" and g["gain_usd"] is not None)
    stcg_total_usd = sum(g["gain_usd"] for g in rows if g["classification"] == "STCG" and g["gain_usd"] is not None)

    return {
        "fy_label": fy_label(_date(fy_start_year, 4, 1)), "fy_start": fy_start, "fy_end": fy_end,
        "rows": rows,
        "ltcg_total_usd": round(ltcg_total_usd, 2), "stcg_total_usd": round(stcg_total_usd, 2),
        "any_missing_rate": any_missing_rate,
    }
