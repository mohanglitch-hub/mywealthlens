"""
Insurance Centre Blueprint
===========================
Routes are imported here so they are registered onto the blueprint
BEFORE app.register_blueprint() is called in app.py.
"""
from flask import Blueprint

insurance_bp = Blueprint(
    "insurance_centre",
    __name__,
    url_prefix="/insurance-centre",
    template_folder="templates",
)

# Import routes HERE — after blueprint is created, before it is registered
from insurance_centre import routes  # noqa