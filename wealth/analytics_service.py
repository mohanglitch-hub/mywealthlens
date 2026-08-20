"""
Wealth — Value Analytics Service (Phase K)
==============================================
Read-only analytics layer over Phase J's WealthValueSnapshot data.
Calculates change/CAGR/category metrics directly from existing
history + current entities — no new data model, no caching, no
mutation of historical records.

Terminology discipline (Section 7/12/62/63/83/84 of the spec):
  - Assets: "value increased/decreased", "appreciation/depreciation"
  - Liabilities: "balance increased/reduced" — NEVER "return" or "loss"
  - Category aggregates: "your holdings", never bare market language
    ("Gold increased 18%" implies market data; this only ever
    reflects the user's own recorded valuations)

CAGR minimum duration (Section 15): 1 year. Below that threshold,
CAGR is not calculated at all — annualizing a short window can
produce a wildly misleading number (e.g. a 1% gain over one week
naively annualized is enormous). This threshold is a deliberate,
documented choice, not a discovered application convention (none
existed before this phase).
"""

from datetime import datetime

from .models import WealthValueSnapshot, WealthAsset, WealthLiability
from . import value_history_service as vhs

CAGR_MIN_YEARS = 1.0

# ── Internal: shared calculation core ─────────────────────────────────────────

def _elapsed_years(first_dt, last_dt):
    """
    Actual elapsed time in years, using real timestamps rather than
    counting history rows or subtracting calendar years (Section 14/94
    — "Jan 2023 -> Aug 2026" must resolve to ~3.64 years, not a naive
    row count or year(end)-year(start)).
    """
    delta = last_dt - first_dt
    return delta.total_seconds() / (365.25 * 24 * 3600)


def _percentage_change(initial_value, current_value):
    """
    None (-> displayed as "N/A") when the initial value is zero or
    the calculation would otherwise be undefined (Section 10/11) —
    never a fabricated ∞% or similar.
    """
    if not initial_value:
        return None
    return (current_value - initial_value) / initial_value * 100.0


def _cagr(initial_value, current_value, years):
    """
    None when: initial value is zero/invalid, elapsed duration is
    zero, or the period is shorter than CAGR_MIN_YEARS (Section 13/15
    — CAGR must not be shown at all for short holdings, not just
    hidden behind a caveat).
    """
    if not initial_value or initial_value <= 0:
        return None
    if years is None or years < CAGR_MIN_YEARS:
        return None
    try:
        return ((current_value / initial_value) ** (1.0 / years) - 1.0) * 100.0
    except (ValueError, ZeroDivisionError):
        # e.g. current_value negative with a fractional exponent —
        # not meaningful for a financial valuation; report as
        # unavailable rather than raise or fabricate a number.
        return None


def _entity_analytics(user_id, entity_type, entity_id, current_value):
    """
    Shared core for both asset and liability analytics — the two
    public functions below differ only in field names/terminology on
    the way out, not in how the numbers are computed (Section
    18/19/20 apply identically to both entity types).
    """
    history = vhs.get_value_history(user_id, entity_type, entity_id)
    # get_value_history returns newest-first; we need oldest for
    # "initial" and to walk chronologically for the chart later.
    history_oldest_first = list(reversed(history))

    if not history_oldest_first:
        return {
            "has_history": False,
            "initial_value": None, "initial_date": None,
            "current_value": current_value, "latest_date": None,
            "absolute_change": None, "percentage_change": None,
            "cagr": None, "years_held": None,
            "consistency_warning": None,
            "chart_points": [],
        }

    first = history_oldest_first[0]
    latest = history_oldest_first[-1]

    initial_value = first.value
    initial_date = first.snapshot_date
    latest_history_value = latest.value
    latest_date = latest.snapshot_date

    absolute_change = current_value - initial_value
    percentage_change = _percentage_change(initial_value, current_value)

    years_held = _elapsed_years(
        datetime.combine(first.created_at.date(), first.created_at.time()),
        datetime.combine(latest.created_at.date(), latest.created_at.time()),
    ) if len(history_oldest_first) > 1 else 0.0
    cagr = _cagr(initial_value, current_value, years_held)

    # Section 21: verify (don't assume) the latest history record
    # matches the current entity value. In normal operation these
    # always match — Phase J's atomic create/update wiring guarantees
    # it — so a mismatch here would indicate a genuine data
    # inconsistency (e.g. manual DB edit bypassing the app). Report
    # it; never silently "fix" historical data to match.
    consistency_warning = None
    if vhs.values_differ(latest_history_value, current_value):
        consistency_warning = (
            f"Latest recorded history value ({latest_history_value}) does not "
            f"match the current entity value ({current_value}). This may "
            f"indicate the entity was changed outside the normal update path."
        )

    chart_points = [
        {"date": h.snapshot_date.isoformat(), "value": h.value}
        for h in history_oldest_first
    ]

    return {
        "has_history": True,
        "initial_value": initial_value, "initial_date": initial_date,
        "current_value": current_value, "latest_date": latest_date,
        "absolute_change": absolute_change, "percentage_change": percentage_change,
        "cagr": cagr, "years_held": years_held,
        "consistency_warning": consistency_warning,
        "chart_points": chart_points,
    }


# ── Public: per-entity analytics ──────────────────────────────────────────────

def asset_value_analytics(user_id, asset):
    """
    Value-performance analytics for one WealthAsset (Section 6/26).
    `asset` must already be confirmed to belong to user_id by the
    caller (the route already does this for the detail page — this
    function doesn't re-fetch the entity, only its history, so it
    can't itself be used to bypass that check).
    """
    return _entity_analytics(user_id, vhs.ENTITY_ASSET, asset.id, asset.current_value)


def liability_value_analytics(user_id, liability):
    """
    Balance-performance analytics for one WealthLiability (Section
    7/27). Same shape as asset analytics, but callers must use
    liability-appropriate labels ("Balance Change", never "Return")
    — the terminology discipline lives in the template layer, since
    the underlying arithmetic is identical (Section 62/63).
    """
    return _entity_analytics(
        user_id, vhs.ENTITY_LIABILITY, liability.id, liability.outstanding_amount)


# ── Public: category-level summaries ──────────────────────────────────────────

def category_summary(user_id, entity_type, category, entities):
    """
    Aggregate "your holdings" change for one category (Section
    28/29/30). Deliberately the SIMPLER of the two approaches the
    spec allows: sums each active entity's OWN first-recorded value
    against its own current value, rather than attempting to build a
    single synchronized category-wide timeline.

    Why not a synchronized timeline (documented per Section 30): if
    Asset A has history from Jan 2024 and Asset B was added in Jun
    2026, there is no honest common "initial" date for the category
    as a whole without either excluding B or fabricating a value for
    B before it existed — the spec explicitly forbids interpolation
    (Section 25/43). Summing each entity's own first-vs-current
    change avoids this while still answering "how has this category
    changed overall" honestly. This does mean a category's
    percentage change is influenced by how long each entity has been
    tracked, not a single clean time window — documented here and in
    the final report as a known limitation.

    Only entities with at least one history record contribute to the
    initial-value sum (an entity with no history yet has no
    meaningful "initial" figure to include).

    `entities` must already be filtered to this user + this category
    + active/non-archived by the caller (Section 60 — archived-asset
    inclusion is a decision for the caller's query, not this
    function).
    """
    total_initial = 0.0
    total_current = 0.0
    contributing_count = 0

    for e in entities:
        current_value = (e.current_value if entity_type == vhs.ENTITY_ASSET
                         else e.outstanding_amount)
        latest = vhs.latest_value_record(user_id, entity_type, e.id)
        first_record = (vhs.get_value_history(user_id, entity_type, e.id) or [None])[-1]
        if first_record is None:
            continue
        total_initial += first_record.value
        total_current += current_value
        contributing_count += 1

    if contributing_count == 0:
        return {
            "category": category, "has_data": False,
            "entity_count": len(entities), "contributing_count": 0,
            "total_initial": None, "total_current": None,
            "absolute_change": None, "percentage_change": None,
        }

    absolute_change = total_current - total_initial
    percentage_change = _percentage_change(total_initial, total_current)

    return {
        "category": category, "has_data": True,
        "entity_count": len(entities), "contributing_count": contributing_count,
        "total_initial": total_initial, "total_current": total_current,
        "absolute_change": absolute_change, "percentage_change": percentage_change,
    }


def all_category_summaries(user_id, entity_type, categories, entities_by_category):
    """
    category_summary() for every category that has at least one
    active entity. Categories with zero entities are skipped
    entirely (Section 59 — no meaningless empty-category analytics),
    not shown with a "0% / N/A" row.
    """
    summaries = []
    for cat in categories:
        entities = entities_by_category.get(cat, [])
        if not entities:
            continue
        summaries.append(category_summary(user_id, entity_type, cat, entities))
    return summaries


# ── Public: top gainers / decliners / liability reductions ────────────────────

def top_asset_movers(user_id, assets, limit=5):
    """
    Returns (top_gainers, top_decliners) — each a list of
    {asset, absolute_change, percentage_change} dicts, sorted by
    absolute value change (Section 34/35). Assets with no history or
    zero change are excluded from both lists — Section 35 explicitly
    says not to force a decliners section when there are none, and
    the same logic applies to gainers.
    """
    movers = []
    for a in assets:
        analytics = asset_value_analytics(user_id, a)
        if not analytics["has_history"] or analytics["absolute_change"] is None:
            continue
        if abs(analytics["absolute_change"]) < 0.005:
            continue
        movers.append({
            "entity": a,
            "absolute_change": analytics["absolute_change"],
            "percentage_change": analytics["percentage_change"],
        })

    gainers = sorted([m for m in movers if m["absolute_change"] > 0],
                     key=lambda m: m["absolute_change"], reverse=True)[:limit]
    decliners = sorted([m for m in movers if m["absolute_change"] < 0],
                       key=lambda m: m["absolute_change"])[:limit]
    return gainers, decliners


def dashboard_summary(user_id, assets):
    """
    Compact summary for the Wealth dashboard card (Section 39 — "do
    not overcrowd the dashboard", concise counts only, full detail
    lives on the dedicated /wealth/analytics page). Returns None
    (hidden entirely) when there's nothing meaningful to show yet,
    matching the same pattern as history_service.dashboard_trend_summary()
    and document_service.dashboard_summary() (both hide their card
    below a minimum data threshold rather than showing an empty one).
    """
    gainers, decliners = top_asset_movers(user_id, assets, limit=1000)
    if not gainers and not decliners:
        return None
    return {
        "up_count": len(gainers), "down_count": len(decliners),
        "top_gainer": gainers[0] if gainers else None,
        "top_decliner": decliners[0] if decliners else None,
    }


def liability_reductions(user_id, liabilities, limit=10):
    """
    Liabilities whose balance has genuinely decreased (Section 36) —
    the reduction amount, not framed as "performance". Only includes
    liabilities with real history and a real decrease.
    """
    reductions = []
    for l in liabilities:
        analytics = liability_value_analytics(user_id, l)
        if not analytics["has_history"] or analytics["absolute_change"] is None:
            continue
        if analytics["absolute_change"] >= -0.005:
            continue  # not a reduction
        reductions.append({
            "entity": l,
            "reduction": -analytics["absolute_change"],
            "percentage_change": analytics["percentage_change"],
        })
    reductions.sort(key=lambda r: r["reduction"], reverse=True)
    return reductions[:limit]
