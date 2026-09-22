"""
Cashflow Centre — Services
==============================
Routes never touch models directly — all reads/writes go through
here, matching the convention used across every other module.
Every query is filtered by user_id (IDOR check).
"""
from datetime import datetime

from cashflow_centre.models import Transaction, Budget, TransactionType
from cashflow_centre.utils import month_bounds
from cashflow_centre.validators import validate_transaction, validate_budget


def _parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


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
    db.session.commit()
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
    db.session.commit()
    return txn, None


def delete_transaction(db, txn, user_id):
    if txn.user_id != user_id:
        return False, "You do not have permission to delete this transaction."
    db.session.delete(txn)
    db.session.commit()
    return True, None


def get_transactions(user_id, year=None, month=None, category=None, txn_type=None):
    """
    Transactions for a user, optionally filtered by month (year+month
    both given), category, and/or type. Ordered newest-first.
    """
    query = Transaction.query.filter_by(user_id=user_id)
    if year and month:
        first_day, last_day = month_bounds(year, month)
        query = query.filter(Transaction.date >= first_day, Transaction.date <= last_day)
    if category:
        query = query.filter_by(category=category)
    if txn_type:
        query = query.filter_by(type=txn_type)
    return query.order_by(Transaction.date.desc(), Transaction.id.desc()).all()


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


# ── Budgets ───────────────────────────────────────────────────────────────────

def upsert_budget(db, user_id, data):
    """Create or update the budget for a category (one row per category)."""
    errors = validate_budget(data)
    if errors:
        return None, errors[0]

    category = data["category"].strip()
    limit    = float(data["monthly_limit"])

    budget = Budget.query.filter_by(user_id=user_id, category=category).first()
    if budget:
        budget.monthly_limit = limit
    else:
        budget = Budget(user_id=user_id, category=category, monthly_limit=limit)
        db.session.add(budget)
    db.session.commit()
    return budget, None


def delete_budget(db, budget, user_id):
    if budget.user_id != user_id:
        return False, "You do not have permission to delete this budget."
    db.session.delete(budget)
    db.session.commit()
    return True, None


def get_budgets_with_progress(user_id, year, month):
    """
    One row per budget the user has set, with this month's actual
    spend against it:
    [{category, monthly_limit, spent, remaining, pct, status}, ...]
    status: 'ok' (<80%), 'warning' (80-100%), 'over' (>100%)
    Sorted by pct descending (closest to/over budget first).
    """
    budgets = Budget.query.filter_by(user_id=user_id).order_by(Budget.category).all()
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


def budgeted_categories(user_id):
    """Category names the user already has a budget for (to grey out in the form)."""
    return {b.category for b in Budget.query.filter_by(user_id=user_id).all()}
