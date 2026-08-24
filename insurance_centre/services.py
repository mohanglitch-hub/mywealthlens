"""
Insurance Centre — Services
============================
Business logic layer. Routes call these functions.
No Flask request/response objects here — pure Python.
"""

from datetime import datetime, date
from models import db
from .models import (
    InsurancePolicy, InsuranceNominee, InsuranceMember,
    InsuranceAddon, InsuranceDocument, InsuranceTimeline,
    PolicyStatus, TimelineEvent, InsuranceCategory,
    InsuranceType, PremiumFrequency
)
from .validators import validate_policy, validate_nominee


# ── Timeline Helper ───────────────────────────────────────────────────────────

def log_timeline(db, policy_id, user_id, event_type, description):
    """Append an entry to the policy timeline. Never overwrites."""
    entry = InsuranceTimeline(
        policy_id   = policy_id,
        user_id     = user_id,
        event_type  = event_type,
        description = description,
    )
    db.session.add(entry)


# ── Policy Services ───────────────────────────────────────────────────────────

def create_policy(db, user_id, data, multi_data=None):
    """
    Create a new insurance policy with related records.
    data:       flat dict (from request.form.to_dict())
    multi_data: multi-value dict (from request.form)
                used for nominees, members, addons (list fields)
    Returns: (policy, None) on success, (None, error_message) on failure
    """
    errors = validate_policy(data, user_id)
    if errors:
        return None, errors[0]

    policy = InsurancePolicy(
        user_id           = user_id,
        category          = data["category"],
        insurance_type    = data["insurance_type"],
        custom_type       = data.get("custom_type", "").strip() or None,
        insurer           = data["insurer"].strip(),
        policy_name       = data.get("policy_name", "").strip() or None,
        policy_number     = data.get("policy_number", "").strip() or None,
        policy_holder     = data.get("policy_holder", "").strip() or None,
        sum_assured       = float(data.get("sum_assured", 0) or 0),
        premium_amount    = float(data.get("premium_amount", 0) or 0),
        premium_frequency = data.get("premium_frequency", PremiumFrequency.YEARLY),
        status            = data.get("status", PolicyStatus.ACTIVE),
        start_date        = _parse_date(data.get("start_date")),
        maturity_date     = _parse_date(data.get("maturity_date")),
        renewal_date      = _parse_date(data.get("renewal_date")),
        expiry_date       = _parse_date(data.get("expiry_date")),
        next_premium_due  = _parse_date(data.get("next_premium_due")),
        vehicle_number     = data.get("vehicle_number", "").strip() or None,
        property_name      = data.get("property_name", "").strip() or None,
        cashless_available = data.get("cashless_available", "").strip() or None,
        claim_history      = data.get("claim_history", "").strip() or None,
        property_type      = data.get("property_type", "").strip() or None,
        policy_type        = data.get("policy_type", "").strip() or None,
        agent_name         = data.get("agent_name", "").strip() or None,
        agent_contact      = data.get("agent_contact", "").strip() or None,
        notes              = data.get("notes", "").strip() or None,
    )
    db.session.add(policy)
    db.session.flush()  # get policy.id before adding related records

    # ── Nominees (Life Insurance) ──────────────────────────────────
    if multi_data and data.get("category") == InsuranceCategory.LIFE:
        names         = multi_data.getlist("nominee_name[]")
        relationships = multi_data.getlist("nominee_relationship[]")
        percentages   = multi_data.getlist("nominee_percentage[]")
        contacts      = multi_data.getlist("nominee_contact[]")

        total_pct = 0
        for i, name in enumerate(names):
            name = name.strip()
            if not name:
                continue
            pct_raw = percentages[i] if i < len(percentages) else ""
            pct = float(pct_raw) if pct_raw else None
            if pct:
                total_pct += pct
                if total_pct > 100:
                    db.session.rollback()
                    return None, f"Total nominee percentage exceeds 100% ({total_pct:.1f}%). Please check nominee shares."
            db.session.add(InsuranceNominee(
                policy_id    = policy.id,
                user_id      = user_id,
                name         = name,
                relationship = relationships[i] if i < len(relationships) else "Other",
                percentage   = pct,
                contact      = contacts[i].strip() if i < len(contacts) else None,
            ))

    # ── Insured Members (Health Insurance) ────────────────────────
    if multi_data and data.get("category") == InsuranceCategory.HEALTH:
        m_names  = multi_data.getlist("member_name[]")
        m_rels   = multi_data.getlist("member_relationship[]")
        m_ages   = multi_data.getlist("member_age[]")

        for i, name in enumerate(m_names):
            name = name.strip()
            if not name:
                continue
            age = m_ages[i] if i < len(m_ages) else ""
            db.session.add(InsuranceMember(
                policy_id    = policy.id,
                user_id      = user_id,
                member_name  = name,
                relationship = m_rels[i] if i < len(m_rels) else "Other",
                age          = int(age) if age and age.isdigit() else None,
            ))

    # ── Motor Add-ons (Motor Insurance) ───────────────────────────
    if multi_data and data.get("category") == InsuranceCategory.MOTOR:
        addons = multi_data.getlist("addons")
        for addon in addons:
            if addon.strip():
                db.session.add(InsuranceAddon(
                    policy_id  = policy.id,
                    user_id    = user_id,
                    addon_name = addon.strip(),
                ))

    # ── Timeline entry ─────────────────────────────────────────────
    log_timeline(db, policy.id, user_id,
                 TimelineEvent.CREATED,
                 f"{policy.insurer} {policy.display_type} added")

    db.session.commit()
    return policy, None


def update_policy(db, policy, user_id, data, multi_data=None):
    """
    Update an existing policy. Logs all changes.
    Handles nominees, members, addons replacement.
    Returns: (policy, None) on success, (None, error) on failure
    """
    errors = validate_policy(data, user_id, existing_policy_id=policy.id)
    if errors:
        return None, errors[0]

    changes = []

    def _track(field, label, formatter=None):
        old_val = getattr(policy, field, None)
        new_raw = data.get(field)
        if new_raw is None:
            return
        if field in ("sum_assured", "premium_amount"):
            new_val = float(new_raw or 0)
        elif "date" in field:
            new_val = _parse_date(new_raw)
        elif isinstance(new_raw, str):
            new_val = new_raw.strip() or None
        else:
            new_val = new_raw

        if str(old_val or "") != str(new_val or ""):
            fmt = formatter or (lambda v: str(v) if v else "—")
            changes.append(f"{label} changed to {fmt(new_val)}")
            setattr(policy, field, new_val)

    fmt_inr = lambda v: f"₹{float(v):,.0f}" if v else "₹0"

    _track("insurer",          "Insurer")
    _track("policy_name",      "Policy Name")
    _track("policy_number",    "Policy Number")
    _track("policy_holder",    "Policy Holder")
    _track("sum_assured",      "Coverage",        fmt_inr)
    _track("premium_amount",   "Premium",         fmt_inr)
    _track("premium_frequency","Premium Frequency")
    _track("status",           "Status")
    _track("start_date",       "Start Date")
    _track("maturity_date",    "Maturity Date")
    _track("renewal_date",     "Renewal Date")
    _track("expiry_date",      "Expiry Date")
    _track("vehicle_number",      "Vehicle Number")
    _track("property_name",       "Property Name")
    _track("agent_name",          "Agent Name")
    _track("agent_contact",       "Agent Contact")
    _track("cashless_available",  "Cashless Available")
    _track("claim_history",       "Claim History")
    _track("property_type",       "Property Type")
    _track("policy_type",         "Policy Type")

    # Custom type
    if data.get("insurance_type") == "Other (Custom)":
        policy.insurance_type = "Other (Custom)"
        new_custom = data.get("custom_type", "").strip() or None
        if new_custom != policy.custom_type:
            changes.append(f"Type: {policy.custom_type} → {new_custom}")
            policy.custom_type = new_custom
    elif data.get("insurance_type"):
        if data["insurance_type"] != policy.insurance_type:
            changes.append(f"Type: {policy.insurance_type} → {data['insurance_type']}")
        policy.insurance_type = data["insurance_type"]
        policy.custom_type = None

    # Notes
    new_notes = data.get("notes", "").strip() or None
    if new_notes != policy.notes:
        policy.notes = new_notes
        log_timeline(db, policy.id, user_id, TimelineEvent.NOTES_UPDATED,
                     "Policy notes updated")

    if changes:
        log_timeline(db, policy.id, user_id, TimelineEvent.COVERAGE_UPDATED,
                     "Updated: " + "; ".join(changes))

    # ── Replace Nominees (Life) ───────────────────────────────────
    if multi_data and data.get("category") == InsuranceCategory.LIFE:
        old_nominees = list(policy.nominees.all())
        policy.nominees.delete()

        names  = multi_data.getlist("nominee_name[]")
        rels   = multi_data.getlist("nominee_relationship[]")
        pcts   = multi_data.getlist("nominee_percentage[]")
        conts  = multi_data.getlist("nominee_contact[]")

        added = []
        for i, name in enumerate(names):
            name = name.strip()
            if not name:
                continue
            pct = pcts[i] if i < len(pcts) else ""
            db.session.add(InsuranceNominee(
                policy_id    = policy.id,
                user_id      = user_id,
                name         = name,
                relationship = rels[i] if i < len(rels) else "Other",
                percentage   = float(pct) if pct else None,
                contact      = conts[i].strip() if i < len(conts) else None,
            ))
            added.append(name)

        if added or old_nominees:
            log_timeline(db, policy.id, user_id, TimelineEvent.NOMINEE_UPDATED,
                         f"Nominees updated: {', '.join(added) if added else 'All removed'}")

    # ── Replace Members (Health) ──────────────────────────────────
    if multi_data and data.get("category") == InsuranceCategory.HEALTH:
        old_members = list(policy.members.all())
        policy.members.delete()

        m_names = multi_data.getlist("member_name[]")
        m_rels  = multi_data.getlist("member_relationship[]")
        m_ages  = multi_data.getlist("member_age[]")

        added = []
        for i, name in enumerate(m_names):
            name = name.strip()
            if not name:
                continue
            age = m_ages[i] if i < len(m_ages) else ""
            db.session.add(InsuranceMember(
                policy_id    = policy.id,
                user_id      = user_id,
                member_name  = name,
                relationship = m_rels[i] if i < len(m_rels) else "Other",
                age          = int(age) if age and str(age).isdigit() else None,
            ))
            added.append(name)

        if added or old_members:
            log_timeline(db, policy.id, user_id, TimelineEvent.MEMBER_UPDATED,
                         f"Members updated: {', '.join(added) if added else 'All removed'}")

    # ── Replace Add-ons (Motor) ───────────────────────────────────
    if multi_data and data.get("category") == InsuranceCategory.MOTOR:
        policy.addons.delete()
        addon_names = multi_data.getlist("addons")
        for addon in addon_names:
            if addon.strip():
                db.session.add(InsuranceAddon(
                    policy_id=policy.id, user_id=user_id,
                    addon_name=addon.strip()))
        log_timeline(db, policy.id, user_id, TimelineEvent.ADDON_UPDATED,
                     f"Add-ons updated: {', '.join(addon_names) or 'None'}")

    db.session.commit()
    return policy, None


def archive_policy(db, policy, user_id):
    """
    Soft delete — marks policy as archived.
    Logs the action. Policy still exists in DB.
    """
    if policy.is_archived:
        return False, "Policy is already archived."

    policy.is_archived  = True
    policy.archived_at  = datetime.utcnow()
    policy.archived_by  = user_id
    policy.status       = PolicyStatus.ARCHIVED

    log_timeline(db, policy.id, user_id,
                 TimelineEvent.ARCHIVED,
                 f"{policy.insurer} {policy.display_type} archived")

    db.session.commit()
    return True, None


def restore_policy(db, policy, user_id):
    """Restore an archived policy back to active."""
    if not policy.is_archived:
        return False, "Policy is not archived."

    policy.is_archived = False
    policy.archived_at = None
    policy.archived_by = None
    policy.status      = PolicyStatus.ACTIVE

    log_timeline(db, policy.id, user_id,
                 TimelineEvent.RESTORED,
                 f"{policy.insurer} {policy.display_type} restored")

    db.session.commit()
    return True, None


def renew_policy(db, policy, user_id, new_renewal_date, new_expiry_date=None):
    """Record a policy renewal — updates dates and logs."""
    old_renewal = policy.renewal_date
    policy.renewal_date = new_renewal_date
    if new_expiry_date:
        policy.expiry_date = new_expiry_date
    policy.status = PolicyStatus.ACTIVE

    log_timeline(db, policy.id, user_id,
                 TimelineEvent.POLICY_RENEWED,
                 f"Policy renewed. Renewal date: {old_renewal} → {new_renewal_date}")

    db.session.commit()
    return policy


# ── Nominee Services ──────────────────────────────────────────────────────────

def add_nominee(db, policy, user_id, data):
    """Add a nominee to a policy. Validates total % does not exceed 100."""
    errors = validate_nominee(data, policy)
    if errors:
        return None, errors[0]

    nominee = InsuranceNominee(
        policy_id    = policy.id,
        user_id      = user_id,
        name         = data["name"].strip(),
        relationship = data["relationship"],
        percentage   = float(data.get("percentage") or 0) or None,
        contact      = data.get("contact","").strip() or None,
    )
    db.session.add(nominee)

    log_timeline(db, policy.id, user_id,
                 TimelineEvent.NOMINEE_UPDATED,
                 f"Nominee added: {nominee.name}")

    db.session.commit()
    return nominee, None


def remove_nominee(db, nominee, user_id):
    policy_id = nominee.policy_id
    name = nominee.name
    db.session.delete(nominee)
    log_timeline(db, policy_id, user_id,
                 TimelineEvent.NOMINEE_UPDATED,
                 f"Nominee removed: {name}")
    db.session.commit()


# ── Health Member Services ────────────────────────────────────────────────────

def add_member(db, policy, user_id, data):
    """Add a health insured member. Only valid for Health Insurance."""
    if policy.category != InsuranceCategory.HEALTH:
        return None, "Members can only be added to Health Insurance policies."

    age = int(data.get("age") or 0)
    if age < 0:
        return None, "Age cannot be negative."

    member = InsuranceMember(
        policy_id    = policy.id,
        user_id      = user_id,
        member_name  = data["member_name"].strip(),
        age          = age or None,
        relationship = data["relationship"],
    )
    db.session.add(member)

    log_timeline(db, policy.id, user_id,
                 TimelineEvent.MEMBER_UPDATED,
                 f"Member added: {member.member_name} ({member.relationship})")

    db.session.commit()
    return member, None


def remove_member(db, member, user_id):
    policy_id = member.policy_id
    name = member.member_name
    db.session.delete(member)
    log_timeline(db, policy_id, user_id,
                 TimelineEvent.MEMBER_UPDATED,
                 f"Member removed: {name}")
    db.session.commit()


# ── Motor Addon Services ──────────────────────────────────────────────────────

def set_addons(db, policy, user_id, addon_names):
    """
    Replace all add-ons for a motor policy.
    addon_names: list of strings
    """
    if policy.category != InsuranceCategory.MOTOR:
        return None, "Add-ons are only valid for Motor Insurance."

    # Remove existing
    policy.addons.delete()

    # Add new
    for name in addon_names:
        if name.strip():
            db.session.add(InsuranceAddon(
                policy_id  = policy.id,
                user_id    = user_id,
                addon_name = name.strip(),
            ))

    log_timeline(db, policy.id, user_id,
                 TimelineEvent.ADDON_UPDATED,
                 f"Add-ons updated: {', '.join(addon_names) or 'None'}")

    db.session.commit()
    return True, None


# ── Document Services ─────────────────────────────────────────────────────────

def save_document_metadata(db, policy, user_id,
                           doc_type, original_name,
                           stored_name, file_path,
                           file_size=None, notes=None, title=None):
    """Record document metadata after file has been saved locally."""
    doc = InsuranceDocument(
        policy_id     = policy.id,
        user_id       = user_id,
        doc_type      = doc_type,
        title         = title,
        original_name = original_name,
        stored_name   = stored_name,
        file_path     = file_path,
        file_size     = file_size,
        notes         = notes,
    )
    db.session.add(doc)

    log_timeline(db, policy.id, user_id,
                 TimelineEvent.DOCUMENT_UPLOADED,
                 f"{doc_type} uploaded: {original_name}")

    db.session.commit()
    return doc


def delete_document(db, doc, user_id):
    """Remove document metadata. Caller must delete the actual file."""
    policy_id = doc.policy_id
    name = doc.original_name
    db.session.delete(doc)
    log_timeline(db, policy_id, user_id,
                 TimelineEvent.DOCUMENT_DELETED,
                 f"Document removed: {name}")
    db.session.commit()


# ── Query Helpers ─────────────────────────────────────────────────────────────

def get_active_policies(user_id):
    """All non-archived policies for a user, ordered by renewal date."""
    return (InsurancePolicy.query
            .filter_by(user_id=user_id, is_archived=False)
            .order_by(InsurancePolicy.renewal_date.asc().nullslast())
            .all())


def get_archived_policies(user_id):
    """All archived policies for a user."""
    return (InsurancePolicy.query
            .filter_by(user_id=user_id, is_archived=True)
            .order_by(InsurancePolicy.archived_at.desc())
            .all())


def get_policies_by_category(user_id, category):
    """Active policies filtered by category."""
    return (InsurancePolicy.query
            .filter_by(user_id=user_id,
                       category=category,
                       is_archived=False)
            .order_by(InsurancePolicy.renewal_date.asc().nullslast())
            .all())


def get_renewals_due(user_id, days=30):
    """Policies with renewal date within the next N days."""
    from datetime import timedelta
    today = date.today()
    cutoff = today + timedelta(days=days)
    return (InsurancePolicy.query
            .filter_by(user_id=user_id, is_archived=False)
            .filter(InsurancePolicy.renewal_date >= today)
            .filter(InsurancePolicy.renewal_date <= cutoff)
            .order_by(InsurancePolicy.renewal_date.asc())
            .all())


def get_overdue_renewals(user_id):
    """Policies with renewal date in the past."""
    return (InsurancePolicy.query
            .filter_by(user_id=user_id, is_archived=False)
            .filter(InsurancePolicy.renewal_date < date.today())
            .order_by(InsurancePolicy.renewal_date.asc())
            .all())


def get_policy_summary(user_id):
    """
    Aggregated summary for dashboard widget.
    Returns dict with counts and totals.
    """
    policies = get_active_policies(user_id)
    total_cover    = sum(p.sum_assured for p in policies)
    total_premium  = sum(p.annual_premium for p in policies)
    due_soon       = len([p for p in policies if p.renewal_status == "due_soon"])
    overdue        = len([p for p in policies if p.renewal_status == "overdue"])
    by_category    = {}
    for p in policies:
        by_category[p.category] = by_category.get(p.category, 0) + 1

    return {
        "total_policies": len(policies),
        "total_cover":    total_cover,
        "total_premium":  total_premium,
        "due_soon":       due_soon,
        "overdue":        overdue,
        "by_category":    by_category,
    }


def search_policies(user_id, query):
    """
    Search policies by policy number, insurer,
    policy holder, vehicle number, property name, custom type.
    """
    q = f"%{query}%"
    return (InsurancePolicy.query
            .filter_by(user_id=user_id, is_archived=False)
            .filter(
                db.or_(
                    InsurancePolicy.policy_number.ilike(q),
                    InsurancePolicy.insurer.ilike(q),
                    InsurancePolicy.policy_holder.ilike(q),
                    InsurancePolicy.vehicle_number.ilike(q),
                    InsurancePolicy.property_name.ilike(q),
                    InsurancePolicy.custom_type.ilike(q),
                    InsurancePolicy.policy_name.ilike(q),
                )
            )
            .order_by(InsurancePolicy.updated_at.desc())
            .all())


# ── Private Helpers ───────────────────────────────────────────────────────────

def _parse_date(value):
    """Parse date string to date object. Returns None if empty/invalid."""
    if not value:
        return None
    try:
        if isinstance(value, date):
            return value
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# ── Dashboard Statistics Services ─────────────────────────────────────────────

def get_active_policy_count(user_id):
    """Count of all active (non-archived) policies."""
    try:
        return InsurancePolicy.query.filter_by(
            user_id=user_id, is_archived=False).count()
    except Exception:
        return 0


def get_total_coverage(user_id):
    """Sum of sum_assured across all active policies."""
    try:
        from sqlalchemy import func
        from models import db
        result = db.session.query(
            func.sum(InsurancePolicy.sum_assured)
        ).filter_by(user_id=user_id, is_archived=False).scalar()
        return result or 0
    except Exception:
        return 0


def get_total_annual_premium(user_id):
    """
    Sum of annual premium equivalents across all active policies.
    Converts all frequencies to annual.
    """
    try:
        policies = InsurancePolicy.query.filter_by(
            user_id=user_id, is_archived=False).all()
        return sum(p.annual_premium for p in policies)
    except Exception:
        return 0


def get_upcoming_renewals(user_id, days=30):
    """Count of policies with renewal due within N days."""
    try:
        return len(get_renewals_due(user_id, days))
    except Exception:
        return 0


def get_total_documents(user_id):
    """Count of all uploaded documents across all policies."""
    try:
        from insurance_centre.models import InsuranceDocument
        return InsuranceDocument.query.filter_by(user_id=user_id).count()
    except Exception:
        return 0


def get_category_stats(user_id):
    """
    Per-category counts and coverage for the category cards.
    Returns list of dicts ordered by InsuranceCategory.ALL.
    """
    try:
        policies = InsurancePolicy.query.filter_by(
            user_id=user_id, is_archived=False).all()
        stats = {}
        for cat in InsuranceCategory.ALL:
            cat_policies = [p for p in policies if p.category == cat]
            stats[cat] = {
                "category":    cat,
                "count":       len(cat_policies),
                "coverage":    sum(p.sum_assured for p in cat_policies),
                "premium":     sum(p.annual_premium for p in cat_policies),
                "has_policies": len(cat_policies) > 0,
            }
        return stats
    except Exception:
        return {cat: {"category": cat, "count": 0, "coverage": 0,
                      "premium": 0, "has_policies": False}
                for cat in InsuranceCategory.ALL}


def get_recent_policies(user_id, limit=5):
    """Most recently created active policies."""
    try:
        return (InsurancePolicy.query
                .filter_by(user_id=user_id, is_archived=False)
                .order_by(InsurancePolicy.created_at.desc())
                .limit(limit)
                .all())
    except Exception:
        return []


def get_recent_activity(user_id, limit=7):
    """Latest N timeline entries across all policies."""
    try:
        from insurance_centre.models import InsuranceTimeline
        return (InsuranceTimeline.query
                .filter_by(user_id=user_id)
                .order_by(InsuranceTimeline.created_at.desc())
                .limit(limit)
                .all())
    except Exception:
        return []


def get_dashboard_data(user_id):
    """
    Single call to get all dashboard data.
    Minimises database round trips.
    """
    try:
        policies       = get_active_policies(user_id)
        renewals_soon  = get_renewals_due(user_id, days=30)
        overdue        = get_overdue_renewals(user_id)
        recent         = get_recent_policies(user_id, limit=5)
        activity       = get_recent_activity(user_id, limit=8)
        category_stats = get_category_stats(user_id)
        doc_count      = get_total_documents(user_id)

        total_coverage = sum(p.sum_assured for p in policies)
        total_premium  = sum(p.annual_premium for p in policies)

        return {
            "policy_count":    len(policies),
            "total_coverage":  total_coverage,
            "total_premium":   total_premium,
            "renewals_soon":   len(renewals_soon),
            "overdue_count":   len(overdue),
            "doc_count":       doc_count,
            "recent_policies": recent,
            "recent_activity": activity,
            "category_stats":  category_stats,
            "has_policies":    len(policies) > 0,
            "overdue_policies": overdue,
            "due_soon_policies": renewals_soon,
        }
    except Exception as e:
        import logging
        logging.error(f"Dashboard data error for user {user_id}: {e}")
        return {
            "policy_count": 0, "total_coverage": 0, "total_premium": 0,
            "renewals_soon": 0, "overdue_count": 0, "doc_count": 0,
            "recent_policies": [], "recent_activity": [],
            "category_stats": {cat: {"category": cat, "count": 0,
                "coverage": 0, "premium": 0, "has_policies": False}
                for cat in InsuranceCategory.ALL},
            "has_policies": False,
            "overdue_policies": [], "due_soon_policies": [],
        }


# ── Phase 5 Service Methods ───────────────────────────────────────────────────

def get_policy_with_related(policy_id, user_id):
    """
    Load a single policy with all related data in one call.
    Validates ownership. Returns None if not found or unauthorized.
    """
    try:
        policy = InsurancePolicy.query.filter_by(
            id=policy_id, user_id=user_id
        ).first()
        if not policy:
            return None

        return {
            "policy":    policy,
            "nominees":  policy.nominees.all(),
            "members":   policy.members.all(),
            "addons":    policy.addons.all(),
            "documents": policy.documents.order_by(
                             InsuranceDocument.uploaded_at.desc()).all(),
            "timeline":  policy.timeline.order_by(
                             InsuranceTimeline.created_at.desc()).limit(20).all(),
        }
    except Exception as e:
        import logging
        logging.error(f"get_policy_with_related error: {e}")
        return None


def get_all_active_policies_for_listing(user_id):
    """
    Return all active policies for the listing page.
    Ordered by category then renewal date.
    Includes only fields needed for list cards — efficient.
    """
    try:
        return (InsurancePolicy.query
                .filter_by(user_id=user_id, is_archived=False)
                .order_by(
                    InsurancePolicy.category.asc(),
                    InsurancePolicy.renewal_date.asc().nullslast()
                )
                .all())
    except Exception:
        return []


# ── Phase 6: Archive page data ────────────────────────────────────────────────

def get_archived_policy_count(user_id):
    """Count of archived policies."""
    try:
        return InsurancePolicy.query.filter_by(
            user_id=user_id, is_archived=True).count()
    except Exception:
        return 0


# ════════════════════════════════════════════════════════════════════
# INSURANCE STATISTICS SERVICE (Phase 8)
# ════════════════════════════════════════════════════════════════════

class InsuranceStatisticsService:
    """
    Centralised statistics for the Insurance Centre dashboard.
    All dashboard metrics should come through this service.
    """

    def __init__(self, user_id):
        self.user_id = user_id
        self._policies = None  # cached

    def _get_policies(self):
        if self._policies is None:
            self._policies = InsurancePolicy.query.filter_by(
                user_id=self.user_id, is_archived=False).all()
        return self._policies

    def active_count(self):
        try:
            return len(self._get_policies())
        except Exception:
            return 0

    def total_coverage(self):
        try:
            return sum(p.sum_assured for p in self._get_policies())
        except Exception:
            return 0

    def total_annual_premium(self):
        try:
            return sum(p.annual_premium for p in self._get_policies())
        except Exception:
            return 0

    def upcoming_renewals_count(self, days=30):
        try:
            return len([p for p in self._get_policies()
                       if p.renewal_status in ("due_soon", "overdue")])
        except Exception:
            return 0

    def overdue_count(self):
        try:
            return len([p for p in self._get_policies()
                       if p.renewal_status == "overdue"])
        except Exception:
            return 0

    def document_count(self):
        try:
            from insurance_centre.models import InsuranceDocument
            return InsuranceDocument.query.filter_by(
                user_id=self.user_id).count()
        except Exception:
            return 0

    def category_totals(self):
        try:
            result = {}
            for cat in InsuranceCategory.ALL:
                cat_policies = [p for p in self._get_policies()
                               if p.category == cat]
                result[cat] = {
                    "count":    len(cat_policies),
                    "coverage": sum(p.sum_assured for p in cat_policies),
                    "premium":  sum(p.annual_premium for p in cat_policies),
                    "has_policies": len(cat_policies) > 0,
                }
            return result
        except Exception:
            return {cat: {"count":0,"coverage":0,"premium":0,"has_policies":False}
                    for cat in InsuranceCategory.ALL}

    def upcoming_renewals_list(self, limit=5):
        """Next N policies due for renewal — for the Renewal Centre widget."""
        try:
            from datetime import date, timedelta
            today  = date.today()
            cutoff = today + timedelta(days=90)
            due = [p for p in self._get_policies()
                   if p.renewal_date and p.renewal_date <= cutoff]
            due.sort(key=lambda p: p.renewal_date or date.max)
            return due[:limit]
        except Exception:
            return []

    def recent_activity(self, limit=7):
        """Latest N timeline activities regardless of date."""
        try:
            from insurance_centre.models import InsuranceTimeline
            events = (InsuranceTimeline.query
                     .filter_by(user_id=self.user_id)
                     .order_by(InsuranceTimeline.created_at.desc())
                     .limit(limit)
                     .all())
            return events
        except Exception:
            return []

    def summary_dict(self):
        """Single call for all dashboard data."""
        return {
            "policy_count":    self.active_count(),
            "total_coverage":  self.total_coverage(),
            "total_premium":   self.total_annual_premium(),
            "renewals_soon":   self.upcoming_renewals_count(),
            "overdue_count":   self.overdue_count(),
            "doc_count":       self.document_count(),
            "category_stats":  self.category_totals(),
            "renewal_centre":  self.upcoming_renewals_list(limit=5),
            "recent_activity": self.recent_activity(limit=7),
            "recent_policies": get_recent_policies(self.user_id, limit=5),
            "has_policies":    self.active_count() > 0,
        }
# ── Document Vault Services ──────────────────────────────────────────────────
# Add these two functions to services.py, right after delete_document().

def get_vault_documents(user_id, q=None, category=None, doc_type=None):
    """
    All documents across every one of the user's insurance policies,
    joined with policy info for filtering/display. Attaches ._policy
    to each document (mirrors the existing pattern already used in
    routes.py's document_vault route).
    """
    query = InsuranceDocument.query.filter_by(user_id=user_id)
    if doc_type:
        query = query.filter(InsuranceDocument.doc_type == doc_type)
    docs = query.order_by(InsuranceDocument.uploaded_at.desc()).all()

    result = []
    for d in docs:
        policy = InsurancePolicy.query.filter_by(
            id=d.policy_id, user_id=user_id).first()
        d._policy = policy

        if category and (not policy or policy.category != category):
            continue

        if q:
            ql = q.lower()
            name = (d.title or d.original_name or "").lower()
            hay = " ".join(filter(None, [
                name,
                (d.doc_type or "").lower(),
                (policy.insurer if policy else "").lower(),
                (policy.display_type if policy else "").lower(),
            ]))
            if ql not in hay:
                continue

        result.append(d)
    return result


def vault_summary(user_id):
    """Total document count + per-category counts for the Vault's
    summary cards. Never fabricated — counts real rows only."""
    docs = InsuranceDocument.query.filter_by(user_id=user_id).all()
    by_category = {cat: 0 for cat in InsuranceCategory.ALL}
    for d in docs:
        policy = InsurancePolicy.query.filter_by(
            id=d.policy_id, user_id=user_id).first()
        if policy and policy.category in by_category:
            by_category[policy.category] += 1
    return {
        "total": len(docs),
        "by_category": [{"category": c, "count": n} for c, n in by_category.items()],
    }