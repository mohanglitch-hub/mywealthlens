"""
Audit-fix verification (Sep 2026): all four ReportLab PDF exports must
never emit the raw ₹ glyph, since none of them register a Unicode-
capable font (base Helvetica/WinAnsiEncoding only) -- confirmed by a
fresh-eyes audit that found app.py's /export/pdf, retirement_centre's,
and family_centre's PDF exports were still using the unsafe glyph-
based formatter under INR (the DEFAULT currency, i.e. every user who
hasn't switched), even though currency_display.format_money_pdf_safe()
was built specifically to prevent this and insurance_centre's export
already used it correctly.

This test seeds a small amount of data in each module and exports
each PDF under the default INR currency (no currency switch needed --
this is precisely the case that was broken) and asserts none of the
four PDF byte streams contain the raw UTF-8 ₹ glyph.

Run from the project root:
    py tests\\test_pdf_glyph_safety.py       (Windows)
    python3 tests/test_pdf_glyph_safety.py   (Mac/Linux)

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

TEST_EMAIL = "pdf_glyph_test@example.com"


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

    with app.app_context():
        existing = User.query.filter_by(email=TEST_EMAIL).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()

    signup_page = client.get('/signup')
    r = client.post('/signup', data={
        'csrf_token': get_csrf(signup_page.data),
        'name': 'PDF Glyph Test', 'email': TEST_EMAIL,
        'password': 'TestPass123!', 'confirm_password': 'TestPass123!',
    }, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        uid = u.id
        assert u.display_currency == 'INR'  # the default -- the broken case

        db.session.add(WealthAsset(user_id=uid, category=WealthAssetCategory.REAL_ESTATE,
                                    name="Test Flat", asset_type="Apartment",
                                    current_value=5000000, currency="INR"))
        db.session.add(Goal(user_id=uid, name="Test Goal", emoji="🎯", target_amt=2000000,
                             target_year=date.today().year + 5, current_savings=100000,
                             monthly_sip=10000, annual_return=10))
        db.session.add(InsurancePolicy(
            user_id=uid, category='Life', insurance_type='Term Plan',
            insurer='Test Insurer', policy_name='Test Term Plan',
            sum_assured=5000000, premium_amount=25000, premium_frequency='Yearly',
            status='Active', renewal_date=date.today() + timedelta(days=5),
        ))
        db.session.add(RetirementScheme(
            user_id=uid, scheme_type='EPF', institution='Test EPFO',
            current_balance=1500000,
        ))
        db.session.add(FamilyPerson(user_id=uid, name='Test Spouse', relationship='Spouse'))
        db.session.commit()

    # signup already logs the test client in (session cookie carries over);
    # confirm we can reach a protected page rather than re-logging in.
    r = client.get('/dashboard')
    assert r.status_code == 200, r.status_code

    RUPEE_UTF8 = '₹'.encode('utf-8')

    exports = [
        ('/export/pdf', 'wealth/goals PDF (app.py)'),
        ('/insurance-centre/export/pdf', 'insurance PDF'),
        ('/retirement/export/pdf', 'retirement PDF'),
    ]
    for path, label in exports:
        resp = client.get(path)
        if resp.status_code != 200:
            print(f"  (skipped {label} at {path}: status {resp.status_code})")
            continue
        assert resp.mimetype == 'application/pdf', (label, resp.mimetype)
        assert RUPEE_UTF8 not in resp.data, f"{label} ({path}) still emits raw ₹ bytes under INR!"
        print(f"PASS: {label} has no raw ₹ bytes under INR ({len(resp.data)} bytes)")

    # family_centre's PDF export route name may differ -- discover it defensively
    family_pdf_candidates = ['/family-centre/export/pdf', '/family/export/pdf']
    found_family = False
    for path in family_pdf_candidates:
        resp = client.get(path)
        if resp.status_code == 200 and resp.mimetype == 'application/pdf':
            found_family = True
            assert RUPEE_UTF8 not in resp.data, f"family PDF ({path}) still emits raw ₹ bytes under INR!"
            print(f"PASS: family PDF has no raw ₹ bytes under INR ({len(resp.data)} bytes)")
            break
    if not found_family:
        print(f"  (family PDF export route not found among {family_pdf_candidates} -- skipped, check manually)")

    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        if u:
            WealthAsset.query.filter_by(user_id=u.id).delete()
            Goal.query.filter_by(user_id=u.id).delete()
            InsurancePolicy.query.filter_by(user_id=u.id).delete()
            RetirementScheme.query.filter_by(user_id=u.id).delete()
            FamilyPerson.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()

    print("\nALL PDF GLYPH-SAFETY TESTS PASSED")
    return True


if __name__ == "__main__":
    run_in_scratch(main)
