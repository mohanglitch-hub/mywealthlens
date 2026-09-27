"""
End-to-end test for the self-service /account/delete route (Batch 3,
Sep 2026). Seeds data across several modules for a test user AND a
separate control user, then:
  1. Confirms wrong password is rejected and nothing is deleted.
  2. Confirms wrong "DELETE" confirmation text is rejected.
  3. Confirms the correct request deletes the account, logs the
     session out, and leaves the control user's data completely
     untouched.

Run from the project root:
    py tests\\test_account_deletion.py       (Windows)
    python3 tests/test_account_deletion.py   (Mac/Linux)

Runs against a disposable scratch copy of the project (see
_scratch_env.py) -- never your real instance/mywealthlens.db. Exits
with status 0 if every check passes, 1 otherwise.
"""
import re
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_env import run_in_scratch

TEST_EMAIL = "delete_route_test@example.com"
CONTROL_EMAIL = "delete_route_control@example.com"
PASSWORD = "TestPass123!"


def get_csrf(page_bytes):
    m = re.search(rb'name="csrf_token" value="([^"]+)"', page_bytes)
    return m.group(1).decode() if m else None


def main():
    from app import app, db
    from models import User, Goal
    from wealth.models import WealthAsset, WealthAssetCategory
    from insurance_centre.models import InsurancePolicy
    from retirement_centre.models import RetirementScheme
    from family_centre.models import FamilyPerson

    client = app.test_client()

    def signup(email, name):
        page = client.get('/signup')
        r = client.post('/signup', data={
            'csrf_token': get_csrf(page.data),
            'name': name, 'email': email,
            'password': PASSWORD, 'confirm_password': PASSWORD,
        }, follow_redirects=True)
        assert r.status_code == 200, r.status_code

    def login(email):
        page = client.get('/login')
        r = client.post('/login', data={
            'csrf_token': get_csrf(page.data),
            'email': email, 'password': PASSWORD,
        }, follow_redirects=True)
        assert r.status_code == 200, r.status_code

    def logout():
        client.get('/logout', follow_redirects=True)

    def seed(user_id):
        db.session.add(WealthAsset(user_id=user_id, category=WealthAssetCategory.REAL_ESTATE,
                                    name="Flat", asset_type="Apartment",
                                    current_value=5000000, currency="INR"))
        db.session.add(Goal(user_id=user_id, name="Goal", emoji="🎯", target_amt=2000000,
                             target_year=date.today().year + 5, current_savings=100000,
                             monthly_sip=10000, annual_return=10))
        db.session.add(InsurancePolicy(
            user_id=user_id, category='Life', insurance_type='Term Plan',
            insurer='Insurer', policy_name='Term Plan',
            sum_assured=5000000, premium_amount=25000, premium_frequency='Yearly',
            status='Active', renewal_date=date.today() + timedelta(days=5),
        ))
        db.session.add(RetirementScheme(
            user_id=user_id, scheme_type='EPF', institution='EPFO',
            current_balance=1500000,
        ))
        db.session.add(FamilyPerson(user_id=user_id, name='Spouse', relationship='Spouse'))
        db.session.commit()

    def counts(user_id):
        return {
            "wealth_asset": WealthAsset.query.filter_by(user_id=user_id).count(),
            "goal": Goal.query.filter_by(user_id=user_id).count(),
            "insurance_policy": InsurancePolicy.query.filter_by(user_id=user_id).count(),
            "retirement_scheme": RetirementScheme.query.filter_by(user_id=user_id).count(),
            "family_person": FamilyPerson.query.filter_by(user_id=user_id).count(),
        }

    # ── Setup: clean slate, then create test + control users with data ──
    with app.app_context():
        for email in (TEST_EMAIL, CONTROL_EMAIL):
            existing = User.query.filter_by(email=email).first()
            if existing:
                WealthAsset.query.filter_by(user_id=existing.id).delete()
                Goal.query.filter_by(user_id=existing.id).delete()
                InsurancePolicy.query.filter_by(user_id=existing.id).delete()
                RetirementScheme.query.filter_by(user_id=existing.id).delete()
                FamilyPerson.query.filter_by(user_id=existing.id).delete()
                db.session.delete(existing)
        db.session.commit()

    signup(CONTROL_EMAIL, "Control User")
    with app.app_context():
        control = User.query.filter_by(email=CONTROL_EMAIL).first()
        control_id = control.id
        seed(control_id)
    logout()

    signup(TEST_EMAIL, "Delete Route Test")
    with app.app_context():
        test_user = User.query.filter_by(email=TEST_EMAIL).first()
        test_id = test_user.id
        seed(test_id)
        before = counts(test_id)
        assert all(v == 1 for v in before.values()), before
    print(f"PASS: seeded test user (id={test_id}) and control user (id={control_id}) with data")

    # already logged in from signup — grab a CSRF-bearing page (preferences)
    prefs_page = client.get('/preferences')
    csrf = get_csrf(prefs_page.data)
    assert csrf, "couldn't find CSRF token on preferences page"

    # ── 1. Wrong password ──
    r = client.post('/account/delete', data={
        'csrf_token': csrf, 'current_password': 'WrongPassword!', 'confirm_delete': 'DELETE',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert User.query.filter_by(email=TEST_EMAIL).first() is not None, "user deleted on wrong password!"
    print("PASS: wrong password rejected, account NOT deleted")

    # ── 2. Wrong confirmation text ──
    r = client.post('/account/delete', data={
        'csrf_token': csrf, 'current_password': PASSWORD, 'confirm_delete': 'delete',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert User.query.filter_by(email=TEST_EMAIL).first() is not None, "user deleted on wrong confirm text!"
    print("PASS: wrong confirmation text rejected, account NOT deleted")

    # ── 3. Correct request: should delete everything ──
    r = client.post('/account/delete', data={
        'csrf_token': csrf, 'current_password': PASSWORD, 'confirm_delete': 'DELETE',
    }, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        assert User.query.filter_by(email=TEST_EMAIL).first() is None, "user row still present after deletion!"
        after = counts(test_id)
        assert all(v == 0 for v in after.values()), f"orphaned rows left behind: {after}"
        print(f"PASS: user row and all cascade rows deleted for test user (id={test_id})")

        # control user completely untouched
        still_control = User.query.filter_by(email=CONTROL_EMAIL).first()
        assert still_control is not None, "control user was deleted too!"
        control_after = counts(control_id)
        assert all(v == 1 for v in control_after.values()), f"control user's data was touched: {control_after}"
        print(f"PASS: control user (id={control_id}) completely untouched")

    # ── 4. Session should be logged out — dashboard should redirect ──
    r = client.get('/dashboard', follow_redirects=False)
    assert r.status_code in (302, 401), f"expected redirect after logout, got {r.status_code}"
    print("PASS: session logged out after account deletion")

    # ── Cleanup: remove control user ──
    with app.app_context():
        control = User.query.filter_by(email=CONTROL_EMAIL).first()
        if control:
            WealthAsset.query.filter_by(user_id=control.id).delete()
            Goal.query.filter_by(user_id=control.id).delete()
            InsurancePolicy.query.filter_by(user_id=control.id).delete()
            RetirementScheme.query.filter_by(user_id=control.id).delete()
            FamilyPerson.query.filter_by(user_id=control.id).delete()
            db.session.delete(control)
        db.session.commit()

    print("\nALL ACCOUNT-DELETION ROUTE TESTS PASSED")
    return True


if __name__ == "__main__":
    run_in_scratch(main)
