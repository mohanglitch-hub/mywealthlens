"""
International Investing Centre Blueprint
============================================
Routes are imported here so they are registered onto the blueprint
BEFORE app.register_blueprint() is called in app.py.

New standalone module (confirmed with Mohan, Sep 2026) for tracking
investments held OUTSIDE India, in their own native currency, anchored
to USD as a fixed reporting currency — see models.py for the full
design rationale.
"""
from flask import Blueprint

international_bp = Blueprint(
    "international_centre",
    __name__,
    url_prefix="/international",
    template_folder="templates",
)

# Import routes HERE — after blueprint is created, before it is registered
from international_centre import routes  # noqa
