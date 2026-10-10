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
    InternationalHoldingDocument, VestingTranche, InternationalTimeline,
    InternationalReminderAck, ScheduleFaYearInput, RateBasis, CgMethod,
    InternationalAssetType, InternationalTxnType, RemittancePurpose,
    TimelineEvent, LRS_ANNUAL_LIMIT_USD,
)
from international_centre import tcs_rules
from international_centre import rates
from international_centre.utils import (
    fy_bounds, fy_label, calendar_year_bounds, fetch_ticker_price, is_long_term,
)
from international_xirr import holding_xirr, portfolio_xirr as _portfolio_xirr_calc
from fx_rates import fetch_fx_rate, FxRateError
from wealth.timezone_utils import today_ist


def _db():
    from models import db
    return db


# ── Timeline (Batch 10.3, Oct 2026) ─────────────────────────────────

def log_timeline(holding, event_type, description):
    """Append an audit entry for `holding`. Does NOT commit — it rides
    along with the caller's own commit, so an action and its audit
    entry are saved together or not at all. Append-only."""
    _db().session.add(InternationalTimeline(
        holding_id=holding.id, user_id=holding.user_id,
        event_type=event_type, description=description[:500],
    ))


def get_timeline(holding, limit=20):
    """Newest-first audit entries for a holding."""
    return holding.timeline.limit(limit).all()


def _money(currency, amount):
    return f"{currency} {amount:,.2f}"


def _nominee_signature(holding):
    """Comparable snapshot of a holding's nominee set, for detecting
    whether an edit actually changed it."""
    return sorted(
        (n.name, (n.relationship or ""), float(n.percentage or 0))
        for n in holding.nominees
    )


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
        entity_address=(data.get("entity_address") or "").strip() or None,
        entity_zip=(data.get("entity_zip") or "").strip() or None,
        entity_nature=(data.get("entity_nature") or "").strip() or None,
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

    holding.value_updated_at = datetime.utcnow()
    db.session.add(holding)
    db.session.flush()  # assigns holding.id, needed for nominees below

    nominee_error = _replace_nominees(db, holding, user_id, multi_data)
    if nominee_error:
        db.session.rollback()
        return None, nominee_error

    _convert_to_usd(holding)
    log_timeline(holding, TimelineEvent.CREATED,
                 f"Added {holding.name} ({holding.asset_type}), "
                 f"value {_money(holding.native_currency, holding.current_value_native)}")
    db.session.commit()
    return holding, None


_TRACKED_FIELDS = [
    ("name", "name"), ("ticker", "ticker"), ("country", "country"),
    ("broker_or_institution", "broker/institution"),
    ("native_currency", "currency"), ("quantity", "quantity"),
    ("avg_cost_native", "avg cost"), ("current_value_native", "value"),
    ("notes", "notes"),
]


def _fmt_field(value):
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def update_holding(holding, data, multi_data=None):
    db = _db()
    before = {attr: getattr(holding, attr) for attr, _ in _TRACKED_FIELDS}
    nominees_before = _nominee_signature(holding)
    holding.name = data["name"].strip()
    holding.ticker = (data.get("ticker") or "").strip().upper() or None
    holding.country = (data.get("country") or "").strip() or None
    holding.broker_or_institution = (data.get("broker_or_institution") or "").strip() or None
    holding.account_number_masked = (data.get("account_number_masked") or "").strip() or None
    holding.entity_address = (data.get("entity_address") or "").strip() or None
    holding.entity_zip = (data.get("entity_zip") or "").strip() or None
    holding.entity_nature = (data.get("entity_nature") or "").strip() or None
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

    # ── Audit trail (Batch 10.3) + manual-value freshness ──
    changes = []
    for attr, label in _TRACKED_FIELDS:
        old_v, new_v = before[attr], getattr(holding, attr)
        if attr == "notes":
            if (old_v or "") != (new_v or ""):
                changes.append("notes edited")
        elif old_v != new_v:
            changes.append(f"{label}: {_fmt_field(old_v)} → {_fmt_field(new_v)}")
    if before["current_value_native"] != holding.current_value_native:
        holding.value_updated_at = datetime.utcnow()
    if changes:
        log_timeline(holding, TimelineEvent.UPDATED, "Updated — " + "; ".join(changes))
    db.session.flush()
    if _nominee_signature(holding) != nominees_before:
        total = holding.total_nominees_percentage
        count = holding.nominees.count()
        log_timeline(holding, TimelineEvent.NOMINEE_UPDATED,
                     f"Nominees updated — {count} nominee(s), shares total {total:.0f}%")
    db.session.commit()
    return holding, None


def archive_holding(holding):
    holding.archived = True
    log_timeline(holding, TimelineEvent.ARCHIVED, "Archived")
    _db().session.commit()


def restore_holding(holding):
    holding.archived = False
    log_timeline(holding, TimelineEvent.RESTORED, "Restored from archive")
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


def refresh_holding(holding, log_event=False):
    """Refreshes a single holding's live price (ticker-based only) and
    USD conversion, then recomputes invested_native/xirr. Does NOT
    commit — callers batch a commit after one or more holdings (see
    refresh_all_holdings()). Never zeroes out a previously-known value
    on a failed network call.

    `log_event=True` (Batch 10.3) records a Value Refreshed entry on the
    holding's timeline when the value actually moved. Only the manual
    single-holding Refresh button passes it — the daily snapshot job and
    Refresh All would otherwise bury the timeline in routine entries."""
    old_native = holding.current_value_native
    old_usd = holding.usd_value
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
    if log_event and (holding.current_value_native != old_native or holding.usd_value != old_usd):
        log_timeline(holding, TimelineEvent.VALUE_REFRESHED,
                     f"Value refreshed — {_money(holding.native_currency, old_native)} → "
                     f"{_money(holding.native_currency, holding.current_value_native)} "
                     f"(${holding.usd_value:,.2f} USD)")


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


# ── Staleness, P&L, listing, alerts (Batch 10.2 / 10.3, Oct 2026) ─────

# A ticker price older than this is "stale". 5 days comfortably spans a
# weekend plus a market holiday, so a normal long weekend doesn't nag.
STALE_PRICE_DAYS = 5
# A manually-valued asset (bank balance, property, bond) has no live feed;
# its value is only as fresh as the last time you typed it in.
STALE_MANUAL_VALUE_DAYS = 90


def get_staleness(holding, now=None):
    """How fresh is this holding's value?

    Returns {kind: 'price'|'value', status: 'fresh'|'stale'|'never',
    age_days, label}. Ticker-based holdings are judged on their last live
    price refresh; everything else on when the user last set the value."""
    now = now or datetime.utcnow()
    if holding.is_ticker_based and holding.ticker:
        ts, limit, kind = holding.price_updated_at, STALE_PRICE_DAYS, "price"
    else:
        ts = holding.value_updated_at or holding.updated_at or holding.created_at
        limit, kind = STALE_MANUAL_VALUE_DAYS, "value"

    if ts is None:
        return {"kind": kind, "status": "never", "age_days": None,
                "label": "Price never refreshed — value is based on your cost"}
    age = (now - ts).days
    status = "stale" if age > limit else "fresh"
    when = "today" if age <= 0 else ("1 day ago" if age == 1 else f"{age} days ago")
    noun = "Price refreshed" if kind == "price" else "Value last updated"
    return {"kind": kind, "status": status, "age_days": age, "label": f"{noun} {when}"}


def holding_pnl(holding):
    """Gain/loss on the net cash put in, in the holding's own currency.

    gain = current value - (buys - sells). Because sale proceeds are
    netted off the cost, this is the combined realised + unrealised result
    — it is NOT split into the two, and it excludes dividends, which are
    returned separately. Not shown for a foreign bank account, where a
    "gain" on a balance isn't meaningful."""
    dividends = sum(t.amount_native for t in holding.transactions
                    if t.txn_type == InternationalTxnType.DIVIDEND)
    show = holding.asset_type != InternationalAssetType.FOREIGN_BANK_ACCOUNT
    invested = holding.invested_native
    if not show or invested is None:
        return {"show": False, "invested_native": invested, "gain_native": None,
                "gain_pct": None, "dividends_native": round(dividends, 2)}
    gain = round((holding.current_value_native or 0.0) - invested, 2)
    pct = round(gain / invested * 100, 2) if invested > 0 else None
    return {"show": True, "invested_native": invested, "gain_native": gain,
            "gain_pct": pct, "dividends_native": round(dividends, 2)}


def get_value_history(holding):
    """Dated USD values for the holding from the daily snapshot job,
    oldest first. Only ever contains days the scheduled job actually ran,
    so a holding that is new (or a PC that was off) has few points."""
    snaps = (InternationalValueSnapshot.query
             .filter_by(holding_id=holding.id)
             .order_by(InternationalValueSnapshot.date)
             .all())
    points = [{"date": sn.date.isoformat(), "usd_value": round(sn.usd_value or 0.0, 2)} for sn in snaps]
    change_usd = change_pct = None
    if len(points) >= 2 and points[0]["usd_value"]:
        change_usd = round(points[-1]["usd_value"] - points[0]["usd_value"], 2)
        change_pct = round(change_usd / points[0]["usd_value"] * 100, 2)
    return {"points": points, "change_usd": change_usd, "change_pct": change_pct}


def _nominee_gap(holding):
    """None if nominees are complete, else a short reason. Same rule as
    Family Centre's Coverage Gaps check, so the two never disagree."""
    if holding.nominees.count() == 0:
        return "No nominee added"
    total = holding.total_nominees_percentage
    if total < 100:
        return f"Nominees total {total:.0f}%, not 100%"
    return None


def get_nominee_gap(holding):
    """Public wrapper: None if nominees are complete, else a short reason."""
    return _nominee_gap(holding)


SORT_OPTIONS = [
    ("value_high", "Value: High → Low"),
    ("value_low", "Value: Low → High"),
    ("name_az", "Name: A → Z"),
    ("name_za", "Name: Z → A"),
    ("gain_high", "Gain %: High → Low"),
    ("gain_low", "Gain %: Low → High"),
    ("xirr_high", "XIRR: High → Low"),
    ("xirr_low", "XIRR: Low → High"),
    ("recently_added", "Recently Added"),
    ("recently_updated", "Recently Updated"),
]
FLAG_OPTIONS = [
    ("stale", "Needs a refresh"),
    ("no_nominee", "Nominee gaps"),
    ("unconverted", "Not converted to USD"),
]


def search_holdings(user_id, q=None, asset_type=None, country=None, currency=None,
                    flag=None, sort="value_high", archived=False):
    """Filtered, sorted holdings for the list page. Always scoped to
    user_id. Returns (rows, facets): rows are dicts with the holding plus
    its P&L / staleness / nominee-gap, and facets are the distinct
    asset types, countries and currencies the user actually has (so the
    filter dropdowns only offer choices that can match something)."""
    base = InternationalHolding.query.filter_by(user_id=user_id, archived=archived).all()
    facets = {
        "asset_types": sorted({h.asset_type for h in base}),
        "countries": sorted({h.country for h in base if h.country}),
        "currencies": sorted({h.native_currency for h in base}),
    }

    needle = (q or "").strip().lower()
    rows = []
    for h in base:
        if asset_type and h.asset_type != asset_type:
            continue
        if country and h.country != country:
            continue
        if currency and h.native_currency != currency:
            continue
        if needle:
            haystack = " ".join(filter(None, [
                h.name, h.ticker, h.country, h.broker_or_institution,
                h.asset_type, h.native_currency, h.account_number_masked,
            ])).lower()
            if needle not in haystack:
                continue
        stale = get_staleness(h)
        gap = _nominee_gap(h)
        unconverted = h.fx_rate_used is None and h.native_currency != "USD"
        if flag == "stale" and stale["status"] == "fresh":
            continue
        if flag == "no_nominee" and gap is None:
            continue
        if flag == "unconverted" and not unconverted:
            continue
        rows.append({"holding": h, "pnl": holding_pnl(h), "stale": stale,
                     "nominee_gap": gap, "unconverted": unconverted})

    def gain_key(r):
        return r["pnl"]["gain_pct"]

    def none_last(keyfn, reverse):
        present = [r for r in rows if keyfn(r) is not None]
        absent = [r for r in rows if keyfn(r) is None]
        present.sort(key=keyfn, reverse=reverse)
        return present + absent

    if sort == "value_low":
        rows.sort(key=lambda r: r["holding"].usd_value or 0.0)
    elif sort == "name_az":
        rows.sort(key=lambda r: r["holding"].name.lower())
    elif sort == "name_za":
        rows.sort(key=lambda r: r["holding"].name.lower(), reverse=True)
    elif sort == "gain_high":
        rows = none_last(gain_key, True)
    elif sort == "gain_low":
        rows = none_last(gain_key, False)
    elif sort == "xirr_high":
        rows = none_last(lambda r: r["holding"].xirr, True)
    elif sort == "xirr_low":
        rows = none_last(lambda r: r["holding"].xirr, False)
    elif sort == "recently_added":
        rows.sort(key=lambda r: r["holding"].created_at or datetime.min, reverse=True)
    elif sort == "recently_updated":
        rows.sort(key=lambda r: r["holding"].updated_at or datetime.min, reverse=True)
    else:  # value_high (default)
        rows.sort(key=lambda r: r["holding"].usd_value or 0.0, reverse=True)
    return rows, facets


def get_asset_categories(user_id):
    """One entry per asset type (all of them, including empty ones, so the
    dashboard can offer '+ Add' for a type the user has none of yet):
    [{asset_type, count, usd_value}], in the module's own type order."""
    holdings = get_holdings(user_id, archived=False)
    out = []
    for t in InternationalAssetType.ALL:
        mine = [h for h in holdings if h.asset_type == t]
        out.append({"asset_type": t, "count": len(mine),
                    "usd_value": round(sum(h.usd_value or 0.0 for h in mine), 2)})
    return out


def get_recent_activity(user_id, limit=6):
    """Latest timeline entries across ALL of the user's holdings, newest
    first, each with its holding attached for linking."""
    rows = (InternationalTimeline.query.filter_by(user_id=user_id)
            .order_by(InternationalTimeline.created_at.desc(), InternationalTimeline.id.desc())
            .limit(limit).all())
    return [{"event": r, "holding": r.holding} for r in rows]


def get_document_count(user_id):
    return InternationalHoldingDocument.query.filter_by(user_id=user_id).count()


def get_alerts(user_id):
    """Things worth the user's attention, for the dashboard banner area,
    most urgent first. Each: {level: danger|warning|info, message, link:
    (endpoint, params)} — services don't build URLs, the route does."""
    alerts = []

    lrs = get_lrs_status(user_id)
    if lrs["status"] == "exceeded":
        alerts.append({"level": "danger",
                       "message": f"LRS limit reached for {lrs['fy_label']} — "
                                  f"${lrs['total_usd']:,.0f} logged against the ${lrs['limit_usd']:,.0f} cap.",
                       "link": ("international_centre.remittances", {})})
    elif lrs["status"] == "warning":
        alerts.append({"level": "warning",
                       "message": f"{lrs['pct_used']}% of your {lrs['fy_label']} LRS limit used — "
                                  f"${lrs['remaining_usd']:,.0f} remaining.",
                       "link": ("international_centre.remittances", {})})

    holdings = get_holdings(user_id, archived=False)

    unconverted = [h for h in holdings if h.fx_rate_used is None and h.native_currency != "USD"]
    if unconverted:
        n = len(unconverted)
        alerts.append({"level": "danger",
                       "message": f"{n} holding{'s' if n != 1 else ''} couldn't be converted to USD "
                                  f"(no exchange rate yet) — your totals are understated until a refresh succeeds.",
                       "link": ("international_centre.holdings_list", {"flag": "unconverted"})})

    stale_prices = [h for h in holdings if h.is_ticker_based and h.ticker
                    and get_staleness(h)["status"] != "fresh"]
    if stale_prices:
        n = len(stale_prices)
        alerts.append({"level": "warning",
                       "message": f"{n} holding{'s have' if n != 1 else ' has'} a price older than "
                                  f"{STALE_PRICE_DAYS} days (or never refreshed). Use Refresh Prices.",
                       "link": ("international_centre.holdings_list", {"flag": "stale"})})

    gaps = [h for h in holdings if _nominee_gap(h)]
    if gaps:
        n = len(gaps)
        alerts.append({"level": "info",
                       "message": f"{n} holding{'s have' if n != 1 else ' has'} no nominee or incomplete nominee shares.",
                       "link": ("international_centre.holdings_list", {"flag": "no_nominee"})})
    return alerts


# ── Dividend income (Batch 10.5, Oct 2026) ───────────────────────────

def _usd_rate(holding):
    """Cached native->USD rate for approximations, or None if unknown."""
    return holding.fx_rate_used or (1.0 if holding.native_currency == "USD" else None)


def get_dividend_income(user_id, fy_start_year, resolver=None):
    """Dividend income for the Indian FY starting 1 Apr `fy_start_year`.

    Same approximation path as get_dtaa_summary() (native -> USD via the
    holding's cached rate -> INR via currency_display.usd_to_inr) so the
    two pages never disagree. A dividend logged without gross/withholding
    (anything entered before Batch 9.5) has only its net amount, so its
    "gross" is shown as that net amount and counted in
    `withholding_unknown` rather than silently assuming no tax was taken.

    `ttm_yield_pct` = dividends received in the last 12 months, gross, over
    the holding's CURRENT value in its own currency — a trailing yield on
    today's value, not a yield on cost."""
    fy_start, fy_end = fy_bounds(_date(fy_start_year, 4, 1))
    resolver = resolver or rates.RateResolver(user_id)
    today = today_ist()
    ttm_start = today - timedelta(days=365)
    holdings = InternationalHolding.query.filter_by(user_id=user_id).all()

    def gross_of(t):
        return t.gross_amount_native if t.gross_amount_native is not None else t.amount_native

    rows, recent = [], []
    month_net_usd = {}
    withholding_unknown = 0
    ttm_gross_usd = 0.0
    active_value_usd = 0.0

    for h in holdings:
        divs = [t for t in h.transactions if t.txn_type == InternationalTxnType.DIVIDEND]
        rate = _usd_rate(h)
        if not h.archived:
            active_value_usd += h.usd_value or 0.0
            if rate is not None:
                ttm_gross_usd += sum(gross_of(t) for t in divs if ttm_start < t.date <= today) * rate
        for t in sorted(divs, key=lambda x: x.date, reverse=True)[:8]:
            recent.append({"holding": h, "date": t.date, "currency": h.native_currency,
                           "net_native": round(t.amount_native, 2), "gross_native": round(gross_of(t), 2),
                           "withheld_native": round(t.tax_withheld_native or 0.0, 2)})

        fy_divs = [t for t in divs if fy_start <= t.date <= fy_end]
        if not fy_divs:
            continue
        withholding_unknown += sum(1 for t in fy_divs if t.gross_amount_native is None)
        gross_native = sum(gross_of(t) for t in fy_divs)
        withheld_native = sum((t.tax_withheld_native or 0.0) for t in fy_divs)
        net_native = sum(t.amount_native for t in fy_divs)
        # Batch 11: rupee figures per dividend at the SBI rate for ITS date (see rates.py)
        resolver.prefetch([(h.native_currency, t.date, _PME, t.ttbr_override) for t in fy_divs])
        inr_res = [resolver.resolve(h.native_currency, t.date, _PME, override=t.ttbr_override,
                                    purpose="Dividend income", override_date=t.ttbr_override_date)
                   for t in fy_divs]
        if all(r.ok for r in inr_res):
            net_inr = round(sum(t.amount_native * r.rate for t, r in zip(fy_divs, inr_res)), 2)
            withheld_inr = round(sum((t.tax_withheld_native or 0.0) * r.rate for t, r in zip(fy_divs, inr_res)), 2)
        else:
            net_inr = withheld_inr = None
        if rate is None:
            net_usd = None
        else:
            net_usd = round(net_native * rate, 2)
            for t in fy_divs:
                key = (t.date.year, t.date.month)
                month_net_usd[key] = month_net_usd.get(key, 0.0) + t.amount_native * rate

        ttm_native = sum(gross_of(t) for t in divs if ttm_start < t.date <= today)
        ttm_yield = (round(ttm_native / h.current_value_native * 100, 2)
                     if ttm_native and h.current_value_native and h.current_value_native > 0 else None)
        rows.append({
            "holding": h, "holding_id": h.id, "name": h.name, "country": h.country or "—",
            "currency": h.native_currency, "payments": len(fy_divs),
            "gross_native": round(gross_native, 2), "withheld_native": round(withheld_native, 2),
            "net_native": round(net_native, 2), "net_usd": net_usd, "net_inr": net_inr,
            "withheld_inr": withheld_inr, "ttm_yield_pct": ttm_yield,
        })

    rows.sort(key=lambda r: (r["net_usd"] or 0.0), reverse=True)
    months = []
    for i in range(12):  # Apr .. Mar
        m = 4 + i
        y = fy_start_year + (1 if m > 12 else 0)
        m = m - 12 if m > 12 else m
        months.append({"label": _date(y, m, 1).strftime("%b %Y"), "net_usd": round(month_net_usd.get((y, m), 0.0), 2)})

    recent.sort(key=lambda r: r["date"], reverse=True)
    portfolio_yield = (round(ttm_gross_usd / active_value_usd * 100, 2)
                       if ttm_gross_usd and active_value_usd > 0 else None)
    return {
        "fy_label": fy_label(_date(fy_start_year, 4, 1)), "fy_start": fy_start, "fy_end": fy_end,
        "rows": rows, "months": months, "recent": recent[:8],
        "total_net_usd": round(sum(r["net_usd"] for r in rows if r["net_usd"] is not None), 2),
        "total_net_inr": round(sum(r["net_inr"] for r in rows if r["net_inr"] is not None), 2),
        "total_withheld_inr": round(sum(r["withheld_inr"] for r in rows if r["withheld_inr"] is not None), 2),
        "payments": sum(r["payments"] for r in rows),
        "any_missing_rate": any(r["net_usd"] is None for r in rows),
        "withholding_unknown": withholding_unknown,
        "portfolio_yield_pct": portfolio_yield, "ttm_gross_usd": round(ttm_gross_usd, 2),
    }


# ── INR-perspective returns (Batch 10.5, Oct 2026) ───────────────────
#
# A holding's XIRR in its own currency hides what rupee investors feel:
# a US stock up 10% in dollars is up more in rupees if the rupee weakened,
# and less if it strengthened. This converts EVERY cash flow at the
# exchange rate on its own date (and today's value at today's rate) and
# re-runs the same XIRR, so the difference between the two figures is the
# currency effect. Rates come from Frankfurter (ECB reference rates),
# which are not the SBI TT buying rate used for tax filing — this is an
# investment-performance view, not a tax figure.

import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor

_HIST_RATE_CACHE = {}      # (from, date) -> native->INR rate. Historical rates never change, so cached for the process lifetime.
_LATEST_RATE_CACHE = {}    # from -> (rate, monotonic_time). Today's rate moves, so it expires.
_LATEST_RATE_TTL = 3600
_FX_CACHE_LOCK = threading.Lock()


def clear_fx_caches():
    """Used by tests (and available if a stale rate is ever suspected)."""
    with _FX_CACHE_LOCK:
        _HIST_RATE_CACHE.clear()
        _LATEST_RATE_CACHE.clear()


def _inr_rates_for(currency, dates):
    """{date: native->INR rate} for each date, fetching only the ones not
    cached, a few at a time. Raises FxRateError if any can't be had."""
    if currency == "INR":
        return {d: 1.0 for d in dates}
    missing = [d for d in dates if (currency, d) not in _HIST_RATE_CACHE]
    if missing:
        def one(d):
            rate, _actual = fetch_fx_rate(currency, "INR", d)
            return d, rate
        with ThreadPoolExecutor(max_workers=min(6, len(missing))) as pool:
            for d, rate in pool.map(one, missing):   # pool.map re-raises FxRateError here
                with _FX_CACHE_LOCK:
                    _HIST_RATE_CACHE[(currency, d)] = rate
    return {d: _HIST_RATE_CACHE[(currency, d)] for d in dates}


def _latest_inr_rate(currency):
    if currency == "INR":
        return 1.0
    with _FX_CACHE_LOCK:
        hit = _LATEST_RATE_CACHE.get(currency)
        if hit and _time.monotonic() - hit[1] < _LATEST_RATE_TTL:
            return hit[0]
    rate, _actual = fetch_fx_rate(currency, "INR")
    with _FX_CACHE_LOCK:
        _LATEST_RATE_CACHE[currency] = (rate, _time.monotonic())
    return rate


_RETURN_TXN_TYPES = (InternationalTxnType.BUY, InternationalTxnType.SELL, InternationalTxnType.DIVIDEND)


def _inr_return_inputs(holding):
    """(txns, reason): the usable transactions, or (None, reason) when an
    INR return can't honestly be calculated for this holding."""
    if holding.asset_type == InternationalAssetType.FOREIGN_BANK_ACCOUNT:
        return None, "not_applicable"
    txns = [t for t in holding.transactions if t.txn_type in _RETURN_TXN_TYPES and t.date]
    if not any(t.txn_type == InternationalTxnType.BUY for t in txns):
        return None, "no_buys"
    return txns, None


def _inr_flows(holding, txns):
    """Per-transaction INR cash flows + today's INR value. Raises FxRateError."""
    dated_rates = _inr_rates_for(holding.native_currency, sorted({t.date for t in txns}))
    flows = [{"date": t.date, "txn_type": t.txn_type, "amount_native": t.amount_native * dated_rates[t.date]} for t in txns]
    current_inr = (holding.current_value_native or 0.0) * _latest_inr_rate(holding.native_currency)
    return flows, current_inr


def _summarise_inr(flows, current_inr):
    buys = sum(f["amount_native"] for f in flows if f["txn_type"] == InternationalTxnType.BUY)
    sells = sum(f["amount_native"] for f in flows if f["txn_type"] == InternationalTxnType.SELL)
    divs = sum(f["amount_native"] for f in flows if f["txn_type"] == InternationalTxnType.DIVIDEND)
    invested = buys - sells
    gain = current_inr - invested
    return {
        "invested_inr": round(invested, 2), "current_inr": round(current_inr, 2),
        "gain_inr": round(gain, 2), "gain_pct": round(gain / invested * 100, 2) if invested > 0 else None,
        "dividends_inr": round(divs, 2), "xirr_inr": holding_xirr(flows, current_inr),
    }


_REASONS = {
    "no_buys": "Add this holding's buy transactions (with dates) to see an INR-perspective return - without them there is no purchase-date exchange rate to use.",
    "not_applicable": "Not shown for a foreign bank account.",
}


def get_inr_return(holding):
    """INR-perspective return for one holding. Never guesses: if an
    exchange rate can't be fetched, returns available=False with the
    reason instead of a partial number."""
    txns, reason = _inr_return_inputs(holding)
    if reason:
        return {"available": False, "reason": reason, "message": _REASONS[reason]}
    try:
        flows, current_inr = _inr_flows(holding, txns)
    except FxRateError as e:
        return {"available": False, "reason": "fx", "message": f"Couldn't fetch exchange rates just now ({e}). Try again shortly."}
    out = _summarise_inr(flows, current_inr)
    out["available"] = True
    out["xirr_native"] = holding.xirr
    out["currency_effect_pts"] = (round(out["xirr_inr"] - holding.xirr, 2)
                                  if out["xirr_inr"] is not None and holding.xirr is not None else None)
    out["currency"] = holding.native_currency
    return out


def get_portfolio_inr_return(user_id):
    """Portfolio-wide INR XIRR across every active holding with a usable
    history. Holdings whose rates can't be fetched, or that have no
    dated buys, are left out and counted in `excluded` — leaving a
    holding out is honest, quietly estimating it is not."""
    all_flows, total_current, included, excluded = [], 0.0, 0, []
    for h in get_holdings(user_id, archived=False):
        txns, reason = _inr_return_inputs(h)
        if reason:
            if reason == "no_buys":
                excluded.append(h.name)
            continue
        try:
            flows, current_inr = _inr_flows(h, txns)
        except FxRateError:
            excluded.append(h.name)
            continue
        all_flows.extend(flows)
        total_current += current_inr
        included += 1
    if not included:
        return {"available": False, "excluded": excluded,
                "message": "No holding has enough dated transaction history for an INR-perspective return yet."}
    out = _summarise_inr(all_flows, total_current)
    out.update({"available": True, "included": included, "excluded": excluded})
    return out


# ── Reminders (Batch 10.6, Oct 2026) ─────────────────────────────────
#
# Reminders are computed from today's date and the user's data every time
# they're asked for — nothing is stored except "I've dealt with this one"
# (InternationalReminderAck). Due dates quoted are the NORMAL statutory
# ones; the government extends them from time to time, so every message
# says to check the notified date.

US_SITUS_ASSET_TYPES = {InternationalAssetType.US_STOCK, InternationalAssetType.US_ETF,
                        InternationalAssetType.RSU_ESPP, InternationalAssetType.INTL_MUTUAL_FUND,
                        InternationalAssetType.FOREIGN_REAL_ESTATE}
_US_COUNTRY_NAMES = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}
US_ESTATE_EXEMPTION_USD = 60_000
US_ESTATE_NEAR_PCT = 0.8


def valid_ack_key(key):
    import re
    return bool(re.fullmatch(r"(schedule_fa:\d{4}|form67:\d{4}|estate:(near|over))", key or ""))


def _acked_keys(user_id):
    return {a.reminder_key for a in InternationalReminderAck.query.filter_by(user_id=user_id).all()}


def acknowledge_reminder(user_id, key):
    db = _db()
    if not valid_ack_key(key):
        return False
    if not InternationalReminderAck.query.filter_by(user_id=user_id, reminder_key=key).first():
        db.session.add(InternationalReminderAck(user_id=user_id, reminder_key=key))
        db.session.commit()
    return True


def unacknowledge_reminder(user_id, key):
    db = _db()
    ack = InternationalReminderAck.query.filter_by(user_id=user_id, reminder_key=key).first()
    if ack:
        db.session.delete(ack)
        db.session.commit()
    return True


def get_us_situs_exposure(user_id):
    """Estimate of US-situs holdings for the estate-tax awareness note.

    Counts active holdings of the asset types that can be US-situs
    (US/International stock, ETF, employer stock, mutual fund, real estate)
    whose country is the United States — or, for a USD-quoted listed
    holding with NO country filled in, assumed US and flagged as
    `assumed`. Foreign bank accounts and bonds are deliberately NOT
    counted (US-bank deposits and portfolio-interest debt of non-US
    persons are generally outside the US estate tax, and bond situs rules
    are too intricate to guess at). Non-US-listed holdings (a UK or Irish
    fund) are not counted. This is an awareness figure, not a tax
    calculation."""
    counted, assumed = [], []
    for h in get_holdings(user_id, archived=False):
        if h.asset_type not in US_SITUS_ASSET_TYPES:
            continue
        country = (h.country or "").strip().lower()
        if country in _US_COUNTRY_NAMES:
            counted.append(h)
        elif not country and h.native_currency == "USD" and h.is_ticker_based:
            counted.append(h)
            assumed.append(h)
    total = round(sum(h.usd_value or 0.0 for h in counted), 2)
    return {"total_usd": total, "holdings": counted, "assumed": assumed,
            "exemption_usd": US_ESTATE_EXEMPTION_USD}


def get_reminders(user_id, today=None):
    """All reminders for the dashboard / Reminders page, most urgent first.

    Each: {key, ack_key|None, kind, level (danger|warning|info), title,
    message, link: (endpoint, params)|None, done (bool), detail (longer
    text for the Reminders page)}."""
    today = today or today_ist()
    acked = _acked_keys(user_id)
    items = []

    holdings_all = InternationalHolding.query.filter_by(user_id=user_id).all()

    # 1. Schedule FA — the last TWO completed calendar years, each disclosed in
    #    its own year's ITR. The older year is kept (and escalates to "danger"
    #    once both usual ITR dates have passed) until the user marks it done, so
    #    a missed filing can't just silently scroll off the list on 1 January.
    def held_by(year_end):
        """Was anything held on or before `year_end`? Judged from when the
        holding was recorded AND from the dates of its transactions/vesting,
        so a holding bought in 2023 but only added to the app in 2026 still
        counts for 2025."""
        for h in holdings_all:
            if h.created_at and h.created_at.date() <= year_end:
                return True
            if any(t.date <= year_end for t in h.transactions):
                return True
            if any(v.vest_date <= year_end for v in h.vesting_tranches):
                return True
        return False

    for cy in (today.year - 1, today.year - 2):
        if not held_by(_date(cy, 12, 31)):
            continue
        due, belated = _date(cy + 1, 7, 31), _date(cy + 1, 12, 31)
        if today <= due:
            days = (due - today).days
            level = "warning" if days <= 45 else "info"
            msg = (f"Schedule FA for calendar year {cy} goes in your ITR, normally due {due:%d %b %Y} - "
                   f"{days} day{'s' if days != 1 else ''} left. Review the report and share it with your CA.")
        elif today <= belated:
            days = (belated - today).days
            level = "warning"
            msg = (f"The normal ITR due date ({due:%d %b %Y}) has passed. If you haven't filed, a belated return "
                   f"is generally possible until {belated:%d %b %Y} ({days} days left), and it must include Schedule FA "
                   f"for calendar year {cy}.")
        else:
            level = "danger"
            msg = (f"Both the normal ({due:%d %b %Y}) and belated ({belated:%d %b %Y}) ITR dates for calendar year {cy} "
                   f"have passed. If Schedule FA wasn't disclosed, speak to your CA about your options. "
                   f"If it was, mark this done.")
        key = f"schedule_fa:{cy}"
        items.append({"key": key, "ack_key": key, "kind": "schedule_fa", "level": level,
                      "title": f"Schedule FA - calendar year {cy}", "message": msg,
                      "detail": "Applies to Indian residents holding foreign assets. Due dates are the usual ones - "
                                "check the notified date for the year, as extensions happen. Mark it done once you have filed.",
                      "link": ("international_centre.schedule_fa", {"year": cy}), "done": key in acked})

    # 2. Foreign tax credit (Form 67 / Form 44) — latest completed FYs with tax withheld.
    last_fy = fy_bounds(today)[0].year - 1
    for fy in (last_fy, last_fy - 1):
        # No network here: this runs on every dashboard load, so it must never wait on an exchange-rate
        # service. It only needs to know whether foreign tax was withheld (native amounts), and shows the
        # rupee figure only when the user's own SBI rate book can produce it.
        summary = get_dtaa_summary(user_id, fy, resolver=rates.RateResolver(user_id, allow_estimate=False))
        if not any(r["withheld_native"] for r in summary["rows"]):
            continue
        amount_text = (f"about Rs. {summary['total_withheld_inr']:,.0f}" if summary["total_withheld_inr"]
                       else "foreign tax (enter your SBI rates to see the rupee amount)")
        outer = _date(fy + 2, 3, 31)               # end of the assessment year
        if fy != last_fy and today > outer:
            continue                                # an older year whose window is long gone: not a reminder any more
        form = "Form 67" if fy <= 2025 else "Form 44"
        itr_due = _date(fy + 1, 7, 31)
        if today > outer:
            level, tail = "danger", "The usual outer limit has passed - speak to your CA about whether the credit can still be claimed."
        elif today > itr_due:
            level, tail = "warning", f"File it BEFORE your return. The usual outer limit is the end of the assessment year ({outer:%d %b %Y}); some sources quote an earlier date, so confirm with your CA."
        else:
            level, tail = "info", f"File it BEFORE your return - the usual outer limit is the end of the assessment year ({outer:%d %b %Y})."
        key = f"form67:{fy}"
        items.append({"key": key, "ack_key": key, "kind": "form67", "level": level,
                      "title": f"{form} - foreign tax credit, {summary['fy_label']}",
                      "message": f"You had {amount_text} withheld on dividends in {summary['fy_label']}. "
                                 f"Claim credit with {form}. {tail}",
                      "detail": ("From tax year 2026-27 the form is renumbered Form 44 under the Income-tax Act, 2025. "
                                 "INR is approximate - the filing needs the SBI TT buying rate on each date." if fy >= 2025 else
                                 "INR is approximate - the filing needs the SBI TT buying rate on each date."),
                      "link": ("international_centre.dtaa_summary", {"fy": fy}), "done": key in acked})

    # 3. Manually-valued holdings whose value is getting old.
    stale = []
    for h in holdings_all:
        if h.archived or (h.is_ticker_based and h.ticker):
            continue
        st = get_staleness(h)
        if st["status"] != "fresh":
            stale.append((h, st))
    stale.sort(key=lambda x: (x[1]["age_days"] is None, -(x[1]["age_days"] or 0)))
    for h, st in stale[:5]:
        age = st["age_days"]
        items.append({"key": f"stale:{h.id}", "ack_key": None, "kind": "stale_value",
                      "level": "warning" if (age or 0) > 180 else "info",
                      "title": f"Update the value of {h.name}",
                      "message": f"Manually valued - last updated {age} days ago. A stale value skews your totals and Schedule FA figures." if age is not None
                                 else "Manually valued and never updated.",
                      "detail": None, "link": ("international_centre.edit_holding", {"holding_id": h.id}), "done": False})
    if len(stale) > 5:
        items.append({"key": "stale:more", "ack_key": None, "kind": "stale_value", "level": "info",
                      "title": f"{len(stale) - 5} more manually-valued holdings need updating",
                      "message": "See them all on the Holdings page.", "detail": None,
                      "link": ("international_centre.holdings_list", {"flag": "stale"}), "done": False})

    # 4. US estate-tax awareness (information, not a calculation).
    exposure = get_us_situs_exposure(user_id)
    total = exposure["total_usd"]
    if total >= US_ESTATE_EXEMPTION_USD * US_ESTATE_NEAR_PCT:
        over = total >= US_ESTATE_EXEMPTION_USD
        tier = "over" if over else "near"
        key = f"estate:{tier}"
        assumed_note = (f" ({len(exposure['assumed'])} holding(s) with no country set were assumed to be US-listed.)"
                        if exposure["assumed"] else "")
        items.append({"key": key, "ack_key": key, "kind": "estate", "level": "warning" if over else "info",
                      "title": "US estate tax - awareness note",
                      "message": (f"Your US-listed holdings are worth about ${total:,.0f}, "
                                  + ("above" if over else "approaching") + f" the ${US_ESTATE_EXEMPTION_USD:,} exemption that "
                                  "non-US persons get. Above it, US estate tax can apply to what your heirs inherit." + assumed_note),
                      "detail": ("India has no estate duty, but the US taxes US-situs assets (US-listed shares and ETFs, even "
                                 "if held through a broker for an Indian resident) of non-US persons above a fixed $60,000 exemption, at rates "
                                 "that can reach 40%, and there is no India-US estate-tax treaty. The estate may need to file Form 706-NA. "
                                 "Non-US-domiciled funds are generally treated differently. This is awareness only - not tax advice and not "
                                 "a calculation of what would be owed: speak to a cross-border tax professional about your situation."),
                      "link": ("international_centre.holdings_list", {"country": "United States"}), "done": key in acked})

    order = {"danger": 0, "warning": 1, "info": 2}
    items.sort(key=lambda i: (i["done"], order[i["level"]]))
    return items


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


def _parse_ttbr_override(data):
    """(rate or None, date or None) from the optional override form fields.
    Already validated by validators._validate_ttbr_override."""
    raw = (data.get("ttbr_override") or "").strip()
    if not raw:
        return None, None
    d_raw = (data.get("ttbr_override_date") or "").strip()
    return float(raw), (datetime.strptime(d_raw, "%Y-%m-%d").date() if d_raw else None)


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
    txn.ttbr_override, txn.ttbr_override_date = _parse_ttbr_override(data)
    db.session.add(txn)
    db.session.flush()
    recompute_holding_financials(holding)
    log_timeline(holding, TimelineEvent.TRANSACTION_ADDED,
                 f"{txn_type} on {txn.date:%d %b %Y} — {_money(holding.native_currency, amount_native)}")
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
    txn.ttbr_override, txn.ttbr_override_date = _parse_ttbr_override(data)
    recompute_holding_financials(txn.holding)
    log_timeline(txn.holding, TimelineEvent.TRANSACTION_EDITED,
                 f"{txn_type} on {txn.date:%d %b %Y} edited — now {_money(txn.holding.native_currency, amount_native)}")
    db.session.commit()
    return txn


def delete_transaction(txn):
    db = _db()
    holding = txn.holding
    summary = (f"{txn.txn_type} on {txn.date:%d %b %Y} — "
               f"{_money(holding.native_currency, txn.amount_native)} deleted")
    db.session.delete(txn)
    db.session.flush()
    recompute_holding_financials(holding)
    log_timeline(holding, TimelineEvent.TRANSACTION_DELETED, summary)
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
    tranche.ttbr_override, tranche.ttbr_override_date = _parse_ttbr_override(data)
    db.session.add(tranche)
    log_timeline(holding, TimelineEvent.VESTING_ADDED,
                 f"{tranche.plan_type} tranche added — {tranche.quantity:g} units vesting "
                 f"{tranche.vest_date:%d %b %Y} at FMV {_money(holding.native_currency, tranche.fmv_native)}")
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
    tranche.ttbr_override, tranche.ttbr_override_date = _parse_ttbr_override(data)
    log_timeline(tranche.holding, TimelineEvent.VESTING_EDITED,
                 f"{tranche.plan_type} tranche edited — {tranche.quantity:g} units, vest {tranche.vest_date:%d %b %Y}")
    db.session.commit()
    return tranche


def delete_vesting_tranche(tranche):
    db = _db()
    log_timeline(tranche.holding, TimelineEvent.VESTING_DELETED,
                 f"{tranche.plan_type} tranche deleted — {tranche.quantity:g} units, vest {tranche.vest_date:%d %b %Y}")
    db.session.delete(tranche)
    db.session.commit()


# ── Remittances (LRS) ───────────────────────────────────────────────

def _remit_category(remittance):
    """Which TCS rule category a remittance falls into."""
    return tcs_rules.category_for(
        purpose_is_education=(remittance.purpose == RemittancePurpose.EDUCATION),
        purpose_is_medical=(remittance.purpose == RemittancePurpose.MEDICAL),
        education_loan_funded=bool(remittance.education_loan_funded),
    )


def recompute_fy_tcs(user_id, any_date_in_fy):
    """Recompute tcs_amount_inr for EVERY remittance in the Indian
    financial year containing `any_date_in_fy`. Does NOT commit.

    Batch 10.1 (Oct 2026): replaces Batch 9.4's lock-in-at-save
    _compute_tcs(). Rates and the threshold now depend on the
    remittance date and purpose (see tcs_rules.py), and the figure for
    one remittance depends on what came before it in the year — so after
    any add or delete the whole year is recomputed in date order. The
    stored numbers therefore always match what is currently logged,
    whatever order entries were typed in."""
    fy_start, fy_end = fy_bounds(any_date_in_fy)
    rows = (RemittanceRecord.query
            .filter_by(user_id=user_id)
            .filter(RemittanceRecord.date >= fy_start, RemittanceRecord.date <= fy_end)
            .all())
    entries = [{"key": r.id, "date": r.date, "amount_inr": r.amount_inr,
                "category": _remit_category(r)} for r in rows]
    result = tcs_rules.compute_fy_tcs(entries)
    for r in rows:
        r.tcs_amount_inr = result[r.id]
    return len(rows)


def recompute_all_tcs(user_id=None):
    """Recompute TCS for every financial year that has remittances, for
    one user or everyone. Commits. Used by `flask international
    recompute-tcs` and safe to re-run any time (idempotent). Returns the
    number of remittance rows processed."""
    db = _db()
    query = RemittanceRecord.query
    if user_id is not None:
        query = query.filter_by(user_id=user_id)
    seen = set()
    total = 0
    for r in query.all():
        key = (r.user_id, fy_bounds(r.date)[0])
        if key in seen:
            continue
        seen.add(key)
        total += recompute_fy_tcs(r.user_id, r.date)
    db.session.commit()
    return total


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

    purpose = data["purpose"].strip()
    loan_raw = str(data.get("education_loan_funded") or "").strip().lower()
    education_loan_funded = (purpose == RemittancePurpose.EDUCATION
                             and loan_raw in ("on", "1", "true", "yes"))

    remittance = RemittanceRecord(
        user_id=user_id,
        holding_id=int(data["holding_id"]) if (data.get("holding_id") or "").strip() else None,
        date=remit_date,
        amount_inr=amount_inr,
        amount_usd=amount_usd,
        fx_rate_used=rate,
        purpose=purpose,
        education_loan_funded=education_loan_funded,
        remitting_bank=(data.get("remitting_bank") or "").strip() or None,
        notes=(data.get("notes") or "").strip() or None,
        tcs_amount_inr=0.0,
    )
    db.session.add(remittance)
    db.session.flush()
    recompute_fy_tcs(user_id, remit_date)
    db.session.commit()
    return remittance


def delete_remittance(remittance):
    db = _db()
    user_id, remit_date = remittance.user_id, remittance.date
    db.session.delete(remittance)
    db.session.flush()
    recompute_fy_tcs(user_id, remit_date)
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
    total_inr = round(sum(r.amount_inr for r in remittances), 2)
    threshold_inr = tcs_rules.threshold_for(anchor_date)  # Batch 10.1 — None before TCS on LRS existed
    tcs_free_remaining_inr = (max(0.0, threshold_inr - total_inr) if threshold_inr else None)

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
        "total_inr": total_inr, "tcs_threshold_inr": threshold_inr,
        "tcs_free_remaining_inr": tcs_free_remaining_inr,
        "tcs_rule": tcs_rules.describe_rules(anchor_date),
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

# ── Reports on the SBI rate engine (Batch 11, Oct 2026) ───────────────
#
# Every INR figure below goes through rates.RateResolver, which answers
# "which SBI TT buying rate, from where?" for each event (override > rate
# book > ECB estimate > missing). Each row carries the rate it used and a
# label for its source, so the page and the exports can show their working.
# USD figures further down are unchanged: they remain the module's
# tracking anchor and are NOT tax figures.

_PME = RateBasis.PREV_MONTH_END


def get_vesting_perquisite_summary(user_id, fy_start_year, resolver=None):
    """RSU/ESPP vesting tranches and their taxable PERQUISITE value
    -- (FMV_at_vest - price_paid) * quantity -- which Indian tax law
    treats as SALARY income in the FY the shares actually vested (a
    completely separate, earlier event from any capital gain realized
    when those shares are later sold -- see classify_capital_gains(),
    which uses this same FMV as the eventual cost basis). Scoped to the
    Indian FY starting 1 Apr `fy_start_year`.

    Batch 11: each tranche's INR value now uses the SBI TT buying rate on
    the last day of the month before the vest month (the Rule 115 salary
    convention), taken from a rate typed on the tranche, then the user's
    rate book, then an ECB estimate (labelled as such). The figure your
    employer reports on Form 12BA / Form 16 is what counts for filing;
    this is a cross-check, not a replacement."""
    fy_start, fy_end = fy_bounds(_date(fy_start_year, 4, 1))
    resolver = resolver or rates.RateResolver(user_id)
    holdings = (InternationalHolding.query
                .filter_by(user_id=user_id, asset_type=InternationalAssetType.RSU_ESPP)
                .all())
    pairs = [(h, v) for h in holdings for v in h.vesting_tranches if fy_start <= v.vest_date <= fy_end]
    resolver.prefetch([(h.native_currency, v.vest_date, _PME, v.ttbr_override) for h, v in pairs])

    rows, results = [], []
    for h, v in pairs:
        inr, res = resolver.convert(v.perquisite_value_native, h.native_currency, v.vest_date, _PME,
                                    override=v.ttbr_override, purpose="RSU/ESPP perquisite",
                                    override_date=v.ttbr_override_date)
        results.append(res)
        rows.append({
            "holding": h, "tranche": v, "currency": h.native_currency,
            "plan_type": v.plan_type, "vest_date": v.vest_date, "quantity": v.quantity,
            "fmv_native": v.fmv_native, "purchase_price_native": v.purchase_price_native or 0.0,
            "perquisite_native": v.perquisite_value_native, "perquisite_inr": inr,
            "rate": res.rate, "rate_source": res.source, "rate_label": res.label(),
            "rate_official": res.official, "rate_target_date": res.target_date,
        })

    rows.sort(key=lambda r: r["vest_date"])
    total_perquisite_inr = sum(r["perquisite_inr"] for r in rows if r["perquisite_inr"] is not None)
    counts = rates.summarize_results(results)
    return {
        "fy_label": fy_label(_date(fy_start_year, 4, 1)), "fy_start": fy_start, "fy_end": fy_end,
        "rows": rows, "total_perquisite_inr": round(total_perquisite_inr, 2),
        "any_missing_rate": any(r["perquisite_inr"] is None for r in rows),
        "rates": counts, "rates_badge": rates.basis_badge(counts),
        "all_official": bool(rows) and counts["official"] == counts["total"],
    }


def get_dtaa_summary(user_id, fy_start_year, resolver=None):
    """Per-holding gross dividend / tax withheld / net received for the
    Indian FY starting 1 Apr `fy_start_year`, in native currency and in
    INR.

    Batch 11: the INR figures are now built dividend by dividend, each at
    the SBI TT buying rate for ITS OWN date (last day of the previous
    month, the Rule 115 convention for income), from a rate typed on that
    dividend, else the user's rate book, else a labelled ECB estimate. A
    holding's INR total is left blank rather than part-summed if any one
    of its dividends has no rate. `events` carries the per-dividend
    working so the page can show it."""
    fy_start, fy_end = fy_bounds(_date(fy_start_year, 4, 1))
    resolver = resolver or rates.RateResolver(user_id)
    holdings = InternationalHolding.query.filter_by(user_id=user_id).all()

    plan = []
    for h in holdings:
        divs = sorted([t for t in h.transactions
                       if t.txn_type == InternationalTxnType.DIVIDEND and fy_start <= t.date <= fy_end],
                      key=lambda t: (t.date, t.id))
        if divs:
            plan.append((h, divs))
    resolver.prefetch([(h.native_currency, d.date, _PME, d.ttbr_override) for h, divs in plan for d in divs])

    rows, results = [], []
    for h, divs in plan:
        events, row_results = [], []
        for d in divs:
            gross_n = d.gross_amount_native if d.gross_amount_native is not None else d.amount_native
            withheld_n = d.tax_withheld_native or 0.0
            res = resolver.resolve(h.native_currency, d.date, _PME, override=d.ttbr_override,
                                   purpose="Dividends / Form 67", override_date=d.ttbr_override_date)
            row_results.append(res)
            events.append({
                "date": d.date, "gross_native": round(gross_n, 2), "withheld_native": round(withheld_n, 2),
                "net_native": round(d.amount_native, 2), "rate": res.rate, "rate_label": res.label(),
                "rate_source": res.source,
                "gross_inr": round(gross_n * res.rate, 2) if res.ok else None,
                "withheld_inr": round(withheld_n * res.rate, 2) if res.ok else None,
                "net_inr": round(d.amount_native * res.rate, 2) if res.ok else None,
            })
        results.extend(row_results)
        complete = all(e["gross_inr"] is not None for e in events)
        counts = rates.summarize_results(row_results)
        rows.append({
            "holding": h, "country": h.country or "—", "currency": h.native_currency,
            "gross_native": round(sum(e["gross_native"] for e in events), 2),
            "withheld_native": round(sum(e["withheld_native"] for e in events), 2),
            "net_native": round(sum(e["net_native"] for e in events), 2),
            "gross_inr": round(sum(e["gross_inr"] for e in events), 2) if complete else None,
            "withheld_inr": round(sum(e["withheld_inr"] for e in events), 2) if complete else None,
            "net_inr": round(sum(e["net_inr"] for e in events), 2) if complete else None,
            "events": events, "rates": counts, "rates_badge": rates.basis_badge(counts),
        })

    total_gross_inr = sum(r["gross_inr"] for r in rows if r["gross_inr"] is not None)
    total_withheld_inr = sum(r["withheld_inr"] for r in rows if r["withheld_inr"] is not None)
    counts = rates.summarize_results(results)
    return {
        "fy_label": fy_label(_date(fy_start_year, 4, 1)), "fy_start": fy_start, "fy_end": fy_end,
        "rows": rows,
        "total_gross_inr": round(total_gross_inr, 2), "total_withheld_inr": round(total_withheld_inr, 2),
        "any_missing_rate": any(r["gross_inr"] is None for r in rows),
        "rates": counts, "rates_badge": rates.basis_badge(counts),
        "all_official": bool(rows) and counts["official"] == counts["total"],
    }


def get_capital_gains_summary(user_id, fy_start_year, resolver=None):
    """LTCG/STCG-classified realized gains across every ticker-based
    holding, for SELLs falling in the Indian FY starting 1 Apr
    `fy_start_year` (capital gains tax follows the year the asset was
    SOLD, not a calendar year).

    Batch 11: rupee figures. Per the user's `cg_method` setting the cost
    and the sale proceeds are converted SEPARATELY, each at the SBI TT
    buying rate for its own date (last day of the previous month - the
    Rule 115 convention), or the gain is worked out in the foreign
    currency and converted ONCE at the sale date's rate. Sources disagree
    on which is right, which is why it is a setting and a question for
    the CA. A row's INR figures are blank (never guessed) when a needed
    rate is missing. `gain_usd` is kept as before: an approximation at the
    holding's current cached rate, for tracking only."""
    fy_start, fy_end = fy_bounds(_date(fy_start_year, 4, 1))
    settings = rates.get_settings(user_id)
    single = settings.cg_method == CgMethod.SINGLE
    resolver = resolver or rates.RateResolver(user_id)
    holdings = InternationalHolding.query.filter_by(user_id=user_id).all()

    per_holding = []
    for h in holdings:
        gains = [g for g in classify_capital_gains(h) if fy_start <= g["sell_date"] <= fy_end]
        if gains:
            per_holding.append((h, gains))

    prefetch = []
    for h, gains in per_holding:
        for g in gains:
            prefetch.append((h.native_currency, g["sell_date"], _PME, g["sell_ref"].ttbr_override))
            if not single:
                prefetch.append((h.native_currency, g["acquisition_date"], _PME, g["acq_ref"].ttbr_override))
    resolver.prefetch(prefetch)

    rows, results = [], []
    any_missing_rate = False
    for h, gains in per_holding:
        usd_rate = h.fx_rate_used or (1.0 if h.native_currency == "USD" else None)
        for g in gains:
            g["holding"] = h
            g["currency"] = h.native_currency
            if usd_rate is None:
                any_missing_rate = True
                g["gain_usd"] = None
            else:
                g["gain_usd"] = round(g["gain_native"] * usd_rate, 2)

            sell_res = resolver.resolve(h.native_currency, g["sell_date"], _PME,
                                        override=g["sell_ref"].ttbr_override, purpose="Capital gains",
                                        override_date=g["sell_ref"].ttbr_override_date)
            results.append(sell_res)
            acq_res = None
            if single:
                if sell_res.ok:
                    g["cost_inr"] = round(g["cost_basis_native"] * sell_res.rate, 2)
                    g["proceeds_inr"] = round(g["proceeds_native"] * sell_res.rate, 2)
                    g["gain_inr"] = round(g["gain_native"] * sell_res.rate, 2)
                else:
                    g["cost_inr"] = g["proceeds_inr"] = g["gain_inr"] = None
            else:
                acq_res = resolver.resolve(h.native_currency, g["acquisition_date"], _PME,
                                           override=g["acq_ref"].ttbr_override, purpose="Capital gains",
                                           override_date=g["acq_ref"].ttbr_override_date)
                results.append(acq_res)
                if sell_res.ok and acq_res.ok:
                    g["cost_inr"] = round(g["cost_basis_native"] * acq_res.rate, 2)
                    g["proceeds_inr"] = round(g["proceeds_native"] * sell_res.rate, 2)
                    g["gain_inr"] = round(g["proceeds_inr"] - g["cost_inr"], 2)
                else:
                    g["cost_inr"] = g["proceeds_inr"] = g["gain_inr"] = None
            g["sell_rate"], g["sell_rate_label"], g["sell_rate_source"] = sell_res.rate, sell_res.label(), sell_res.source
            g["acq_rate"] = acq_res.rate if acq_res else None
            g["acq_rate_label"] = acq_res.label() if acq_res else "Not used (single-rate method)"
            g["acq_rate_source"] = acq_res.source if acq_res else None
            g["rates_ok"] = g["gain_inr"] is not None
            rows.append(g)

    rows.sort(key=lambda g: g["sell_date"], reverse=True)

    def total(cls, key):
        return round(sum(g[key] for g in rows if g["classification"] == cls and g[key] is not None), 2)

    counts = rates.summarize_results(results)
    return {
        "fy_label": fy_label(_date(fy_start_year, 4, 1)), "fy_start": fy_start, "fy_end": fy_end,
        "rows": rows,
        "ltcg_total_usd": total("LTCG", "gain_usd"), "stcg_total_usd": total("STCG", "gain_usd"),
        "ltcg_total_inr": total("LTCG", "gain_inr"), "stcg_total_inr": total("STCG", "gain_inr"),
        "any_missing_rate": any_missing_rate,
        "any_missing_inr": any(g["gain_inr"] is None for g in rows),
        "cg_method": settings.cg_method, "cg_method_label": rates.METHOD_LABELS[settings.cg_method],
        "rates": counts, "rates_badge": rates.basis_badge(counts),
        "all_official": bool(rows) and counts["official"] == counts["total"],
    }


# ── Schedule FA in rupees (Batch 11) ─────────────────────────────────

# What "nature of entity" to suggest when the user hasn't filled it in.
_DEFAULT_ENTITY_NATURE = {
    InternationalAssetType.US_STOCK: "Listed company",
    InternationalAssetType.US_ETF: "Exchange-traded fund",
    InternationalAssetType.INTL_MUTUAL_FUND: "Mutual fund",
    InternationalAssetType.RSU_ESPP: "Listed company (employer)",
    InternationalAssetType.FOREIGN_BANK_ACCOUNT: "Bank",
    InternationalAssetType.FOREIGN_REAL_ESTATE: "Immovable property",
    InternationalAssetType.FOREIGN_BOND: "Bond issuer",
}


def get_schedule_fa_year_input(holding, calendar_year):
    return ScheduleFaYearInput.query.filter_by(holding_id=holding.id, calendar_year=calendar_year).first()


def save_schedule_fa_year_input(holding, calendar_year, data):
    """Upsert the optional per-year figures. Returns an error string or None.
    An all-blank form deletes the row (back to snapshot-derived figures)."""
    db = _db()

    def num(key, label):
        raw = (data.get(key) or "").strip()
        if not raw:
            return None, None
        try:
            val = float(raw)
        except ValueError:
            return None, f"Please enter a valid {label}."
        if val < 0:
            return None, f"The {label} cannot be negative."
        return val, None

    peak_value, err = num("peak_value_native", "peak value")
    if err:
        return err
    closing_value, err = num("closing_value_native", "closing value")
    if err:
        return err
    peak_raw = (data.get("peak_date") or "").strip()
    peak_date = None
    if peak_raw:
        try:
            peak_date = datetime.strptime(peak_raw, "%Y-%m-%d").date()
        except ValueError:
            return "Please enter a valid peak date."
        y_start, y_end = calendar_year_bounds(calendar_year)
        if not (y_start <= peak_date <= y_end):
            return f"The peak date must fall within calendar year {calendar_year}."
    if (peak_value is None) != (peak_date is None):
        return "Enter the peak value and the peak date together, or leave both blank."
    notes = (data.get("notes") or "").strip()[:200] or None

    row = get_schedule_fa_year_input(holding, calendar_year)
    if peak_value is None and closing_value is None and not notes:
        if row:
            db.session.delete(row)
            db.session.commit()
        return None
    if not row:
        row = ScheduleFaYearInput(user_id=holding.user_id, holding_id=holding.id, calendar_year=calendar_year)
        db.session.add(row)
    row.peak_value_native, row.peak_date = peak_value, peak_date
    row.closing_value_native, row.notes = closing_value, notes
    db.session.commit()
    return None


def _fa_lots_held_in_year(h, year_start, year_end):
    """Acquisition lots the holding held at ANY point in the calendar year,
    as [{'date', 'cost_native', 'ref'}] - what Schedule FA's "initial value
    of investment" is built from. For ticker-based holdings this is the lots
    still open on 1 January (partly-sold lots count only their remaining
    quantity, FIFO) plus everything acquired during the year, even if sold
    again within it. Other holdings use their dated BUY amounts."""
    if not h.is_ticker_based:
        return [{"date": t.date, "cost_native": t.amount_native, "ref": t}
                for t in h.transactions if t.txn_type == InternationalTxnType.BUY and t.date <= year_end]

    events = []
    for t in h.transactions:
        if t.txn_type == InternationalTxnType.BUY and t.quantity and t.quantity > 0:
            events.append((t.date, 0, t.id, "BUY", t))
        elif t.txn_type == InternationalTxnType.SELL and t.quantity and t.quantity > 0:
            events.append((t.date, 1, t.id, "SELL", t))
    for v in h.vesting_tranches:
        if v.quantity and v.quantity > 0:
            events.append((v.vest_date, 0, v.id, "VEST", v))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    lots = deque()
    held = []
    for ev_date, _rank, _id, kind, obj in events:
        if ev_date > year_end:
            break
        if kind in ("BUY", "VEST"):
            unit = obj.amount_native / obj.quantity if kind == "BUY" else obj.fmv_native
            lot = {"date": ev_date, "qty": obj.quantity, "unit_cost": unit, "ref": obj}
            if ev_date < year_start:
                lots.append(lot)
            else:
                held.append({"date": ev_date, "cost_native": round(obj.quantity * unit, 2), "ref": obj})
        elif kind == "SELL" and ev_date < year_start:
            remaining = obj.quantity
            while remaining > 1e-9 and lots:
                take = min(remaining, lots[0]["qty"])
                lots[0]["qty"] -= take
                remaining -= take
                if lots[0]["qty"] <= 1e-9:
                    lots.popleft()
    carried = [{"date": l["date"], "cost_native": round(l["qty"] * l["unit_cost"], 2), "ref": l["ref"]} for l in lots]
    return sorted(carried + held, key=lambda x: x["date"])


def get_schedule_fa_summary(user_id, calendar_year, resolver=None):
    """Per-holding Schedule FA (Table A3-style) figures for the given
    CALENDAR year (Schedule FA's own reporting period).

    The USD columns (opening/peak/closing/proceeds) are the module's
    tracking figures from the daily snapshots, unchanged and NOT tax
    figures. Batch 11 adds the rupee columns the form actually asks for:
      initial value ... cost of every lot held in the year, each at the SBI
                         TT buying rate for its acquisition date
      peak value ...... on the peak date's rate
      closing value ... on the 31 December rate (always that date itself)
      gross paid ...... dividends/interest credited in the year, each on its date
      gross proceeds .. sales in the year, each on its date
    "Its date" follows the user's `fa_basis` setting (the day itself, per
    the form's instructions, or the last day of the previous month, per
    Rule 115). Peak and closing come from the per-year inputs the user
    typed (broker statement figures in the holding's own currency) when
    present, else from the daily USD snapshots - converted back to the
    holding's currency, an approximation flagged on the row for non-USD
    holdings. Any figure whose rate is missing is blank, never guessed."""
    year_start, year_end = calendar_year_bounds(calendar_year)
    settings = rates.get_settings(user_id)
    basis = settings.fa_basis
    resolver = resolver or rates.RateResolver(user_id)
    holdings = get_holdings(user_id, archived=False)
    inputs = {i.holding_id: i for i in ScheduleFaYearInput.query.filter_by(
        user_id=user_id, calendar_year=calendar_year).all()}

    # Pass 1: gather everything each row needs, and every rate it will ask for.
    plans, prefetch = [], []
    for h in holdings:
        snapshots = (InternationalValueSnapshot.query
                     .filter_by(holding_id=h.id)
                     .filter(InternationalValueSnapshot.date >= year_start,
                             InternationalValueSnapshot.date <= year_end)
                     .order_by(InternationalValueSnapshot.date).all())
        if snapshots:
            opening, closing = snapshots[0].usd_value, snapshots[-1].usd_value
            peak_snap = max(snapshots, key=lambda s: s.usd_value)
            peak, peak_snap_date = peak_snap.usd_value, peak_snap.date
            data_complete = (snapshots[0].date <= year_start + timedelta(days=10) and
                             snapshots[-1].date >= year_end - timedelta(days=10))
        else:
            opening = closing = peak = h.usd_value or 0.0
            peak_snap_date = None
            data_complete = False

        sells = sorted([t for t in h.transactions
                        if t.txn_type == InternationalTxnType.SELL and year_start <= t.date <= year_end],
                       key=lambda t: (t.date, t.id))
        divs = sorted([t for t in h.transactions
                       if t.txn_type == InternationalTxnType.DIVIDEND and year_start <= t.date <= year_end],
                      key=lambda t: (t.date, t.id))
        lots = _fa_lots_held_in_year(h, year_start, year_end)
        inp = inputs.get(h.id)
        peak_date = inp.peak_date if (inp and inp.peak_date) else (peak_snap_date or year_end)
        cur = h.native_currency
        for lot in lots:
            prefetch.append((cur, lot["date"], basis, lot["ref"].ttbr_override))
        for t in sells + divs:
            prefetch.append((cur, t.date, basis, t.ttbr_override))
        prefetch.append((cur, peak_date, basis, None))
        prefetch.append((cur, year_end, RateBasis.SAME_DAY, None))
        plans.append(dict(h=h, opening=opening, closing=closing, peak=peak, data_complete=data_complete,
                          peak_snap_date=peak_snap_date, sells=sells, divs=divs, lots=lots, inp=inp,
                          peak_date=peak_date, have_snapshots=bool(snapshots)))
    resolver.prefetch(prefetch)

    # Pass 2: convert and assemble.
    rows, all_results = [], []
    for pl in plans:
        h, inp, cur = pl["h"], pl["inp"], pl["h"].native_currency
        row_results = []

        def conv(amount, when, b, override=None, odate=None):
            inr, res = resolver.convert(amount, cur, when, b, override=override, purpose="Schedule FA",
                                        override_date=odate)
            row_results.append(res)
            return inr

        def to_native(usd):
            """Snapshot USD -> holding currency (exact for USD holdings)."""
            if cur == "USD":
                return usd, False
            if h.fx_rate_used:
                return usd / h.fx_rate_used, True
            return None, True

        approx_native = False
        # closing
        if inp and inp.closing_value_native is not None:
            closing_native, closing_src = inp.closing_value_native, "your input"
        else:
            closing_native, approx = to_native(pl["closing"])
            approx_native |= approx
            closing_src = "daily snapshots" if pl["have_snapshots"] else "current value (no snapshots)"
        # peak
        if inp and inp.peak_value_native is not None:
            peak_native, peak_src = inp.peak_value_native, "your input"
        else:
            peak_native, approx = to_native(pl["peak"])
            approx_native |= approx
            peak_src = "daily snapshots" if pl["have_snapshots"] else "current value (no snapshots)"

        closing_inr = conv(closing_native, year_end, RateBasis.SAME_DAY) if closing_native is not None else None
        peak_inr = conv(peak_native, pl["peak_date"], basis) if peak_native is not None else None

        lots = pl["lots"]
        initial_inr = None
        if lots:
            parts = [conv(l["cost_native"], l["date"], basis, l["ref"].ttbr_override, l["ref"].ttbr_override_date)
                     for l in lots]
            initial_inr = round(sum(parts), 2) if all(p is not None for p in parts) else None
        acq_dates = [l["date"] for l in lots]
        acquisition_date = min(acq_dates) if acq_dates else None

        paid_parts = [conv(t.gross_amount_native if t.gross_amount_native is not None else t.amount_native,
                           t.date, basis, t.ttbr_override, t.ttbr_override_date) for t in pl["divs"]]
        gross_paid_inr = (round(sum(paid_parts), 2) if all(p is not None for p in paid_parts) else None) if paid_parts else 0.0
        proceeds_parts = [conv(t.amount_native, t.date, basis, t.ttbr_override, t.ttbr_override_date)
                          for t in pl["sells"]]
        proceeds_inr = (round(sum(proceeds_parts), 2) if all(p is not None for p in proceeds_parts) else None) if proceeds_parts else 0.0

        # legacy USD tracking figure for proceeds (kept as before)
        gross_proceeds_usd = 0.0
        if pl["sells"]:
            usd_rate = h.fx_rate_used or 1.0
            gross_proceeds_usd = round(sum(t.amount_native for t in pl["sells"]) * usd_rate, 2)

        # what is still missing before this row could be copied into the form
        nature = (h.entity_nature or "").strip()
        missing = []
        if not (h.entity_address or "").strip():
            missing.append("address")
        if not (h.entity_zip or "").strip():
            missing.append("ZIP code")
        if not nature:
            missing.append("nature of entity (using a suggested default)")
        if h.asset_type not in (InternationalAssetType.FOREIGN_BANK_ACCOUNT, InternationalAssetType.FOREIGN_REAL_ESTATE):
            if not lots:
                missing.append("dated buy/vest transactions (needed for initial value and acquisition date)")
        if not (inp and inp.peak_date) and not pl["peak_snap_date"]:
            missing.append("peak date")
        counts = rates.summarize_results(row_results)
        all_results.extend(row_results)

        rows.append({
            "holding": h, "country": h.country or "—", "asset_type": h.asset_type,
            "entity_name": h.name, "entity_address": h.entity_address or "", "entity_zip": h.entity_zip or "",
            "entity_nature": nature or _DEFAULT_ENTITY_NATURE.get(h.asset_type, "—"),
            "acquisition_date": acquisition_date or (h.created_at.date() if h.created_at else None),
            "acquisition_from_lots": bool(acq_dates), "lot_count": len(lots),
            "opening_usd": round(pl["opening"] or 0, 2), "peak_usd": round(pl["peak"] or 0, 2),
            "closing_usd": round(pl["closing"] or 0, 2), "gross_proceeds_usd": gross_proceeds_usd,
            "data_complete": pl["data_complete"],
            "initial_inr": initial_inr, "peak_inr": peak_inr, "closing_inr": closing_inr,
            "peak_date": pl["peak_date"], "peak_source": peak_src, "closing_source": closing_src,
            "gross_paid_inr": gross_paid_inr, "proceeds_inr": proceeds_inr,
            "approx_native": approx_native, "has_input": inp is not None,
            "missing": missing, "rates": counts, "rates_badge": rates.basis_badge(counts),
        })

    def tot(key):
        return round(sum(r[key] for r in rows if r[key] is not None), 2)

    counts = rates.summarize_results(all_results)
    return {
        "calendar_year": calendar_year, "rows": rows,
        "any_incomplete": any(not r["data_complete"] for r in rows),
        "total_initial_inr": tot("initial_inr"), "total_peak_inr": tot("peak_inr"),
        "total_closing_inr": tot("closing_inr"), "total_gross_paid_inr": tot("gross_paid_inr"),
        "total_proceeds_inr": tot("proceeds_inr"),
        "any_missing_inr": any(r[k] is None for r in rows for k in ("peak_inr", "closing_inr")),
        "fa_basis": basis, "fa_basis_label": rates.BASIS_LABELS[basis],
        "rates": counts, "rates_badge": rates.basis_badge(counts),
        "all_official": bool(rows) and counts["official"] == counts["total"] and counts["total"] > 0,
    }


def get_needed_rates(user_id, fy_start_year):
    """The SBI rates the user has NOT yet entered (and that no typed
    override covers) for one tax year: every rate the Schedule FA (the
    calendar year ending in this FY), capital gains, dividend/Form 67 and
    RSU perquisite reports would ask for. Runs the real report functions in
    'collect' mode (no network), so it can never disagree with them."""
    resolver = rates.RateResolver(user_id, allow_estimate=False)
    get_schedule_fa_summary(user_id, fy_start_year, resolver=resolver)
    get_capital_gains_summary(user_id, fy_start_year, resolver=resolver)
    get_dtaa_summary(user_id, fy_start_year, resolver=resolver)
    get_vesting_perquisite_summary(user_id, fy_start_year, resolver=resolver)
    return resolver.needed_list()


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
    log_timeline(holding, TimelineEvent.DOCUMENT_UPLOADED,
                 f"Document uploaded — {original_name} ({doc_type})")
    db.session.commit()
    return doc


def delete_document(db, doc, user_id):
    """Remove document metadata. Caller must delete the actual file
    first (see utils.delete_document_file) — matches every other
    module's Document Vault delete_document()."""
    if doc.user_id != user_id:
        return False, "You do not have permission to delete this document."
    holding = doc.holding
    log_timeline(holding, TimelineEvent.DOCUMENT_DELETED, f"Document deleted — {doc.original_name}")
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
                lots.append({"date": obj.date, "qty": obj.quantity, "unit_cost": obj.amount_native / obj.quantity, "ref": obj})
        elif kind == "VEST":
            if obj.quantity and obj.quantity > 0:
                lots.append({"date": obj.vest_date, "qty": obj.quantity, "unit_cost": obj.fmv_native, "ref": obj})
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
                    # Batch 11: the source events, so each can carry its own SBI rate override
                    "acq_ref": lot["ref"], "sell_ref": t,
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
