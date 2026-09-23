"""
Cashflow Centre — Services
==============================
Routes never touch models directly — all reads/writes go through
here, matching the convention used across every other module.
Every query is filtered by user_id (IDOR check).
"""
from datetime import datetime

from cashflow_centre.models import Transaction, Budget, TransactionType
from cashflow_centre.utils import month_bounds, adjacent_month_key, parse_month_key
from cashflow_centre.validators import validate_transaction, validate_budget


def _parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def _commit(db):
    """
    Commit the current transaction, rolling back cleanly on any DB
    exception so a failed write never leaves a partial row behind.
    Returns an error string on failure, None on success.
    """
    try:
        db.session.commit()
        return None
    except Exception:
        db.session.rollback()
        return "Something went wrong saving your changes. Please try again."


# ── Transactions ──────────────────────────────────────────────────────────────

def create_transaction(db, user_id, data):
    """Returns (transaction, error). On error, nothing is written."""
    errors = validate_transaction(data)
    if errors:
        return None, errors[0]

    txn = Transaction(
        user_id        = user_id,
        date           = _parse_date(data["date"].strip()),
        type           = data["type"].strip(),
        category       = data["category"].strip(),
        amount         = float(data["amount"]),
        payment_method = (data.get("payment_method") or "").strip() or None,
        description    = (data.get("description") or "").strip() or None,
    )
    db.session.add(txn)
    error = _commit(db)
    if error:
        return None, error
    return txn, None


def update_transaction(db, txn, user_id, data):
    if txn.user_id != user_id:
        return None, "You do not have permission to edit this transaction."

    errors = validate_transaction(data)
    if errors:
        return None, errors[0]

    txn.date           = _parse_date(data["date"].strip())
    txn.type           = data["type"].strip()
    txn.category       = data["category"].strip()
    txn.amount         = float(data["amount"])
    txn.payment_method = (data.get("payment_method") or "").strip() or None
    txn.description    = (data.get("description") or "").strip() or None
    error = _commit(db)
    if error:
        return None, error
    return txn, None


def delete_transaction(db, txn, user_id):
    if txn.user_id != user_id:
        return False, "You do not have permission to delete this transaction."
    db.session.delete(txn)
    error = _commit(db)
    if error:
        return False, error
    return True, None


def get_transactions(user_id, year=None, month=None, category=None, txn_type=None,
                      from_date=None, to_date=None):
    """
    Transactions for a user, optionally filtered by an explicit date
    range (from_date/to_date, both given — takes priority) or by a
    single month (year+month, both given), plus category and/or type.
    Ordered newest-first.
    """
    query = Transaction.query.filter_by(user_id=user_id)
    if from_date and to_date:
        query = query.filter(Transaction.date >= from_date, Transaction.date <= to_date)
    elif year and month:
        first_day, last_day = month_bounds(year, month)
        query = query.filter(Transaction.date >= first_day, Transaction.date <= last_day)
    if category:
        query = query.filter_by(category=category)
    if txn_type:
        query = query.filter_by(type=txn_type)
    return query.order_by(Transaction.date.desc(), Transaction.id.desc()).all()


def bulk_create_transactions(db, user_id, rows):
    """
    Insert several transactions from CSV import in one commit.
    `rows` is a list of dicts with date/type/category/amount/
    payment_method/description already in the same string-keyed shape
    create_transaction() expects — every row is still run through
    validate_transaction() (via create_transaction's own checks) so
    imported data can never bypass the rules manual entry enforces.
    Returns (created_count, errors) where errors is a list of
    {row_num, message} for rows that failed validation; nothing is
    written to the database unless the whole batch validates —
    partial imports would leave the user guessing what actually landed.
    """
    validated = []
    errors = []
    for row in rows:
        row_errors = validate_transaction(row)
        if row_errors:
            errors.append({"row_num": row.get("row_num"), "message": row_errors[0]})
        else:
            validated.append(row)

    if errors:
        return 0, errors

    for row in validated:
        db.session.add(Transaction(
            user_id        = user_id,
            date           = _parse_date(row["date"].strip()),
            type           = row["type"].strip(),
            category       = row["category"].strip(),
            amount         = float(row["amount"]),
            payment_method = (row.get("payment_method") or "").strip() or None,
            description    = (row.get("description") or "").strip() or None,
        ))
    error = _commit(db)
    if error:
        return 0, [{"row_num": None, "message": error}]
    return len(validated), []


def get_quick_add_defaults(user_id):
    """
    Smart defaults for the dashboard's Quick Add modal, derived purely
    from the user's own transaction history (no extra storage needed):
      - last_payment_method: payment method on their most recent
        transaction that set one.
      - frequent_expense_categories / frequent_income_categories: the
        4 most-used categories of each type in the last 90 days, most
        frequent first, so common entries are one click away.
    """
    last_txn = Transaction.query.filter_by(user_id=user_id) \
        .filter(Transaction.payment_method.isnot(None)) \
        .order_by(Transaction.date.desc(), Transaction.id.desc()).first()
    last_payment_method = last_txn.payment_method if last_txn else None

    cutoff = today_ist_minus_days(90)
    recent = Transaction.query.filter_by(user_id=user_id) \
        .filter(Transaction.date >= cutoff).all()

    def top_categories(txn_type, limit=4):
        counts = {}
        for t in recent:
            if t.type == txn_type:
                counts[t.category] = counts.get(t.category, 0) + 1
        return [c for c, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)][:limit]

    return {
        "last_payment_method": last_payment_method,
        "frequent_expense_categories": top_categories(TransactionType.EXPENSE),
        "frequent_income_categories": top_categories(TransactionType.INCOME),
    }


def today_ist_minus_days(days):
    from wealth.timezone_utils import today_ist
    from datetime import timedelta
    return today_ist() - timedelta(days=days)


def get_month_summary(user_id, year, month):
    """
    Returns {
        total_income, total_expense, net,
        by_category: {category: amount, ...} (expenses only, sorted desc),
    } for the given month.
    """
    txns = get_transactions(user_id, year=year, month=month)

    total_income  = sum(t.amount for t in txns if t.type == TransactionType.INCOME)
    total_expense = sum(t.amount for t in txns if t.type == TransactionType.EXPENSE)

    by_category = {}
    for t in txns:
        if t.type == TransactionType.EXPENSE:
            by_category[t.category] = by_category.get(t.category, 0) + t.amount
    by_category = dict(sorted(by_category.items(), key=lambda kv: kv[1], reverse=True))

    return {
        "total_income":  total_income,
        "total_expense": total_expense,
        "net":           total_income - total_expense,
        "by_category":   by_category,
        "transactions":  txns,
    }


def get_year_trend(user_id, year):
    """
    Monthly income/expense/net totals for every month of `year`, for a
    12-point trend chart. Always returns all 12 months (Jan..Dec) even
    when a month has no transactions, so the chart has a full x-axis.
    """
    import calendar
    from datetime import date

    first_day = date(year, 1, 1)
    last_day = date(year, 12, calendar.monthrange(year, 12)[1])
    txns = get_transactions(user_id, from_date=first_day, to_date=last_day)

    monthly = {m: {"income": 0, "expense": 0} for m in range(1, 13)}
    for t in txns:
        bucket = monthly[t.date.month]
        if t.type == TransactionType.INCOME:
            bucket["income"] += t.amount
        elif t.type == TransactionType.EXPENSE:
            bucket["expense"] += t.amount

    labels, income, expense, net = [], [], [], []
    for m in range(1, 13):
        labels.append(calendar.month_abbr[m])
        income.append(monthly[m]["income"])
        expense.append(monthly[m]["expense"])
        net.append(monthly[m]["income"] - monthly[m]["expense"])

    return {"labels": labels, "income": income, "expense": expense, "net": net}


# ── Budgets ───────────────────────────────────────────────────────────────────

def upsert_budget(db, user_id, year, month, data):
    """Create or update the budget for a category, scoped to one month."""
    errors = validate_budget(data)
    if errors:
        return None, errors[0]

    category = data["category"].strip()
    limit    = float(data["monthly_limit"])

    budget = Budget.query.filter_by(user_id=user_id, category=category,
                                     year=year, month=month).first()
    if budget:
        budget.monthly_limit = limit
    else:
        budget = Budget(user_id=user_id, category=category, year=year, month=month,
                         monthly_limit=limit)
        db.session.add(budget)
    error = _commit(db)
    if error:
        return None, error
    return budget, None


def delete_budget(db, budget, user_id):
    if budget.user_id != user_id:
        return False, "You do not have permission to delete this budget."
    db.session.delete(budget)
    error = _commit(db)
    if error:
        return False, error
    return True, None


def get_budgets_with_progress(user_id, year, month):
    """
    One row per budget the user has set FOR THIS MONTH, with this
    month's actual spend against it:
    [{category, monthly_limit, spent, remaining, pct, status}, ...]
    status: 'ok' (<80%), 'warning' (80-100%), 'over' (>100%)
    Sorted by pct descending (closest to/over budget first).
    """
    budgets = Budget.query.filter_by(user_id=user_id, year=year, month=month) \
                          .order_by(Budget.category).all()
    summary = get_month_summary(user_id, year, month)
    spend_by_category = summary["by_category"]

    rows = []
    for b in budgets:
        spent = spend_by_category.get(b.category, 0)
        pct = (spent / b.monthly_limit * 100) if b.monthly_limit else 0
        status = "over" if pct > 100 else "warning" if pct >= 80 else "ok"
        rows.append({
            "id": b.id,
            "category": b.category,
            "monthly_limit": b.monthly_limit,
            "spent": spent,
            "remaining": b.monthly_limit - spent,
            "pct": min(pct, 100),
            "pct_raw": pct,
            "status": status,
        })
    rows.sort(key=lambda r: r["pct_raw"], reverse=True)
    return rows


def budgeted_categories(user_id, year, month):
    """Category names the user already has a budget for THIS month (to grey out in the form)."""
    return {b.category for b in Budget.query.filter_by(user_id=user_id, year=year, month=month).all()}


def copy_last_month_budgets(db, user_id, year, month):
    """
    Copy every budget from the previous month into (year, month),
    skipping categories that already have a budget this month.
    Returns (count_copied, error).
    """
    prev_year, prev_month = parse_month_key(adjacent_month_key(year, month, -1))
    prev_budgets = Budget.query.filter_by(user_id=user_id, year=prev_year, month=prev_month).all()
    if not prev_budgets:
        return 0, "No budgets found for last month to copy."

    already = budgeted_categories(user_id, year, month)
    copied = 0
    for b in prev_budgets:
        if b.category in already:
            continue
        db.session.add(Budget(user_id=user_id, category=b.category, year=year, month=month,
                               monthly_limit=b.monthly_limit))
        copied += 1

    if copied == 0:
        return 0, "Every category from last month is already budgeted this month."

    error = _commit(db)
    if error:
        return 0, error
    return copied, None


def apply_budget_to_future_months(db, budget, user_id, num_months=12):
    """
    Carry one budget's limit forward into the next `num_months`
    months, creating a row for any month that doesn't already have a
    budget for that category (existing future budgets for that
    category are left untouched, never silently overwritten).
    Returns (count_applied, error).
    """
    if budget.user_id != user_id:
        return 0, "You do not have permission to modify this budget."

    year, month = budget.year, budget.month
    applied = 0
    for i in range(1, num_months + 1):
        target_year, target_month = parse_month_key(adjacent_month_key(year, month, i))
        existing = Budget.query.filter_by(user_id=user_id, category=budget.category,
                                           year=target_year, month=target_month).first()
        if existing:
            continue
        db.session.add(Budget(user_id=user_id, category=budget.category,
                               year=target_year, month=target_month,
                               monthly_limit=budget.monthly_limit))
        applied += 1

    if applied == 0:
        return 0, "Every future month already has a budget for this category."

    error = _commit(db)
    if error:
        return 0, error
    return applied, None
