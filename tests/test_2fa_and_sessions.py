"""
End-to-end test for Two-Factor Authentication and Session Management
(Batch 3, Sep 2026).

Covers:
  1. 2FA setup: QR/secret shown, wrong code rejected, correct code
     enables 2FA and issues 10 backup codes.
  2. Login now requires the second factor: password alone lands on
     the 2FA step, not the dashboard.
  3. A wrong code at the login 2FA step is rejected.
  4. A correct TOTP code at login completes authentication.
  5. A backup code works exactly once (second use is rejected).
  6. Session management: two "devices" (test clients) logged in
     simultaneously both show up in the active-sessions list; revoking
     one logs only that one out (the other keeps working); "log out
     all other devices" leaves only the caller's session alive.
  7. Disabling 2FA requires password + code, and turns login back to
     single-factor.

Run from the project root:
    py tests\\test_2fa_and_sessions.py       (Windows)
    python3 tests/test_2fa_and_sessions.py   (Mac/Linux)

Runs against a disposable scratch copy of the project (see
_scratch_env.py) -- never your real instance/mywealthlens.db. Exits
with status 0 if every check passes, 1 otherwise.
"""
import re
import sys
import os

import pyotp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_env import run_in_scratch

TEST_EMAIL = "twofa_session_test@example.com"
PASSWORD = "TestPass123!"


def get_csrf(page_bytes):
    m = re.search(rb'name="csrf_token" value="([^"]+)"', page_bytes)
    return m.group(1).decode() if m else None


def main():
    from app import app, db, limiter
    from models import User, BackupCode, UserSession

    def reset_rate_limits():
        # This test's test-client requests all share one IP (127.0.0.1),
        # so the real per-IP brute-force rate limits on /login and
        # /login/2fa (correctly strict -- 5 per 15 min) would otherwise
        # trip from the test's own repeated logins long before any real
        # bug. Reset between phases so the test isolates 2FA/session
        # *logic*, not the rate limiter (which has its own coverage).
        limiter.storage.reset()

    def new_client():
        return app.test_client()

    def signup(client, email, name):
        page = client.get('/signup')
        r = client.post('/signup', data={
            'csrf_token': get_csrf(page.data),
            'name': name, 'email': email,
            'password': PASSWORD, 'confirm_password': PASSWORD,
        }, follow_redirects=True)
        assert r.status_code == 200, r.status_code

    def login(client, email, expect_2fa=False):
        page = client.get('/login')
        r = client.post('/login', data={
            'csrf_token': get_csrf(page.data),
            'email': email, 'password': PASSWORD,
        }, follow_redirects=True)
        assert r.status_code == 200, r.status_code
        body = r.get_data(as_text=True)
        if expect_2fa:
            assert 'Enter your authentication code' in body, "expected to land on 2FA step"
        return r

    def submit_2fa(client, code):
        page = client.get('/login/2fa')
        csrf = get_csrf(page.data)
        return client.post('/login/2fa', data={'csrf_token': csrf, 'code': code}, follow_redirects=True)

    # ── Setup: clean slate ──
    with app.app_context():
        existing = User.query.filter_by(email=TEST_EMAIL).first()
        if existing:
            BackupCode.query.filter_by(user_id=existing.id).delete()
            UserSession.query.filter_by(user_id=existing.id).delete()
            db.session.delete(existing)
            db.session.commit()

    client_a = new_client()
    signup(client_a, TEST_EMAIL, "2FA Session Test")
    with app.app_context():
        user = User.query.filter_by(email=TEST_EMAIL).first()
        user_id = user.id
        assert user.totp_enabled is False
    print(f"PASS: signed up test user (id={user_id}), 2FA off by default")

    # ── 1. Start 2FA setup, reject wrong code, confirm with right code ──
    setup_page = client_a.get('/account/2fa/setup')
    assert setup_page.status_code == 200
    setup_csrf = get_csrf(setup_page.data)
    with app.app_context():
        secret = User.query.get(user_id).totp_secret
        assert secret, "secret should be generated on GET /account/2fa/setup"

    r = client_a.post('/account/2fa/setup', data={'csrf_token': setup_csrf, 'code': '000000'}, follow_redirects=True)
    with app.app_context():
        assert User.query.get(user_id).totp_enabled is False, "wrong code must not enable 2FA"
    print("PASS: wrong setup code rejected, 2FA still off")

    totp = pyotp.TOTP(secret)
    valid_code = totp.now()
    r = client_a.post('/account/2fa/setup', data={'csrf_token': setup_csrf, 'code': valid_code}, follow_redirects=True)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Two-Factor Authentication Enabled' in body

    backup_codes = list(dict.fromkeys(re.findall(r'[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}', body)))
    assert len(backup_codes) == 10, f"expected 10 unique backup codes, got {len(backup_codes)}"
    with app.app_context():
        u = User.query.get(user_id)
        assert u.totp_enabled is True
        assert BackupCode.query.filter_by(user_id=user_id).count() == 10
    print(f"PASS: correct code enabled 2FA, got {len(backup_codes)} backup codes")

    # ── 2. Log out, log back in — should now require 2FA ──
    reset_rate_limits()
    client_a.get('/logout', follow_redirects=True)
    login(client_a, TEST_EMAIL, expect_2fa=True)

    # ── 3. Wrong code at login 2FA step ──
    r = submit_2fa(client_a, '000000')
    assert 'Invalid authentication code' in r.get_data(as_text=True)
    r_dash = client_a.get('/dashboard', follow_redirects=False)
    assert r_dash.status_code in (302, 401), "must NOT be logged in after wrong 2FA code"
    print("PASS: wrong login 2FA code rejected, not authenticated")

    # ── 4. Correct TOTP code completes login ──
    r = submit_2fa(client_a, totp.now())
    r_dash = client_a.get('/dashboard')
    assert r_dash.status_code == 200, "must be logged in after correct 2FA code"
    print("PASS: correct login 2FA code completes authentication")

    # ── 5. Backup code works once, then is rejected on reuse ──
    reset_rate_limits()
    client_a.get('/logout', follow_redirects=True)
    login(client_a, TEST_EMAIL, expect_2fa=True)
    first_backup_code = backup_codes[0]
    r = submit_2fa(client_a, first_backup_code)
    r_dash = client_a.get('/dashboard')
    assert r_dash.status_code == 200, "backup code should log the user in"
    print("PASS: backup code logs the user in")

    reset_rate_limits()
    client_a.get('/logout', follow_redirects=True)
    login(client_a, TEST_EMAIL, expect_2fa=True)
    r = submit_2fa(client_a, first_backup_code)
    assert 'Invalid authentication code' in r.get_data(as_text=True), "used backup code must be rejected on reuse"
    r_dash = client_a.get('/dashboard', follow_redirects=False)
    assert r_dash.status_code in (302, 401)
    print("PASS: reused backup code rejected")

    # log back in for real with TOTP to continue the session tests
    reset_rate_limits()
    submit_2fa(client_a, totp.now())
    assert client_a.get('/dashboard').status_code == 200

    # ── 6. Session management across two "devices" ──
    reset_rate_limits()
    client_b = new_client()
    login(client_b, TEST_EMAIL, expect_2fa=True)
    submit_2fa(client_b, totp.now())
    assert client_b.get('/dashboard').status_code == 200

    with app.app_context():
        sessions = UserSession.query.filter_by(user_id=user_id).all()
        assert len(sessions) == 2, f"expected 2 active sessions, got {len(sessions)}"
    print("PASS: two simultaneous logins produce two UserSession rows")

    prefs_a = client_a.get('/preferences')
    prefs_csrf_a = get_csrf(prefs_a.data)
    body_a = prefs_a.get_data(as_text=True)
    assert 'THIS DEVICE' in body_a
    assert body_a.count('Log out') >= 2  # own row's "Log out this device" + the other row's "Log out"

    # Revoke client_b's session from client_a's preferences page, by finding the
    # session row whose id is NOT the one client_a itself is using (we don't have
    # direct access to which token belongs to which client from here, so instead
    # revoke by finding the non-"current" row rendered in client_a's own page).
    action_ids = re.findall(r'action="/account/sessions/(\d+)/revoke"', body_a)
    assert len(action_ids) == 2, f"expected 2 revoke buttons, got {action_ids}"

    # Determine which one is "this device" (client_a) vs the other, by checking
    # the THIS DEVICE marker position relative to each block.
    blocks = body_a.split('Log out this device')
    # blocks[0] contains everything up to and including client_a's own action id
    this_device_id = re.findall(r'/account/sessions/(\d+)/revoke', blocks[0])[-1]
    other_id = [i for i in action_ids if i != this_device_id][0]

    r = client_a.post(f'/account/sessions/{other_id}/revoke',
                       data={'csrf_token': prefs_csrf_a}, follow_redirects=True)
    assert r.status_code == 200

    # client_b should now be logged out on its next request
    r_b_dash = client_b.get('/dashboard', follow_redirects=False)
    assert r_b_dash.status_code in (302, 401), "client_b should be logged out after its session was revoked"
    # client_a should still be logged in
    r_a_dash = client_a.get('/dashboard')
    assert r_a_dash.status_code == 200, "client_a should remain logged in"
    print("PASS: revoking one device's session logs out only that device")

    # ── log client_b back in, then test 'log out all other devices' from client_a ──
    reset_rate_limits()
    login(client_b, TEST_EMAIL, expect_2fa=True)
    submit_2fa(client_b, totp.now())
    assert client_b.get('/dashboard').status_code == 200

    prefs_a2 = client_a.get('/preferences')
    prefs_csrf_a2 = get_csrf(prefs_a2.data)
    r = client_a.post('/account/sessions/revoke-others',
                       data={'csrf_token': prefs_csrf_a2}, follow_redirects=True)
    assert r.status_code == 200
    assert client_b.get('/dashboard', follow_redirects=False).status_code in (302, 401)
    assert client_a.get('/dashboard').status_code == 200
    with app.app_context():
        remaining = UserSession.query.filter_by(user_id=user_id).count()
        assert remaining == 1, f"expected exactly 1 session left, got {remaining}"
    print("PASS: 'log out all other devices' leaves only the caller's session")

    # ── 7. Disable 2FA requires password + code ──
    prefs_a3 = client_a.get('/preferences')
    prefs_csrf_a3 = get_csrf(prefs_a3.data)
    r = client_a.post('/account/2fa/disable', data={
        'csrf_token': prefs_csrf_a3, 'current_password': 'WrongPassword!', 'code': totp.now(),
    }, follow_redirects=True)
    with app.app_context():
        assert User.query.get(user_id).totp_enabled is True, "wrong password must not disable 2FA"

    r = client_a.post('/account/2fa/disable', data={
        'csrf_token': prefs_csrf_a3, 'current_password': PASSWORD, 'code': totp.now(),
    }, follow_redirects=True)
    with app.app_context():
        u = User.query.get(user_id)
        assert u.totp_enabled is False, "correct password+code should disable 2FA"
        assert u.totp_secret is None
        assert BackupCode.query.filter_by(user_id=user_id).count() == 0
    print("PASS: 2FA disabled correctly, secret and backup codes cleared")

    reset_rate_limits()
    client_a.get('/logout', follow_redirects=True)
    login(client_a, TEST_EMAIL, expect_2fa=False)
    assert client_a.get('/dashboard').status_code == 200
    print("PASS: login is single-factor again after disabling 2FA")

    # ── Cleanup ──
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        if u:
            BackupCode.query.filter_by(user_id=u.id).delete()
            UserSession.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()

    print("\nALL 2FA AND SESSION MANAGEMENT TESTS PASSED")
    return True


if __name__ == "__main__":
    run_in_scratch(main)
