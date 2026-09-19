"""
Goal Glide Path, Rebalancing & Post-Retirement Drawdown
===========================================================
Three related pieces of goal-planning math that all build on the same
underlying simulation, kept in one module since they share that
simulation and their own combined docstring here:

1. glide_path_curve() — a year-by-year table from now to the goal's
   target year: target equity/debt allocation %, that year's monthly
   SIP (after step-up), and the projected corpus at that year's end.
   The equity target glides LINEARLY from goal.glide_start_equity_pct
   (now) to goal.glide_end_equity_pct (at the target year) — a simple,
   transparent rule (no "smart" curve-fitting) matching how most goal
   planners present this: aggressive early, conservative as the goal
   nears, editable per goal via those two fields.

2. rebalance_suggestion() — compares a goal's LINKED holdings' actual
   current equity/debt split (via each GoalHoldingLink.asset_class)
   against the glide path's target for the CURRENT year (curve[0]),
   and returns a one-line "move ₹X equity -> debt" (or the reverse)
   suggestion, or None if there's nothing meaningful to rebalance
   (no linked holdings, or already within a small tolerance band).

3. drawdown_curve() — for is_retirement_goal goals only: simulates
   monthly withdrawals from the retirement corpus from retirement_age
   to life_expectancy, expenses inflating each year, remaining corpus
   still growing at post_retirement_return_pct. Returns yearly
   samples for a depletion chart, plus whether/when the corpus runs
   out before life_expectancy.

All three take the already-loaded Goal object (and, where needed, the
linked-holdings rows from app.py's _goal_linked_value()) rather than
querying the DB themselves — kept as pure functions, easy to unit-test
without a live app/db context, same pattern as cas_xirr.py.
"""

from datetime import datetime as _dt


def _current_year():
    return _dt.now().year


def glide_path_curve(goal, linked_value=0.0):
    """
    Returns a list of dicts, one per year from the current year to
    goal.target_year inclusive:
      {year, equity_pct, debt_pct, monthly_sip, projected_corpus}

    projected_corpus is the running balance at THAT year's end,
    simulated month by month (current_savings + linked_value as the
    starting balance, goal.monthly_sip stepped up by goal.step_up_pct
    once every 12 months, growth at goal.annual_return/12 per month)
    — the same math as _stepup_sip_future_value()/calculate_goal() in
    app.py, just sampled at each year-end instead of only at the end,
    and starting from the ACTUAL linked value rather than the manually
    typed current_savings alone (matching how the Goals page already
    treats "effective current savings" — see goals() in app.py).
    """
    current_year = _current_year()
    years = max(goal.target_year - current_year, 0)
    r = ((goal.annual_return or 0) / 100) / 12
    step_up = (goal.step_up_pct or 0) / 100
    equity_start = goal.glide_start_equity_pct if goal.glide_start_equity_pct is not None else 75.0
    equity_end   = goal.glide_end_equity_pct   if goal.glide_end_equity_pct   is not None else 30.0

    balance = (goal.current_savings or 0) + (linked_value or 0)
    sip = goal.monthly_sip or 0

    curve = []
    # Year 0 = now, before any months have elapsed — this row is what
    # rebalance_suggestion() compares actual holdings against.
    curve.append({
        "year": current_year,
        "equity_pct": round(equity_start, 1),
        "debt_pct": round(100 - equity_start, 1),
        "monthly_sip": round(sip, 2),
        "projected_corpus": round(balance, 2),
    })

    if years == 0:
        return curve

    for y in range(1, years + 1):
        for _m in range(12):
            balance = balance * (1 + r) + sip
        sip *= (1 + step_up)  # step up AFTER a full year, matching _stepup_sip_future_value()
        progress = y / years
        equity_pct = equity_start + (equity_end - equity_start) * progress
        curve.append({
            "year": current_year + y,
            "equity_pct": round(equity_pct, 1),
            "debt_pct": round(100 - equity_pct, 1),
            "monthly_sip": round(sip, 2),
            "projected_corpus": round(balance, 2),
        })

    return curve


def rebalance_suggestion(goal, linked_rows, tolerance_pct=3.0):
    """
    linked_rows: the list from app.py's _goal_linked_value() — dicts
    of {'link': GoalHoldingLink, 'holding': the actual holding row}.

    Buckets each linked holding's allocated value (holding.value *
    allocation_pct/100, or .current_value/.value depending on holding
    type — see _holding_value() in app.py, already applied by the
    caller) by its link's asset_class, compares the resulting
    equity:debt split against the glide path's CURRENT-year target,
    and returns either None (nothing to do) or a dict:
      {direction: 'equity_to_debt' | 'debt_to_equity', amount, message}

    'other'-classified holdings (real estate, gold, vehicles, ...) are
    excluded from the equity/debt split entirely — the glide path only
    concerns the equity/debt mix, matching how the reference product
    (NaviPlan) shows Equity/Debt/Hybrid separately rather than forcing
    every asset into a two-way split.
    """
    equity_value = sum(
        row["allocated_value"] for row in linked_rows
        if row["link"].asset_class == "equity"
    )
    debt_value = sum(
        row["allocated_value"] for row in linked_rows
        if row["link"].asset_class == "debt"
    )
    total = equity_value + debt_value
    if total <= 0:
        return None

    curve = glide_path_curve(goal, linked_value=0.0)  # only the allocation %, not the corpus figure
    target_equity_pct = curve[0]["equity_pct"] if curve else 100.0

    current_equity_pct = (equity_value / total) * 100
    diff_pct = target_equity_pct - current_equity_pct
    if abs(diff_pct) < tolerance_pct:
        return None

    amount = abs(diff_pct) / 100 * total
    if diff_pct > 0:
        return {
            "direction": "debt_to_equity",
            "amount": round(amount, 2),
            "message": f"Move ₹{amount:,.0f} debt → equity to reach the {target_equity_pct:.0f}% equity target for this year.",
        }
    else:
        return {
            "direction": "equity_to_debt",
            "amount": round(amount, 2),
            "message": f"Move ₹{amount:,.0f} equity → debt to reach the {target_equity_pct:.0f}% equity target for this year.",
        }


def drawdown_curve(goal, corpus_at_retirement):
    """
    Only meaningful when goal.is_retirement_goal. Simulates monthly
    withdrawals from corpus_at_retirement (typically the projected
    corpus at the goal's target year, i.e. curve[-1]['projected_corpus']
    from glide_path_curve()) starting at goal.retirement_age, expenses
    inflating once a year at goal.expense_inflation_pct, remaining
    corpus still growing at goal.post_retirement_return_pct.

    Returns {rows: [{age, year, corpus}], depleted_at_age: int|None}
    — rows are yearly (end-of-year) samples for a chart; depleted_at_age
    is set the first year the corpus hits zero before life_expectancy,
    or None if it lasts the full horizon.

    Returns None if the goal doesn't have enough retirement-specific
    fields filled in to run the simulation (retirement_age and
    monthly_expense_today are both required; everything else has a
    sane default already on the Goal model).
    """
    if not goal.is_retirement_goal:
        return None
    if not goal.retirement_age or not goal.monthly_expense_today:
        return None

    life_expectancy = goal.life_expectancy or 85
    years = max(life_expectancy - goal.retirement_age, 0)
    if years == 0:
        return {"rows": [], "depleted_at_age": None}

    monthly_return = ((goal.post_retirement_return_pct or 0) / 100) / 12
    inflation = (goal.expense_inflation_pct or 0) / 100

    corpus = float(corpus_at_retirement or 0)
    monthly_expense = goal.monthly_expense_today
    current_year = _current_year()
    retirement_year = current_year + max(goal.target_year - current_year, 0)

    rows = []
    depleted_at_age = None
    for y in range(years):
        age = goal.retirement_age + y
        for _m in range(12):
            if corpus <= 0:
                corpus = 0
                break
            corpus = corpus * (1 + monthly_return) - monthly_expense
        if corpus < 0:
            corpus = 0
        rows.append({
            "age": age + 1,
            "year": retirement_year + y + 1,
            "corpus": round(corpus, 2),
        })
        if corpus <= 0 and depleted_at_age is None:
            depleted_at_age = age + 1
        monthly_expense *= (1 + inflation)

    return {"rows": rows, "depleted_at_age": depleted_at_age}
