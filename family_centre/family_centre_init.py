"""
Family Centre Blueprint
==========================
Routes are imported here so they are registered onto the blueprint
BEFORE app.register_blueprint() is called in app.py.

This module owns no new database tables — it is a read-only view
that aggregates existing data (InsuranceNominee, RetirementScheme
Nominee, and Wealth's Family & Inherited Wealth assets) into one
place. See routes.py for the full rationale.
"""
from flask import Blueprint

family_bp = Blueprint(
    "family_centre",
    __name__,
    url_prefix="/family",
    template_folder="templates",
)

# Import routes HERE — after blueprint is created, before it is registered
from family_centre import routes  # noqa
