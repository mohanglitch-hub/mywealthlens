"""
Family Centre — Models
=========================
Family Centre started as a pure read-only view over three existing
sources (InsuranceNominee, RetirementSchemeNominee, WealthAsset's
Family & Inherited Wealth). This is its first owned table — for
someone with no nominee entry and no gifted/inherited asset, but who
you still want on record (a sibling, a parent with no policy naming
them, anyone).

Deliberately minimal: name + relationship, nothing else. No age, no
contact info, no documents — those are real ideas for later, not
assumed here. Matches this app's established discipline of adding
exactly what was asked for, not what might eventually be useful.
"""
from datetime import datetime
from models import db


class FamilyPerson(db.Model):
    """A person added directly to the Family Centre, independent of
    any Insurance/Retirement nominee entry or Wealth asset."""
    __tablename__ = "family_person"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name         = db.Column(db.String(200), nullable=False)
    relationship = db.Column(db.String(100), nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<FamilyPerson {self.name} ({self.relationship})>"