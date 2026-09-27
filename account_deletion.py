"""
Account Deletion — Shared Cascade-Delete Logic (Sep 2026, Batch 3)
========================================================================
The single source of truth for "delete everything tied to one user_id,
across every module" — used by BOTH the self-service web route
(app.py's /account/delete) and the admin CLI tool (delete_user.py).
Previously this logic only existed inline inside delete_user.py (and
a separate, already-stale copy of the same table list in
inventory_user.py) — a real risk, since a table list that isn't kept
in one place drifts out of date exactly like this one had (see below).

Audit note (Sep 2026): before this rewrite, delete_user.py and
inventory_user.py both referenced three tables — `family`,
`family_member`, `family_invite` — that no longer exist (they were
the old, abandoned multi-user account-sharing feature; new Family
Centre uses `family_person`/`family_timeline` instead, per the
project's own history). Running either script would have thrown a
real "no such table" error. Also missing several tables added since:
`mutual_fund_transaction`, `stock_transaction`, `wealth_asset_heir`,
`notification_log`, `goal_holding_link` (see below), `family_person`,
`family_timeline`. This module fixes all of that by deriving the
table list from what's actually in this schema right now, in one
place both callers import.

SQLite note: this project has never enabled SQLite foreign-key
enforcement (confirmed by prior audit), so ondelete='CASCADE' on a
model's ForeignKey is NOT relied upon here — every table is deleted
explicitly, children before parents, exactly like delete_user.py's
original approach.
"""
import os
import shutil
from datetime import datetime


# Tables with their own direct `user_id` column — every child table
# added since Insurance/Retirement's original build now carries
# user_id directly (not just policy_id/scheme_id), so these can all
# be deleted by user_id in one pass, no policy/scheme lookup needed.
DIRECT_TABLES = [
    "mutual_fund", "mutual_fund_transaction",
    "stock", "stock_transaction",
    "goal", "user_profile",
    "net_worth_history",
    "family_person", "family_timeline",
    "wealth_asset", "wealth_asset_heir", "wealth_liability",
    "wealth_value_snapshot", "wealth_snapshot", "wealth_snapshot_log",
    "wealth_document",
    "insurance_policy", "insurance_nominee", "insurance_member",
    "insurance_addon", "insurance_document", "insurance_timeline",
    "retirement_scheme", "retirement_contribution",
    "retirement_scheme_nominee", "retirement_document", "retirement_timeline",
    "cashflow_transaction", "cashflow_budget", "cashflow_recurring_payment",
    "notification_log",
    "user_session", "backup_code",
]

# goal_holding_link has no user_id column of its own (it links a Goal
# to a holding, see models.py's GoalHoldingLink docstring) — must be
# deleted via a subquery on goal_id BEFORE the goal table itself, or
# it's left orphaned (SQLite FK enforcement is off, so nothing else
# would catch this).
GOAL_ID_TABLES = ["goal_holding_link"]

# fx_rate_cache is deliberately NOT here — it's keyed by currency, not
# user_id (shared across every user, see models.py's FxRateCache
# docstring). Nothing to delete there for one account.


def backup_db(app):
    """Copies the live SQLite file aside before any destructive change
    — same safety net delete_user.py has always had. Returns the
    backup path, or None if the DB isn't SQLite or the file can't be
    located (caller decides whether that's fatal)."""
    db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not db_uri.startswith("sqlite:///"):
        return None
    db_path = db_uri.replace("sqlite:///", "", 1)
    if not os.path.isabs(db_path):
        db_path = os.path.join(app.instance_path, db_path)
    if not os.path.exists(db_path):
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(
        os.path.dirname(db_path), f"mywealthlens_pre_user_delete_backup_{ts}.db")
    shutil.copy2(db_path, backup_path)
    return backup_path


def wipe_document_files(app, db, user_id):
    """Removes uploaded document files on disk — never touched by a
    database-level deletion alone. Returns a list of removed dir
    paths, for reporting."""
    from sqlalchemy import text
    removed = []

    wealth_dir = os.path.join(app.instance_path, "documents", "wealth", str(user_id))
    if os.path.isdir(wealth_dir):
        shutil.rmtree(wealth_dir)
        removed.append(wealth_dir)

    policy_ids = [r[0] for r in db.session.execute(
        text("SELECT id FROM insurance_policy WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]
    for pid in policy_ids:
        d = os.path.join(app.instance_path, "documents", "insurance", str(pid))
        if os.path.isdir(d):
            shutil.rmtree(d)
            removed.append(d)

    scheme_ids = [r[0] for r in db.session.execute(
        text("SELECT id FROM retirement_scheme WHERE user_id = :uid"),
        {"uid": user_id}).fetchall()]
    for sid in scheme_ids:
        d = os.path.join(app.instance_path, "documents", "retirement", str(sid))
        if os.path.isdir(d):
            shutil.rmtree(d)
            removed.append(d)

    return removed


def cascade_delete_user(app, db, user_id):
    """
    Deletes every row tied to `user_id`, across every table, children
    before parents, then the user row itself. Does NOT create a
    backup or touch document files — callers that want those call
    backup_db()/wipe_document_files() themselves first, since a CLI
    tool and a self-service web route want different messaging/UX
    around them (see delete_user.py and app.py's /account/delete).

    Returns {table_name: rows_deleted, ...} plus "user": 1 on success.
    Raises if the user_id doesn't exist — callers check first.
    """
    from sqlalchemy import text

    summary = {}

    for table in GOAL_ID_TABLES:
        n = db.session.execute(text(
            f"DELETE FROM {table} WHERE goal_id IN "
            f"(SELECT id FROM goal WHERE user_id = :uid)"
        ), {"uid": user_id}).rowcount
        if n:
            summary[table] = n

    for table in DIRECT_TABLES:
        n = db.session.execute(
            text(f"DELETE FROM {table} WHERE user_id = :uid"), {"uid": user_id}).rowcount
        if n:
            summary[table] = n

    db.session.execute(text("DELETE FROM user WHERE id = :uid"), {"uid": user_id})
    db.session.commit()
    summary["user"] = 1
    return summary
