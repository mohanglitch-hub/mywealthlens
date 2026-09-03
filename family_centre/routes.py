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
import re

from .utils import format_inr
from . import services
from .models import TimelineEvent

from flask import render_template, redirect, url_for, request, flash
from flask_login import login_required, current_user

from family_centre import family_bp
from family_centre.models import FamilyPerson, FamilyTimeline
from insurance_centre.models import InsuranceNominee, InsurancePolicy
from retirement_centre.models import RetirementSchemeNominee, RetirementScheme
from wealth.models import WealthAsset, WealthAssetHeir


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
            "contact": n.contact,
            "value_at_stake": (n.percentage / 100 * n.policy.sum_assured) if n.percentage else None,
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
            "contact": n.contact,
            "value_at_stake": (n.percentage / 100 * n.scheme.current_balance) if n.percentage else None,
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

    # ── Intended Heirs (active assets with recorded heirs) ──
    # The inverse direction from the benefactor entries just above:
    # "who should get this asset", using the SAME table shape as
    # InsuranceNominee/RetirementSchemeNominee — an asset can have
    # multiple heirs, each with their own %, just like a policy can
    # have multiple nominees. An asset can ALSO have an
    # original_owner (inherited from someone) at the same time as
    # having heirs (going to someone else) — two unrelated facts,
    # handled by two separate queries.
    heir_rows = (
        WealthAssetHeir.query
        .join(WealthAsset, WealthAssetHeir.asset_id == WealthAsset.id)
        .filter(WealthAsset.user_id == user_id,
                WealthAsset.is_archived == False)
        .all()
    )
    for h in heir_rows:
        _add(h.name, {
            "direction": "heir",
            "relationship": h.relationship,
            "source": "Wealth",
            "item_name": h.asset.name,
            "percentage": h.percentage,
            "value_at_stake": (h.percentage / 100 * h.asset.current_value) if h.percentage else None,
            "link": f"/wealth/assets/{h.asset_id}",
        })

    # ── Manually-added family members ──
    manual_rows = FamilyPerson.query.filter_by(user_id=user_id).all()
    for m in manual_rows:
        _add(m.name, {
            "direction": "manual",
            "relationship": m.relationship,
            "source": "Family Centre",
            "family_person_id": m.id,
            "raw_name": m.name,
        })

    # Attach Primary Contact / Minor+Guardian metadata, where set.
    # Covers people who only exist here via a nominee/benefactor/heir
    # entry (their FamilyPerson row, if any, was created on-demand —
    # see services.get_or_create_family_person) as well as manually
    # added ones.
    metadata_by_name = services.get_metadata_by_name(user_id)
    for p in people.values():
        meta = metadata_by_name.get(p["display_name"].strip().lower())
        p["is_primary_contact"] = bool(meta and meta.is_primary_contact)
        p["is_minor"] = bool(meta and meta.is_minor)
        p["guardian_name"] = meta.guardian_name if meta else None
        p["guardian_relationship"] = meta.guardian_relationship if meta else None
        p["guardian_contact"] = meta.guardian_contact if meta else None

    # Surface one contact number per person, for the card header —
    # the first non-empty one found across their entries, not
    # repeated on every line (most people only ever have one
    # recorded anyway, and showing it once, prominently, next to
    # their name is more useful in a crisis than buried per-entry).
    for p in people.values():
        p["contact"] = next(
            (e["contact"] for e in p["entries"] if e.get("contact")), None
        )
        # Total rupee value across every nominee/heir entry with a
        # known share — turns "50% share" into an actual number.
        # Only nominee/heir entries carry value_at_stake; benefactor
        # entries (what THEY gave YOU) and manual entries (no
        # percentage concept at all) never contribute here.
        known_values = [
            e["value_at_stake"] for e in p["entries"]
            if e.get("value_at_stake") is not None
        ]
        p["total_value_at_stake"] = sum(known_values) if known_values else None

    # Stable order: Primary Contact first (if set), then
    # most-connected person, then alphabetical
    return sorted(
        people.values(),
        key=lambda p: (
            0 if p["is_primary_contact"] else 1,
            -len(p["entries"]),
            p["display_name"].lower(),
        )
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


def _find_relationship_inconsistencies(people):
    """
    Flags a person (already correctly grouped by name — this is a
    within-person check, not a cross-person one like the duplicate
    detector above) whose entries disagree on the relationship. If
    Chaithra is "Sister" on one policy and "Cousin" on another,
    that's worth a second look — one of them is likely a mistake.

    Deliberately does NOT use fuzzy text matching here, unlike name
    duplicates: "Wife" and "Spouse" mean the same thing but share
    almost no letters, so a spelling-similarity check would miss the
    exact case it's meant to catch. Any genuine difference (after
    trimming whitespace and case) gets flagged — this can't tell a
    real conflict from two honest phrasings of the same relationship,
    so like the duplicate detector, it flags rather than judges.
    """
    inconsistencies = []
    for p in people:
        seen = {}  # normalized -> original casing, first seen
        for e in p["entries"]:
            rel = e.get("relationship")
            if not rel or not rel.strip():
                continue
            key = rel.strip().lower()
            if key not in seen:
                seen[key] = rel.strip()
        if len(seen) > 1:
            inconsistencies.append({
                "display_name": p["display_name"],
                "relationships": list(seen.values()),
            })
    return inconsistencies


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

    assets = WealthAsset.query.filter_by(
        user_id=user_id, is_archived=False).all()
    for a in assets:
        total_pct = a.total_heirs_percentage
        if a.heirs.count() == 0:
            gaps.append({
                "source": "Wealth", "item_name": a.name,
                "issue": "No intended heir set", "link": f"/wealth/assets/{a.id}",
            })
        elif total_pct < 100:
            gaps.append({
                "source": "Wealth", "item_name": a.name,
                "issue": f"Heirs total {total_pct:.0f}%, not 100%",
                "link": f"/wealth/assets/{a.id}",
            })

    return gaps


def _all_known_names(user_id):
    """Every distinct name already recorded anywhere (nominees,
    benefactors, manually-added people) — feeds the Add Family
    form's autocomplete, so a near-miss spelling can be caught
    before it's typed, not just flagged after the fact."""
    return sorted({p["display_name"] for p in _build_people(user_id)})


def _dominant_relationship(entries):
    """The most common non-empty relationship text across a person's
    entries — most people only ever have one, but a nominee listed
    on two policies could theoretically have it typed differently
    each time; picking the most common (not just the first) matches
    the spirit of _find_relationship_inconsistencies flagging
    genuine disagreements rather than this function silently picking
    a side."""
    from collections import Counter
    vals = [e.get("relationship") for e in entries if e.get("relationship") and e["relationship"].strip()]
    if not vals:
        return None
    return Counter(v.strip() for v in vals).most_common(1)[0][0]


def _classify_relationship(relationship):
    """
    Maps a free-text relationship string (typed elsewhere in the
    app — Insurance/Retirement nominee forms, Wealth's original
    owner field, Family Centre's own Add Person form) to a
    generation tier for the family tree: -1 (parents' generation),
    0 (your own generation — spouse and siblings), 1 (children's
    generation), or None if it doesn't match anything recognised.

    Grandparent/grandchild wording folds into the parent/child tier
    rather than getting a 4th and 5th row — this diagram mirrors the
    three-generation structure Mohan described (parents / you,
    spouse & siblings / children), and a separate row for the rare
    "grandfather" entry would add width without adding clarity.

    Word-boundary matching (not plain substring) so "grandson" is
    checked and claimed by the grandchild keywords BEFORE the
    child-tier check ever sees it — a naive substring check would
    misfile "grandson" as "son" because the word literally contains it.

    Deliberately keyword-based against free text, not a structured
    field — nothing elsewhere in the app asks for a relationship in
    a fixed vocabulary, so this can't be 100% reliable. A relationship
    that doesn't match anything here (a nominee with none recorded,
    or something like "Cousin", "Friend", "Guardian", "Colleague")
    returns None and is deliberately NOT guessed into a row — see
    "Other Connections" in the route/template.
    """
    if not relationship:
        return None, None
    r = relationship.strip().lower()

    def has(word):
        return re.search(r'\b' + re.escape(word) + r'\b', r) is not None

    if any(has(k) for k in [
        "grandson", "granddaughter", "son-in-law", "daughter-in-law",
        "son", "daughter",
    ]):
        return 1, None
    if any(has(k) for k in [
        "grandfather", "grandmother", "grandpa", "grandma",
        "father-in-law", "mother-in-law", "father", "mother", "dad", "mom", "papa",
    ]):
        return -1, None
    if any(has(k) for k in ["wife", "husband", "spouse"]):
        return 0, "spouse"
    if any(has(k) for k in ["sister-in-law", "brother-in-law", "sister", "brother"]):
        return 0, "sibling"
    return None, None


def _build_tree_data(people):
    """
    A genealogical chart, top to bottom by generation — parents on
    top, you + spouse + siblings in the middle, children on the
    bottom — rather than the earlier radial "how is everyone
    connected" layout. Positions computed here in Python (no JS
    charting dependency anywhere else in this app); the template
    just draws lines and circles at the coordinates this returns.

    Anyone whose relationship text doesn't classify into a
    generation (see _classify_relationship) is returned separately
    under "other" rather than placed in the diagram — showing them
    in the wrong row would misrepresent the family structure this
    chart exists to show correctly.
    """
    ROW_Y = {-1: 90, 0: 300, 1: 510}
    BUS_TOP_Y = (ROW_Y[-1] + ROW_Y[0]) / 2
    BUS_BOTTOM_Y = (ROW_Y[0] + ROW_Y[1]) / 2
    SPACING = 160
    MIN_WIDTH = 700

    parents, children, siblings, spouses, other = [], [], [], [], []
    for p in people:
        rel = _dominant_relationship(p["entries"])
        tier, role = _classify_relationship(rel)
        entry = {
            "name": p["display_name"],
            "relationship": rel,
            "is_primary_contact": p["is_primary_contact"],
            "is_minor": p["is_minor"],
            "connection_count": len(p["entries"]),
        }
        if tier == -1:
            entry["role"] = "parent"
            parents.append(entry)
        elif tier == 1:
            entry["role"] = "child"
            children.append(entry)
        elif tier == 0 and role == "spouse":
            entry["role"] = "spouse"
            spouses.append(entry)
        elif tier == 0 and role == "sibling":
            entry["role"] = "sibling"
            siblings.append(entry)
        else:
            other.append(entry)

    for group in (parents, children, siblings, spouses, other):
        group.sort(key=lambda e: e["name"].lower())

    if not (parents or children or siblings or spouses):
        return {"has_tree": False, "other": other, "viewbox_width": MIN_WIDTH,
                "viewbox_height": 600, "nodes": [], "edges": []}

    # Middle row, left to right: siblings, then You, then spouse(s) —
    # spouse always immediately beside You.
    middle_count = len(siblings) + 1 + len(spouses)
    row_counts = [len(parents), middle_count, len(children)]
    viewbox_width = max(MIN_WIDTH, (max(row_counts) + 1) * SPACING)
    center_x = viewbox_width / 2

    def _spread(items, y):
        n = len(items)
        if n == 0:
            return []
        start_x = center_x - (n - 1) * SPACING / 2
        return [{**item, "x": start_x + i * SPACING, "y": y} for i, item in enumerate(items)]

    parent_nodes = _spread(parents, ROW_Y[-1])
    child_nodes = _spread(children, ROW_Y[1])

    you_marker = {"name": "You", "relationship": None, "is_primary_contact": False,
                  "is_minor": False, "connection_count": 0, "role": "you"}
    middle_items = siblings + [you_marker] + spouses
    middle_nodes = _spread(middle_items, ROW_Y[0])
    you_node = next(n for n in middle_nodes if n["role"] == "you")
    sibling_nodes = [n for n in middle_nodes if n["role"] == "sibling"]
    spouse_nodes = [n for n in middle_nodes if n["role"] == "spouse"]

    union_x = (you_node["x"] + spouse_nodes[0]["x"]) / 2 if spouse_nodes else you_node["x"]
    union_y = ROW_Y[0]

    edges = []  # each: {"type": "trunk"/"branch", "x1","y1","x2","y2"}

    for s in spouse_nodes:
        edges.append({"x1": you_node["x"], "y1": you_node["y"], "x2": s["x"], "y2": s["y"], "kind": "spouse"})
    for s in sibling_nodes:
        edges.append({"x1": union_x, "y1": union_y, "x2": s["x"], "y2": s["y"], "kind": "sibling"})

    if parent_nodes:
        edges.append({"x1": union_x, "y1": union_y, "x2": union_x, "y2": BUS_TOP_Y, "kind": "trunk"})
        xs = [n["x"] for n in parent_nodes]
        edges.append({"x1": min(xs), "y1": BUS_TOP_Y, "x2": max(xs), "y2": BUS_TOP_Y, "kind": "bus"})
        for n in parent_nodes:
            edges.append({"x1": n["x"], "y1": BUS_TOP_Y, "x2": n["x"], "y2": n["y"], "kind": "trunk"})

    if child_nodes:
        edges.append({"x1": union_x, "y1": union_y, "x2": union_x, "y2": BUS_BOTTOM_Y, "kind": "trunk"})
        xs = [n["x"] for n in child_nodes]
        edges.append({"x1": min(xs), "y1": BUS_BOTTOM_Y, "x2": max(xs), "y2": BUS_BOTTOM_Y, "kind": "bus"})
        for n in child_nodes:
            edges.append({"x1": n["x"], "y1": BUS_BOTTOM_Y, "x2": n["x"], "y2": n["y"], "kind": "trunk"})

    all_nodes = parent_nodes + middle_nodes + child_nodes
    viewbox_height = ROW_Y[1] + 120

    return {
        "has_tree": True,
        "nodes": all_nodes,
        "edges": edges,
        "other": other,
        "viewbox_width": viewbox_width,
        "viewbox_height": viewbox_height,
    }


@family_bp.route("/")
@login_required
def dashboard():
    people = _build_people(current_user.id)
    duplicates = _find_possible_duplicates(people)
    relationship_conflicts = _find_relationship_inconsistencies(people)
    gaps = _coverage_gaps(current_user.id)

    total_nominee_entries = sum(
        1 for p in people for e in p["entries"] if e["direction"] == "nominee"
    )
    total_benefactor_entries = sum(
        1 for p in people for e in p["entries"] if e["direction"] == "benefactor"
    )
    total_heir_entries = sum(
        1 for p in people for e in p["entries"] if e["direction"] == "heir"
    )

    return render_template(
        "family_centre/dashboard.html",
        people=people,
        people_count=len(people),
        total_nominee_entries=total_nominee_entries,
        total_benefactor_entries=total_benefactor_entries,
        total_heir_entries=total_heir_entries,
        duplicates=duplicates,
        relationship_conflicts=relationship_conflicts,
        gaps=gaps,
        all_known_names=_all_known_names(current_user.id),
        format_inr=format_inr,
    )


@family_bp.route("/add", methods=["POST"])
@login_required
def add_person():
    name = (request.form.get("name") or "").strip()
    relationship = (request.form.get("relationship") or "").strip() or None

    if not name:
        flash("A name is required.", "error")
        return redirect(url_for("family_centre.dashboard"))

    person = FamilyPerson(user_id=current_user.id, name=name, relationship=relationship,
                           is_manual=True)
    from models import db
    db.session.add(person)
    services.log_timeline(
        current_user.id, TimelineEvent.PERSON_ADDED,
        f"{name} added" + (f" ({relationship})" if relationship else ""),
        person_name=name,
    )
    db.session.commit()
    flash(f'"{name}" added.', "success")
    return redirect(url_for("family_centre.dashboard"))


@family_bp.route("/people/<int:person_id>/delete", methods=["POST"])
@login_required
def delete_person(person_id):
    person = FamilyPerson.query.filter_by(
        id=person_id, user_id=current_user.id).first_or_404()
    from models import db
    removed_name = person.name
    db.session.delete(person)
    services.log_timeline(
        current_user.id, TimelineEvent.PERSON_REMOVED,
        f"{removed_name} removed", person_name=removed_name,
    )
    db.session.commit()
    flash(f'"{person.name}" removed.', "success")
    return redirect(url_for("family_centre.dashboard"))


@family_bp.route("/people/<int:person_id>/edit", methods=["POST"])
@login_required
def edit_person(person_id):
    person = FamilyPerson.query.filter_by(
        id=person_id, user_id=current_user.id).first_or_404()

    name = (request.form.get("name") or "").strip()
    if not name:
        flash("A name is required.", "error")
        return redirect(url_for("family_centre.dashboard"))

    old_name = person.name
    person.name = name
    person.relationship = (request.form.get("relationship") or "").strip() or None
    from models import db
    services.log_timeline(
        current_user.id, TimelineEvent.PERSON_UPDATED,
        f"{old_name} updated" + (f" → {name}" if old_name != name else ""),
        person_name=name,
    )
    db.session.commit()
    flash(f'"{name}" updated.', "success")
    return redirect(url_for("family_centre.dashboard"))


@family_bp.route("/people/primary-contact/set", methods=["POST"])
@login_required
def set_primary_contact():
    name = (request.form.get("name") or "").strip()
    person, error = services.set_primary_contact(current_user.id, name)
    if error:
        flash(error, "error")
    else:
        flash(f'"{person.name}" marked as Primary Contact.', "success")
    return redirect(url_for("family_centre.dashboard"))


@family_bp.route("/people/primary-contact/clear", methods=["POST"])
@login_required
def clear_primary_contact():
    name = (request.form.get("name") or "").strip()
    services.clear_primary_contact(current_user.id, name)
    flash(f'"{name}" unmarked as Primary Contact.', "success")
    return redirect(url_for("family_centre.dashboard"))


@family_bp.route("/people/minor-guardian/set", methods=["POST"])
@login_required
def set_minor_guardian():
    name = (request.form.get("name") or "").strip()
    guardian_name = request.form.get("guardian_name")
    guardian_relationship = request.form.get("guardian_relationship")
    guardian_contact = request.form.get("guardian_contact")
    person, error = services.set_minor_guardian(
        current_user.id, name, guardian_name, guardian_relationship, guardian_contact
    )
    if error:
        flash(error, "error")
    else:
        flash(f'"{person.name}" marked as a minor with guardian details recorded.', "success")
    return redirect(url_for("family_centre.dashboard"))


@family_bp.route("/people/minor-guardian/clear", methods=["POST"])
@login_required
def clear_minor_guardian():
    name = (request.form.get("name") or "").strip()
    services.clear_minor_guardian(current_user.id, name)
    flash(f'"{name}" — minor + guardian status cleared.', "success")
    return redirect(url_for("family_centre.dashboard"))


@family_bp.route("/audit")
@login_required
def audit_trail():
    """Family Centre's own audit trail — matches the Insurance /
    Retirement Timeline pattern, most-recent first."""
    entries = (
        FamilyTimeline.query
        .filter_by(user_id=current_user.id)
        .order_by(FamilyTimeline.created_at.desc())
        .all()
    )
    return render_template("family_centre/audit.html", entries=entries)


@family_bp.route("/tree")
@login_required
def family_tree():
    """Visual family tree — radial diagram, You at the centre."""
    people = _build_people(current_user.id)
    tree_data = _build_tree_data(people)
    return render_template(
        "family_centre/tree.html",
        tree_data=tree_data,
        people_count=len(people),
    )


@family_bp.route("/export/pdf")
@login_required
def export_pdf():
    """Export the Family Centre 'In Case of Emergency' summary PDF
    for the current user only."""
    from . import export as export_module
    return export_module.build_family_pdf(current_user)