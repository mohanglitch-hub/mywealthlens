"""
Cashflow Centre — Models
==============================
Scoping decision (confirmed with Mohan): manual transaction entry only
— no bank/card statement parsing, no SMS reading (not possible from a
web app anyway), no Account Aggregator integration. Budgets are a
simple per-category monthly limit, not a full envelope-budgeting
system. Both can be extended later once this core pipeline is proven.

Two tables:
  1. Transaction — a single income or expense entry
  2. Budget      — one monthly limit per (user, expense category)

Deletion: transactions use a straightforward delete with a confirm
modal, NOT the Archive→Restore lifecycle used elsewhere in the app —
a deliberate exception, since transactions are high-volume and
individually low-stakes (unlike a policy, scheme, or asset).
"""
from datetime import datetime
from models import db


class TransactionType:
    INCOME  = "income"
    EXPENSE = "expense"
    ALL = [INCOME, EXPENSE]


class ExpenseCategory:
    FOOD          = "Food & Dining"
    TRANSPORT     = "Transport"
    BILLS         = "Bills & Utilities"
    SHOPPING      = "Shopping"
    ENTERTAINMENT = "Entertainment"
    HEALTHCARE    = "Healthcare"
    RENT_EMI      = "Rent / EMI"
    EDUCATION     = "Education"
    TRAVEL        = "Travel"
    INVESTMENTS   = "Investments"
    OTHER         = "Other"
    ALL = [FOOD, TRANSPORT, BILLS, SHOPPING, ENTERTAINMENT, HEALTHCARE,
           RENT_EMI, EDUCATION, TRAVEL, INVESTMENTS, OTHER]


class IncomeCategory:
    SALARY   = "Salary"
    BUSINESS = "Business Income"
    INTEREST = "Interest"
    DIVIDEND = "Dividend"
    RENTAL   = "Rental Income"
    OTHER    = "Other Income"
    ALL = [SALARY, BUSINESS, INTEREST, DIVIDEND, RENTAL, OTHER]


class PaymentMethod:
    CASH        = "Cash"
    UPI         = "UPI"
    DEBIT_CARD  = "Debit Card"
    CREDIT_CARD = "Credit Card"
    NET_BANKING = "Net Banking"
    OTHER       = "Other"
    ALL = [CASH, UPI, DEBIT_CARD, CREDIT_CARD, NET_BANKING, OTHER]


class Transaction(db.Model):
    """A single manually-entered income or expense entry."""
    __tablename__ = "cashflow_transaction"
    __table_args__ = (
        db.Index("ix_cf_txn_user_date", "user_id", "date"),
    )

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    date   = db.Column(db.Date, nullable=False)
    type   = db.Column(db.String(10), nullable=False)   # TransactionType.INCOME/EXPENSE
    category = db.Column(db.String(50), nullable=False) # from ExpenseCategory.ALL or IncomeCategory.ALL, matching type

    amount = db.Column(db.Float, nullable=False)

    payment_method = db.Column(db.String(30), nullable=True)  # PaymentMethod.ALL, optional
    description    = db.Column(db.String(255), nullable=True) # optional note/payee

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Transaction {self.type} {self.category} {self.amount}>"

    @property
    def month_key(self):
        """'YYYY-MM' — used to group transactions by month."""
        return self.date.strftime("%Y-%m")


class Budget(db.Model):
    """One monthly spending limit per (user, expense category)."""
    __tablename__ = "cashflow_budget"
    __table_args__ = (
        db.UniqueConstraint("user_id", "category", name="uq_cf_budget_user_category"),
    )

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    category      = db.Column(db.String(50), nullable=False)  # from ExpenseCategory.ALL
    monthly_limit = db.Column(db.Float, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Budget {self.category} {self.monthly_limit}>"
