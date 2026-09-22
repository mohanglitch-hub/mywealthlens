"""
Cashflow Centre Blueprint
==============================
Routes are imported here so they are registered onto the blueprint
BEFORE app.register_blueprint() is called in app.py.

Answers "where does my money actually go every month?" — manual
transaction entry (income/expense) plus per-category monthly budgets.
Deliberately NOT bank statement parsing or SMS/Account Aggregator
integration in this first version (see cashflow_centre/models.py for
the scoping rationale).
"""
from flask import Blueprint

cashflow_bp = Blueprint(
    "cashflow_centre",
    __name__,
    url_prefix="/cashflow",
    template_folder="templates",
)

# Import routes HERE — after blueprint is created, before it is registered
from cashflow_centre import routes  # noqa
