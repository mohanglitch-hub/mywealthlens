"""
Wealth Module Blueprint
=========================
Phase A — Foundation & Architecture.

Completely independent from the existing Assets module — no shared
tables, no foreign keys in either direction, no compatibility layer.
See models.py for the full architectural reasoning.

Routes are imported here so they are registered onto the blueprint
BEFORE app.register_blueprint() is called in app.py.
"""
from flask import Blueprint

wealth_bp = Blueprint(
    "wealth",
    __name__,
    url_prefix="/wealth",
    template_folder="templates",
)

# Import routes HERE — after blueprint is created, before it is registered
from wealth import routes  # noqa
