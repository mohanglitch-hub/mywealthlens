"""
Session Management — "log out other devices" (Sep 2026, Batch 3)
====================================================================
Flask's session cookie is signed but held entirely client-side by
default: once issued, the server has no way to invalidate it early.
That's fine for normal use, but it means there was previously no way
to answer "what's logged into my account right now?" or to force a
stolen/forgotten session to log out remotely.

This module adds a thin server-side layer on top of Flask-Login:
every successful login writes one UserSession row (see models.py) and
puts its random `session_token` into the browser's own session
cookie. app.py's before_request hook then confirms that token still
has a live row before treating the request as authenticated --
deleting the row (see revoke_session/revoke_other_sessions) is what
actually ends that device's session, on its very next request.

Nothing here touches WHO is allowed to log in (that's still
email+password, optionally +2FA — see twofa.py). This only tracks and
can end sessions that already exist.
"""
import re
import secrets
from datetime import datetime, timedelta

from flask import session as flask_session, request

from models import db, UserSession

SESSION_TOKEN_BYTES = 32

# Only touch last_seen_at this often per session, rather than on every
# single request -- an UPDATE on every page load would be wasteful for
# no real benefit (the "last seen" display only needs minute-level
# accuracy, not per-request).
LAST_SEEN_UPDATE_INTERVAL = timedelta(minutes=5)


def create_session(user):
    """
    Called right after login_user()/signup succeeds (and after a 2FA
    challenge passes, if the account has one). Generates a fresh
    random token, stores it in the browser's session cookie, and
    records a matching UserSession row. Returns the token (rarely
    needed by callers -- it's already in flask.session['sid']).
    """
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    flask_session['sid'] = token
    row = UserSession(
        user_id=user.id,
        session_token=token,
        user_agent=(request.headers.get('User-Agent', '') or '')[:255],
        ip_address=(request.remote_addr or '')[:64],
        created_at=datetime.utcnow(),
        last_seen_at=datetime.utcnow(),
    )
    db.session.add(row)
    db.session.commit()
    return token


def validate_and_touch(user_id):
    """
    Called from app.py's before_request for every authenticated
    request. Returns True if the browser's session token still has a
    live UserSession row for this user (and refreshes last_seen_at,
    at most every LAST_SEEN_UPDATE_INTERVAL) -- False means this
    session was revoked (from another device, or logged out) and the
    caller should log the request out.

    A request with NO token at all (e.g. a session predating this
    feature, or a non-browser API-style call) is treated as valid --
    this is additive tracking, not a hard requirement to have a
    UserSession row, so upgrading existing logged-in users doesn't log
    everyone out at once.
    """
    token = flask_session.get('sid')
    if not token:
        return True

    row = UserSession.query.filter_by(user_id=user_id, session_token=token).first()
    if row is None:
        return False

    now = datetime.utcnow()
    if now - row.last_seen_at >= LAST_SEEN_UPDATE_INTERVAL:
        row.last_seen_at = now
        db.session.commit()
    return True


def end_current_session(user_id):
    """Called from /logout — removes this device's own UserSession row
    (in addition to Flask-Login's logout_user())."""
    token = flask_session.get('sid')
    if token:
        UserSession.query.filter_by(user_id=user_id, session_token=token).delete()
        db.session.commit()
    flask_session.pop('sid', None)


def current_session_token():
    return flask_session.get('sid')


def list_sessions(user_id):
    """Newest-active-first, for the Preferences > Security list."""
    return (UserSession.query
            .filter_by(user_id=user_id)
            .order_by(UserSession.last_seen_at.desc())
            .all())


def revoke_session(user_id, session_row_id):
    """
    Deletes one specific session by its UserSession.id, scoped to
    user_id so one account can never revoke another's session.
    Returns True if a row was actually deleted, and whether it was
    the CALLER's own current session (the caller then needs to also
    log itself out immediately, rather than waiting for the next
    request's before_request check).
    """
    row = UserSession.query.filter_by(id=session_row_id, user_id=user_id).first()
    if row is None:
        return False, False
    was_current = (row.session_token == flask_session.get('sid'))
    db.session.delete(row)
    db.session.commit()
    return True, was_current


def revoke_other_sessions(user_id):
    """Deletes every session for user_id EXCEPT the caller's current
    one. Returns the number of sessions revoked."""
    current_token = flask_session.get('sid')
    q = UserSession.query.filter(UserSession.user_id == user_id)
    if current_token:
        q = q.filter(UserSession.session_token != current_token)
    count = q.count()
    q.delete(synchronize_session=False)
    db.session.commit()
    return count


# ── Lightweight device/browser labeling ──
# Deliberately not a dependency (no user-agents/ua-parser package) --
# just enough regex matching to turn "Mozilla/5.0 (Windows NT 10.0;
# Win64; x64) ... Chrome/128.0 ..." into "Chrome on Windows", which is
# all the Security page needs to help someone recognize (or not
# recognize) a session.
_BROWSER_PATTERNS = [
    (re.compile(r'Edg/'), 'Edge'),
    (re.compile(r'OPR/|Opera'), 'Opera'),
    (re.compile(r'Chrome/'), 'Chrome'),
    (re.compile(r'CriOS/'), 'Chrome'),
    (re.compile(r'FxiOS/'), 'Firefox'),
    (re.compile(r'Firefox/'), 'Firefox'),
    (re.compile(r'Safari/'), 'Safari'),
]
_OS_PATTERNS = [
    (re.compile(r'Windows'), 'Windows'),
    (re.compile(r'Mac OS X|Macintosh'), 'macOS'),
    (re.compile(r'iPhone|iPad|iOS'), 'iOS'),
    (re.compile(r'Android'), 'Android'),
    (re.compile(r'Linux'), 'Linux'),
]


def describe_device(user_agent):
    """'Chrome on Windows', 'Safari on iOS', or a generic fallback for
    an empty/unrecognized user-agent string."""
    ua = user_agent or ''
    browser = next((name for pat, name in _BROWSER_PATTERNS if pat.search(ua)), None)
    os_name = next((name for pat, name in _OS_PATTERNS if pat.search(ua)), None)
    if browser and os_name:
        return f"{browser} on {os_name}"
    if browser:
        return browser
    if os_name:
        return f"Unknown browser on {os_name}"
    return "Unknown device"
