"""
Retirement Centre Blueprint
==============================
Routes are imported here so they are registered onto the blueprint
BEFORE app.register_blueprint() is called in app.py.

Phase A — Foundation only. See retirement_centre/routes.py for
what's implemented so far.
"""
from flask import Blueprint

retirement_bp = Blueprint(
    "retirement_centre",
    __name__,
    url_prefix="/retirement",
    template_folder="templates",
)

# Import routes HERE — after blueprint is created, before it is registered
from retirement_centre import routes  # noqa
