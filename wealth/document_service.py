"""
Wealth — Document Vault Service (Phase G)
=============================================
Dedicated service layer for Wealth Documents, kept as its own file —
same organizational choice as history_service.py (Section 90 of the
Phase G spec explicitly suggests "wealth_document_service.py").

CRITICAL: this module NEVER touches financial calculations. It has
no knowledge of Net Worth, Asset values, Liability balances, or
Wealth Snapshots — it only ever reads WealthAsset/WealthLiability
far enough to confirm a document's optional relationship is (a) real
and (b) owned by the same user (Section 8/9/66/67 — uploading,
editing, or deleting a document must have zero financial side
effects).
"""

import os

from .models import (
    WealthDocument, WealthAsset, WealthLiability,
    WealthDocumentCategory, DOCUMENT_TYPES_BY_CATEGORY,
)


# ── Relationship Validation ───────────────────────────────────────────────────

def _validate_relationships(user_id, asset_id, liability_id):
    """
    Both relationships are optional (Section 7/81 — a document may
    belong to neither). If provided, each must be a real record
    owned by this user — never trusted blindly from a form field
    (Section 5: never trust user_id/related ids from the client
    without an ownership-scoped query).

    Returns an error string, or None if valid.
    """
    if asset_id:
        asset = WealthAsset.query.filter_by(id=asset_id, user_id=user_id).first()
        if not asset:
            return "The selected Asset could not be found."
    if liability_id:
        liability = WealthLiability.query.filter_by(id=liability_id, user_id=user_id).first()
        if not liability:
            return "The selected Liability could not be found."
    return None


# ── Upload ─────────────────────────────────────────────────────────────────────

def save_document_metadata(db, user_id, category, document_type,
                           original_name, stored_name, file_path,
                           file_size=None, title=None, description=None,
                           asset_id=None, liability_id=None):
    """
    Record document metadata AFTER the physical file has already been
    saved to disk (Section 17/18 — file save happens first at the
    route layer; this only ever runs once that succeeded). Returns
    (doc, error). On error, NOTHING is written to the database — the
    route is responsible for deleting the already-saved physical file
    in that case, so no orphaned file is left behind (Section 18).

    Uploading has no side effects on Wealth calculations — this
    function touches nothing but the wealth_document table.
    """
    rel_error = _validate_relationships(user_id, asset_id, liability_id)
    if rel_error:
        return None, rel_error

    ext = os.path.splitext(original_name)[1].lower()

    doc = WealthDocument(
        user_id       = user_id,
        asset_id      = asset_id or None,
        liability_id  = liability_id or None,
        category      = category,
        document_type = document_type,
        title         = (title or "").strip() or None,
        description   = (description or "").strip() or None,
        original_name = original_name,
        stored_name   = stored_name,
        file_path     = file_path,
        file_extension= ext,
        file_size     = file_size,
    )
    db.session.add(doc)
    db.session.commit()
    return doc, None


# ── Retrieval ──────────────────────────────────────────────────────────────────

def get_document_or_none(document_id, user_id):
    """
    Ownership-scoped lookup (Section 42 — never Document.query.get()
    followed by an ownership assumption). None if not found or not
    owned by this user; routes turn that into a 404.
    """
    return WealthDocument.query.filter_by(id=document_id, user_id=user_id).first()


def get_vault_documents(user_id, q=None, category=None, document_type=None,
                        asset_id=None, liability_id=None, sort_by="newest"):
    """
    All of a user's Wealth documents, filtered/sorted at the database
    level (Section 49/64 — never load the whole table into Python to
    filter/sort in application code).
    """
    query = WealthDocument.query.filter_by(user_id=user_id)

    if category:
        query = query.filter(WealthDocument.category == category)
    if document_type:
        query = query.filter(WealthDocument.document_type == document_type)
    if asset_id:
        query = query.filter(WealthDocument.asset_id == asset_id)
    if liability_id:
        query = query.filter(WealthDocument.liability_id == liability_id)
    if q:
        like = f"%{q}%"
        query = query.filter(db_or(WealthDocument, like))

    sort_map = {
        "newest":  WealthDocument.uploaded_at.desc(),
        "oldest":  WealthDocument.uploaded_at.asc(),
        "name_az": WealthDocument.title.asc(),
        "name_za": WealthDocument.title.desc(),
        "largest": WealthDocument.file_size.desc(),
        "smallest": WealthDocument.file_size.asc(),
    }
    query = query.order_by(sort_map.get(sort_by, WealthDocument.uploaded_at.desc()))

    return query.all()


def db_or(model, like):
    """Small helper so get_vault_documents stays readable — title OR
    original_name OR description match (Section 20: search by title,
    original filename, description)."""
    from models import db
    return db.or_(
        model.title.ilike(like),
        model.original_name.ilike(like),
        model.description.ilike(like),
    )


def get_documents_for_asset(asset_id, user_id):
    """Documents related to a specific Asset (Section 30) — reused by
    both the Asset detail page and nowhere else, so the relationship
    logic lives in exactly one place."""
    return (WealthDocument.query
            .filter_by(user_id=user_id, asset_id=asset_id)
            .order_by(WealthDocument.uploaded_at.desc())
            .all())


def get_documents_for_liability(liability_id, user_id):
    """Documents related to a specific Liability (Section 31)."""
    return (WealthDocument.query
            .filter_by(user_id=user_id, liability_id=liability_id)
            .order_by(WealthDocument.uploaded_at.desc())
            .all())


def document_count_for_asset(asset_id, user_id):
    """Efficient COUNT(*), not len(query.all()) (Section 34)."""
    return WealthDocument.query.filter_by(user_id=user_id, asset_id=asset_id).count()


def document_count_for_liability(liability_id, user_id):
    return WealthDocument.query.filter_by(user_id=user_id, liability_id=liability_id).count()


def vault_summary(user_id):
    """
    Total document count + per-category counts, for the Vault's
    summary cards (Section 19) and the dashboard card (Section 32) —
    the SAME function backs both, so they can never diverge (mirrors
    Phase F's dashboard_trend_summary() / history page relationship).
    """
    docs = WealthDocument.query.filter_by(user_id=user_id).all()
    by_category = {cat: 0 for cat in WealthDocumentCategory.ALL}
    for d in docs:
        if d.category in by_category:
            by_category[d.category] += 1
    return {
        "total": len(docs),
        "by_category": [{"category": c, "count": n} for c, n in by_category.items() if n > 0],
    }


def dashboard_summary(user_id, recent_count=3):
    """
    Everything the Wealth dashboard's Document Vault card needs
    (Section 32/94). Returns None only when there are zero documents
    at all — mirrors the "don't clutter the dashboard" instruction by
    keeping this to a total + a short recent list, nothing more.
    """
    total = WealthDocument.query.filter_by(user_id=user_id).count()
    if total == 0:
        return None
    recent = (WealthDocument.query.filter_by(user_id=user_id)
             .order_by(WealthDocument.uploaded_at.desc())
             .limit(recent_count).all())
    return {"total": total, "recent": recent}


# ── Update ─────────────────────────────────────────────────────────────────────

def update_document_metadata(db, doc, user_id, title, description,
                              category, document_type, asset_id, liability_id):
    """
    Metadata-only edit (Section 28) — never touches the physical
    file. Re-validates the category/document_type pairing and any
    Asset/Liability relationship exactly as upload does, so an edit
    can't put the record into a state upload would have rejected.
    """
    if doc.user_id != user_id:
        return False, "You do not have permission to edit this document."

    if category not in WealthDocumentCategory.ALL:
        return False, "Invalid document category selected."
    if document_type not in DOCUMENT_TYPES_BY_CATEGORY.get(category, []):
        return False, "Invalid document type for the selected category."

    rel_error = _validate_relationships(user_id, asset_id, liability_id)
    if rel_error:
        return False, rel_error

    doc.title         = (title or "").strip() or None
    doc.description    = (description or "").strip() or None
    doc.category        = category
    doc.document_type   = document_type
    doc.asset_id        = asset_id or None
    doc.liability_id    = liability_id or None
    db.session.commit()
    return True, None


# ── Delete ─────────────────────────────────────────────────────────────────────

def delete_document(db, doc, user_id):
    """
    Remove the document's DATABASE record only (Section 27) — the
    caller (route) is responsible for removing the physical file via
    utils.delete_document_file(), mirroring the exact split already
    established in insurance_centre/routes.py's delete_document():
    file removal and metadata removal are two explicit steps, not
    hidden inside one opaque call, so a failure in either is visible.
    Never touches Assets, Liabilities, Net Worth, or Wealth History —
    this function's only table is wealth_document.
    """
    if doc.user_id != user_id:
        return False, "You do not have permission to delete this document."

    db.session.delete(doc)
    db.session.commit()
    return True, None
