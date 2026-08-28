"""
Family Centre — Routes
========================
This module owns no new database tables. It is a read-only view that
aggregates three things that already exist, each in its own module:

  - InsuranceNominee    (insurance_centre) — who receives a policy's
    payout, and what share
  - RetirementSchemeNominee (retirement_centre) — who receives a
    scheme's balance, and what share
  - WealthAsset.is_family_or_inherited (wealth) — assets that came
    FROM a named person (inherited, gifted, family-owned)

These three were deliberately kept as separate tables in their own
modules — InsuranceNominee and RetirementSchemeNominee in particular
have a documented decision NOT to share one model ("different
modules, different lifecycle, no genuine cross-module dependency to
share" — retirement_centre/models.py). That decision stands; this
page doesn't merge the underlying data, it just shows it together.

The one genuinely new thing here is the People view: nominee/
benefactor entries are grouped by name so the same person shows up
once, with everything they're connected to, instead of scattered
across three separate module pages with no shared concept of them
as a person. This grouping is a simple case-insensitive name match
— not real identity resolution. Two people who happen to share a
name would be merged; the same person spelled two different ways
would show up twice. That's a real, known limitation, not a bug —
stated here so it's never quietly assumed to be smarter than it is.
"""
from flask import render_template
from flask_login import login_required, current_user

from family_centre import family_bp
from insurance_centre.models import InsuranceNominee, InsurancePolicy
from retirement_centre.models import RetirementSchemeNominee, RetirementScheme
from wealth.models import WealthAsset


def _build_people(user_id):
    """
    Returns a list of {display_name, entries} dicts — one per unique
    person (grouped by name.strip().lower()), each entries list
    holding every nominee/benefactor connection found for them,
    across all three sources, newest source data first isn't
    meaningful here so entries are just grouped by source in a
    stable, predictable order: Insurance, then Retirement, then
    Wealth.
    """
    people = {}  # key: lowered name -> {"display_name": ..., "entries": [...]}

    def _add(raw_name, entry):
        if not raw_name or not raw_name.strip():
            return
        key = raw_name.strip().lower()
        if key not in people:
            people[key] = {"display_name": raw_name.strip(), "entries": []}
        people[key]["entries"].append(entry)

    # ── Insurance Nominees (active policies only) ──
    ins_rows = (
        InsuranceNominee.query
        .join(InsurancePolicy, InsuranceNominee.policy_id == InsurancePolicy.id)
        .filter(InsurancePolicy.user_id == user_id,
                InsurancePolicy.is_archived == False)
        .all()
    )
    for n in ins_rows:
        _add(n.name, {
            "direction": "nominee",
            "relationship": n.relationship,
            "source": "Insurance",
            "item_name": f"{n.policy.display_type} — {n.policy.insurer}",
            "percentage": n.percentage,
            "link": f"/insurance-centre/policy/{n.policy_id}",
        })

    # ── Retirement Nominees (active schemes only) ──
    ret_rows = (
        RetirementSchemeNominee.query
        .join(RetirementScheme, RetirementSchemeNominee.scheme_id == RetirementScheme.id)
        .filter(RetirementScheme.user_id == user_id,
                RetirementScheme.is_archived == False)
        .all()
    )
    for n in ret_rows:
        _add(n.name, {
            "direction": "nominee",
            "relationship": n.relationship,
            "source": "Retirement",
            "item_name": n.scheme.display_type,
            "percentage": n.percentage,
            "link": f"/retirement/scheme/{n.scheme_id}",
        })

    # ── Family & Inherited Wealth (active assets only, with a
    #    recorded original owner — an asset marked "Family Owned"
    #    with no name entered has nobody to group by) ──
    wealth_rows = (
        WealthAsset.query
        .filter(WealthAsset.user_id == user_id,
                WealthAsset.is_archived == False,
                WealthAsset.original_owner.isnot(None),
                WealthAsset.original_owner != "")
        .all()
    )
    for a in wealth_rows:
        if not a.is_family_or_inherited:
            continue
        _add(a.original_owner, {
            "direction": "benefactor",
            "relationship": a.original_owner_relationship,
            "source": "Wealth",
            "item_name": a.name,
            "detail": a.source_type,
            "date_received": a.date_received,
            "link": f"/wealth/assets/{a.id}",
        })

    # Stable order: most-connected person first, then alphabetical
    return sorted(
        people.values(),
        key=lambda p: (-len(p["entries"]), p["display_name"].lower())
    )


@family_bp.route("/")
@login_required
def dashboard():
    people = _build_people(current_user.id)

    total_nominee_entries = sum(
        1 for p in people for e in p["entries"] if e["direction"] == "nominee"
    )
    total_benefactor_entries = sum(
        1 for p in people for e in p["entries"] if e["direction"] == "benefactor"
    )

    return render_template(
        "family_centre/dashboard.html",
        people=people,
        people_count=len(people),
        total_nominee_entries=total_nominee_entries,
        total_benefactor_entries=total_benefactor_entries,
    )
