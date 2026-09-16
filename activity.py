"""
Unified Activity Feed
========================
Merges "recent activity" from all four modules (Wealth, Insurance,
Retirement, Family Centre) into one chronological, cross-module feed
for the main Dashboard — replacing the four separate per-module
"Recent Activity" widgets (and their per-item detail-page equivalents
— Insurance's Policy Timeline, Retirement's scheme-level Activity
section) that used to exist, plus Family Centre's separate Audit
Trail page, all now folded into this one place.

Deliberately does NOT introduce a new shared database table. Each
module already has its own way of producing recent-activity data —
Wealth derives it from existing timestamps, Insurance/Retirement/
Family Centre each log to their own append-only Timeline table. This
module doesn't touch any of that; it just calls each module's
existing function, normalizes the shape, and merges + sorts the
result. Insurance and Retirement Centre stay untouched beyond the
data their own get_recent_activity() functions already returned —
no other UI or feature change to either module itself.

Category colours are intentionally text-only (colour on the word,
no background pill) — a background-tinted label looks wrong once the
app supports dark mode, since a light pastel background barely reads
against a dark card. Plain coloured text has no such problem.
"""
from flask import url_for

CATEGORY_COLORS = {
    "created": "var(--up)",
    "updated": "#D97706",
    "removed": "var(--down)",
    "document": "#7C3AED",
}


def categorize(event_type):
    """
    Buckets the differently-worded event_type strings from each
    module ("Policy Created", "Asset Added", "Person Added", ...)
    into one shared, consistently-coloured set of four categories.
    "document" is checked first and wins regardless of the verb used
    ("Document Uploaded" is a document event, not a "created" event)
    since distinguishing "a file was touched" from "a data field
    changed" is the actual point of the colour coding.
    """
    text = (event_type or "").lower()
    if "document" in text:
        return "document"
    if "created" in text or "added" in text:
        return "created"
    if "removed" in text or "deleted" in text or "archived" in text:
        return "removed"
    return "updated"


def _normalize(module, module_icon, event_type, description, timestamp, link):
    category = categorize(event_type)
    return {
        "module": module,
        "module_icon": module_icon,
        "event_type": event_type,
        "description": description,
        "timestamp": timestamp,
        "link": link,
        "category": category,
        "color": CATEGORY_COLORS[category],
    }


def get_unified_activity(user_id, days=10):
    """
    Returns every activity across all four modules from the last
    `days` days, newest first. Callers decide how much of this to
    show — the Dashboard widget shows the first 5, the full Activity
    page shows all of it.
    """
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)

    events = []

    # ── Wealth ──────────────────────────────────────────────────────
    from wealth.services import get_recent_activity as wealth_activity
    for e in wealth_activity(user_id, limit=50):
        link = None
        if e.get("entity_kind") == "asset":
            link = url_for("wealth.asset_detail", asset_id=e["entity_id"])
        elif e.get("entity_kind") == "liability":
            link = url_for("wealth.liability_detail", liability_id=e["entity_id"])
        elif e.get("entity_kind") == "document":
            link = url_for("wealth.document_detail", document_id=e["entity_id"])
        elif e.get("entity_kind") == "snapshot":
            link = url_for("wealth.snapshot_detail", snapshot_id=e["entity_id"])
        events.append(_normalize(
            "Wealth", "💰", e["event_type"], e["description"], e["timestamp"], link,
        ))

    # ── Insurance ───────────────────────────────────────────────────
    from insurance_centre.services import get_recent_activity as insurance_activity
    for e in insurance_activity(user_id, limit=50):
        link = url_for("insurance_centre.policy_detail", policy_id=e.policy_id)
        events.append(_normalize(
            "Insurance", "🛡️", e.event_type, e.description, e.created_at, link,
        ))

    # ── Retirement ──────────────────────────────────────────────────
    from retirement_centre.services import recent_activity as retirement_activity
    for e in retirement_activity(user_id, limit=50):
        link = url_for("retirement_centre.scheme_detail", scheme_id=e.scheme_id)
        events.append(_normalize(
            "Retirement", "🏦", e.event_type, e.description, e.created_at, link,
        ))

    # ── Family Centre ───────────────────────────────────────────────
    from family_centre.services import get_recent_activity as family_activity
    for e in family_activity(user_id, limit=50):
        link = url_for("family_centre.dashboard")
        events.append(_normalize(
            "Family", "👪", e.event_type, e.description, e.created_at, link,
        ))

    events = [e for e in events if e["timestamp"] >= cutoff]
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    return events
