"""
Family Centre — Routes
========================
Aggregates four things into one People view:

  - InsuranceNominee    (insurance_centre) — who receives a policy's
    payout, and what share
  - RetirementSchemeNominee (retirement_centre) — who receives a
    scheme's balance, and what share
  - WealthAsset.is_family_or_inherited (wealth) — assets that came
    FROM a named person (inherited, gifted, family-owned)
  - FamilyPerson (family_centre's own table) — anyone added directly,
    with no nominee entry or gifted asset of their own

The first three were deliberately kept as separate tables in their
own modules — InsuranceNominee and RetirementSchemeNominee in
particular have a documented decision NOT to share one model
("different modules, different lifecycle, no genuine cross-module
dependency to share" — retirement_centre/models.py). That decision
stands; this page doesn't merge the underlying data, it just shows
it together. FamilyPerson is new — the first data Family Centre
owns outright, for people who wouldn't otherwise appear anywhere.

The People view groups all four sources by name so the same person
shows up once. Grouping is a simple case-insensitive exact match —
not real identity resolution. Two people who happen to share a name
would be merged; the same person spelled two different ways would
show up twice. A separate "Possible Duplicates?" check (see
_find_possible_duplicates) flags near-miss spellings using a fuzzy
text comparison, calibrated against real examples, not guessed:
"Priya Sharma" vs "Priya Shrma" (a dropped letter) scores 0.957;
"Priya Sharma" vs "Priyanka Sharma" (a genuinely different person
who shares a surname) scores 0.889. The 0.90 threshold sits cleanly
between those two groups. Known gap, stated plainly: "Priya Sharma"
vs "Priya S. Sharma" (a real duplicate, just with a middle initial
added) also scores 0.889 — mathematically identical to the
different-person case above — so that specific kind of near-miss
won't be caught. Accepted deliberately to avoid false alarms on
unrelated people who share a common surname.
"""
from difflib import SequenceMatcher

from flask import render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user

from family_centre import family_bp
from family_centre.models import FamilyPerson
from insurance_centre.models import InsuranceNominee, InsurancePolicy
from retirement_centre.models import RetirementSchemeNominee, RetirementScheme
from wealth.models import WealthAsset


DUPLICATE_THRESHOLD = 0.90


def _build_people(user_id):
    """
    Returns a list of {display_name, entries} dicts — one per unique
    person (grouped by name.strip().lower()), each entries list
    holding every connection found for them across all four sources,
    in a stable order: Insurance, then Retirement, then Wealth, then
    manually-added.
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

    # ── Manually-added family members ──
    manual_rows = FamilyPerson.query.filter_by(user_id=user_id).all()
    for m in manual_rows:
        _add(m.name, {
            "direction": "manual",
            "relationship": m.relationship,
            "source": "Family Centre",
            "family_person_id": m.id,
        })

    # Stable order: most-connected person first, then alphabetical
    return sorted(
        people.values(),
        key=lambda p: (-len(p["entries"]), p["display_name"].lower())
    )


def _find_possible_duplicates(people):
    """
    Flags pairs of DIFFERENT people whose names are suspiciously
    close — likely the same person, spelled two different ways
    somewhere. Deliberately does not auto-merge anything; the fix
    (correcting the spelling at its source) is left to the user,
    since this tool has no reliable way to know for certain these
    are the same person versus two people who just sound alike.
    """
    duplicates = []
    names = [p["display_name"] for p in people]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            score = SequenceMatcher(
                None, names[i].lower(), names[j].lower()
            ).ratio()
            if score >= DUPLICATE_THRESHOLD:
                duplicates.append((people[i], people[j], score))
    duplicates.sort(key=lambda d: -d[2])
    return duplicates


def _coverage_gaps(user_id):
    """
    Answers "what happens if I'm not around" more directly than the
    People view alone: which active policies/schemes have NO nominee
    at all, and which have nominees whose percentages don't add up
    to 100 — either gap, a portion of the payout has nowhere defined
    to go.
    """
    gaps = []

    policies = InsurancePolicy.query.filter_by(
        user_id=user_id, is_archived=False).all()
    for p in policies:
        total_pct = sum(n.percentage or 0 for n in p.nominees)
        if p.nominees.count() == 0:
            gaps.append({
                "source": "Insurance", "item_name": f"{p.display_type} — {p.insurer}",
                "issue": "No nominee added", "link": f"/insurance-centre/policy/{p.id}",
            })
        elif total_pct < 100:
            gaps.append({
                "source": "Insurance", "item_name": f"{p.display_type} — {p.insurer}",
                "issue": f"Nominees total {total_pct:.0f}%, not 100%",
                "link": f"/insurance-centre/policy/{p.id}",
            })

    schemes = RetirementScheme.query.filter_by(
        user_id=user_id, is_archived=False).all()
    for s in schemes:
        total_pct = sum(n.percentage or 0 for n in s.nominees)
        if s.nominees.count() == 0:
            gaps.append({
                "source": "Retirement", "item_name": s.display_type,
                "issue": "No nominee added", "link": f"/retirement/scheme/{s.id}",
            })
        elif total_pct < 100:
            gaps.append({
                "source": "Retirement", "item_name": s.display_type,
                "issue": f"Nominees total {total_pct:.0f}%, not 100%",
                "link": f"/retirement/scheme/{s.id}",
            })

    return gaps


def _all_known_names(user_id):
    """Every distinct name already recorded anywhere (nominees,
    benefactors, manually-added people) — feeds the Add Family
    form's autocomplete, so a near-miss spelling can be caught
    before it's typed, not just flagged after the fact."""
    return sorted({p["display_name"] for p in _build_people(user_id)})


@family_bp.route("/")
@login_required
def dashboard():
    people = _build_people(current_user.id)
    duplicates = _find_possible_duplicates(people)
    gaps = _coverage_gaps(current_user.id)

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
        duplicates=duplicates,
        gaps=gaps,
        all_known_names=_all_known_names(current_user.id),
    )


@family_bp.route("/add", methods=["POST"])
@login_required
def add_person():
    name = (request.form.get("name") or "").strip()
    relationship = (request.form.get("relationship") or "").strip() or None

    if not name:
        flash("A name is required.", "error")
        return redirect(url_for("family_centre.dashboard"))

    person = FamilyPerson(user_id=current_user.id, name=name, relationship=relationship)
    from models import db
    db.session.add(person)
    db.session.commit()
    flash(f'"{name}" added.', "success")
    return redirect(url_for("family_centre.dashboard"))


@family_bp.route("/people/<int:person_id>/delete", methods=["POST"])
@login_required
def delete_person(person_id):
    person = FamilyPerson.query.filter_by(
        id=person_id, user_id=current_user.id).first_or_404()
    from models import db
    db.session.delete(person)
    db.session.commit()
    flash(f'"{person.name}" removed.', "success")
    return redirect(url_for("family_centre.dashboard"))