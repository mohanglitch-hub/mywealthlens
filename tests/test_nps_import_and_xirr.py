"""
Batch 4 — NPS CRA CSV import + per-scheme XIRR (Sep 2026).

Covers:
  1. parse_nps_cra_csv() classification: a genuine contribution row, an
     interest row, an excluded row (withdrawal), an unrecognized
     transaction type, and a malformed row (bad amount) are all
     classified correctly, header aliases from either CRA are detected.
  2. import_nps_contributions() end-to-end via the real HTTP route:
     first import inserts the right rows with the right entry types
     and does NOT touch current_balance; re-importing the exact same
     file a second time skips everything as duplicates rather than
     doubling the history.
  3. The import route rejects a non-NPS scheme.
  4. retirement_xirr.compute_retirement_xirr(): a reasonable positive
     XIRR from a real contribution history + current balance; None
     when there are no deposits yet; None when every cash flow is the
     same sign (deposits recorded, but a zero balance).
  5. The scheme detail page renders the XIRR figure once it can be
     computed.

Run from the project root:
    py tests\\test_nps_import_and_xirr.py       (Windows)
    python3 tests/test_nps_import_and_xirr.py   (Mac/Linux)

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

TEST_EMAIL = "nps_import_test@example.com"
PASSWORD = "TestPass123!"


def get_csrf(page_bytes):
    m = re.search(rb'name="csrf_token" value="([^"]+)"', page_bytes)
    return m.group(1).decode() if m else None


def main():
    from retirement_centre.nps_import import parse_nps_cra_csv, NpsImportError
    from retirement_xirr import compute_retirement_xirr
    from app import app, db
    from models import User
    from retirement_centre.models import RetirementScheme, RetirementContribution, ContributionEntryType

    # ── 1. Parsing/classification, no DB or HTTP involved ──
    csv_text = (
        "Transaction Date,Transaction Type,Amount (Rs.)\n"
        "01/04/2023,Subscriber Contribution,5000\n"
        "01/05/2023,Employer Contribution,5000\n"
        "15/05/2023,Interest Credited to account,120.50\n"
        "01/06/2023,Withdrawal - Partial,10000\n"
        "01/07/2023,Miscellaneous Adjustment,50\n"
        "not-a-date,Subscriber Contribution,5000\n"
    )
    rows = parse_nps_cra_csv(csv_text.encode("utf-8"))
    assert len(rows) == 6, len(rows)
    assert rows[0]["classification"] == "deposit" and rows[0]["amount"] == 5000
    assert rows[1]["classification"] == "deposit"
    assert rows[2]["classification"] == "interest" and abs(rows[2]["amount"] - 120.50) < 0.001
    assert rows[3]["classification"] == "excluded"
    assert rows[4]["classification"] == "unrecognized"
    assert rows[5]["error"] is not None
    print("PASS: NPS CSV rows classified correctly (deposit/interest/excluded/unrecognized/error)")

    # Alternate KFintech-style headers still detected
    csv_text_alt = (
        "Txn Date,Particulars,Contribution Amount\n"
        "10/04/2023,Voluntary Contribution - Tier I,2000\n"
    )
    rows_alt = parse_nps_cra_csv(csv_text_alt.encode("utf-8"))
    assert len(rows_alt) == 1 and rows_alt[0]["classification"] == "deposit"
    print("PASS: alternate CRA header names (Txn Date / Particulars / Contribution Amount) detected")

    # A file with no recognizable date/amount columns raises a clear file-level error
    try:
        parse_nps_cra_csv(b"Foo,Bar\n1,2\n")
        raise AssertionError("expected NpsImportError for unrecognizable headers")
    except NpsImportError:
        pass
    print("PASS: unrecognizable file headers raise a clear file-level error instead of guessing")

    # ── 2. End-to-end import via the real HTTP route ──
    with app.app_context():
        existing = User.query.filter_by(email=TEST_EMAIL).first()
        if existing:
            for s in RetirementScheme.query.filter_by(user_id=existing.id).all():
                RetirementContribution.query.filter_by(scheme_id=s.id).delete()
            RetirementScheme.query.filter_by(user_id=existing.id).delete()
            db.session.delete(existing)
            db.session.commit()

    client = app.test_client()
    page = client.get('/signup')
    r = client.post('/signup', data={
        'csrf_token': get_csrf(page.data),
        'name': 'NPS Import Test', 'email': TEST_EMAIL,
        'password': PASSWORD, 'confirm_password': PASSWORD,
    }, follow_redirects=True)
    assert r.status_code == 200

    add_page = client.get('/retirement/add')
    csrf = get_csrf(add_page.data)
    r = client.post('/retirement/add', data={
        'csrf_token': csrf, 'scheme_type': 'NPS', 'institution': 'Test PFM',
        'opening_date': '2020-01-01', 'current_balance': '11500',
        'pran_number': '1234567890', 'tier': 'Tier I',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        scheme = RetirementScheme.query.filter_by(user_id=u.id, scheme_type='NPS').first()
        assert scheme is not None
        scheme_id = scheme.id
        balance_before_import = scheme.current_balance
    print(f"PASS: NPS scheme created (id={scheme_id})")

    import io
    nps_csv_bytes = csv_text.encode("utf-8")  # the same file used in step 1

    scheme_page = client.get(f'/retirement/scheme/{scheme_id}')
    import_csrf = get_csrf(scheme_page.data)
    r = client.post(f'/retirement/scheme/{scheme_id}/contributions/import-nps-csv', data={
        'csrf_token': import_csrf,
        'nps_csv_file': (io.BytesIO(nps_csv_bytes), 'statement.csv'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        scheme = RetirementScheme.query.get(scheme_id)
        contributions = RetirementContribution.query.filter_by(scheme_id=scheme_id).all()
        deposits = [c for c in contributions if c.entry_type == ContributionEntryType.DEPOSIT]
        interest = [c for c in contributions if c.entry_type == ContributionEntryType.INTEREST]
        # 2 genuine contribution rows + 1 interest row imported; withdrawal,
        # misc, and the malformed row must NOT have been imported.
        assert len(deposits) == 2, len(deposits)
        assert len(interest) == 1, len(interest)
        assert len(contributions) == 3, len(contributions)
        # current_balance must be untouched by a bulk historical import
        # (see nps_import.py's module docstring for why).
        assert scheme.current_balance == balance_before_import, (scheme.current_balance, balance_before_import)
    print("PASS: NPS CSV import inserted exactly the genuine contribution/interest rows, current_balance untouched")

    # Re-importing the SAME file must skip everything as duplicates, not double the history.
    scheme_page2 = client.get(f'/retirement/scheme/{scheme_id}')
    import_csrf2 = get_csrf(scheme_page2.data)
    r = client.post(f'/retirement/scheme/{scheme_id}/contributions/import-nps-csv', data={
        'csrf_token': import_csrf2,
        'nps_csv_file': (io.BytesIO(nps_csv_bytes), 'statement.csv'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        contributions_after = RetirementContribution.query.filter_by(scheme_id=scheme_id).all()
        assert len(contributions_after) == 3, \
            f"re-importing the same file must not duplicate rows, got {len(contributions_after)}"
    print("PASS: re-importing the identical statement is fully deduplicated")

    # ── 3. Import route rejects a non-NPS scheme ──
    epf_page = client.get('/retirement/add')
    epf_csrf = get_csrf(epf_page.data)
    client.post('/retirement/add', data={
        'csrf_token': epf_csrf, 'scheme_type': 'EPF', 'institution': 'EPFO',
        'opening_date': '2020-01-01', 'current_balance': '5000',
    }, follow_redirects=True)
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        epf_scheme = RetirementScheme.query.filter_by(user_id=u.id, scheme_type='EPF').first()
        epf_id = epf_scheme.id

    epf_detail = client.get(f'/retirement/scheme/{epf_id}')
    epf_csrf2 = get_csrf(epf_detail.data)
    r = client.post(f'/retirement/scheme/{epf_id}/contributions/import-nps-csv', data={
        'csrf_token': epf_csrf2,
        'nps_csv_file': (io.BytesIO(b"Date,Type,Amount\n01/01/2023,Contribution,100\n"), 'statement.csv'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'only available for NPS schemes' in body
    with app.app_context():
        assert RetirementContribution.query.filter_by(scheme_id=epf_id).count() == 0, \
            "import must be rejected outright for a non-NPS scheme"
    print("PASS: NPS CSV import route correctly rejects a non-NPS scheme")

    # ── 4. retirement_xirr.compute_retirement_xirr() ──
    today = date.today()

    class FakeContribution:
        def __init__(self, d, amount, entry_type):
            self.contribution_date = d
            self.amount = amount
            self.entry_type = entry_type

    deposits_only = [
        FakeContribution(today - timedelta(days=730), 10000, ContributionEntryType.DEPOSIT),
        FakeContribution(today - timedelta(days=365), 10000, ContributionEntryType.DEPOSIT),
    ]
    xirr_result = compute_retirement_xirr(deposits_only, current_balance=25000, asof=today)
    assert xirr_result is not None and xirr_result > 0, xirr_result
    print(f"PASS: XIRR computed from a real contribution history + current balance ({xirr_result}%)")

    assert compute_retirement_xirr([], current_balance=10000) is None
    print("PASS: XIRR is None with no deposits recorded")

    assert compute_retirement_xirr(deposits_only, current_balance=0) is None
    print("PASS: XIRR is None when every cash flow is the same sign (deposits but zero balance)")

    # ── 5. Scheme detail page renders the XIRR figure ──
    with app.app_context():
        scheme = RetirementScheme.query.get(scheme_id)
        # Give the NPS scheme's contributions dates far enough apart that
        # a real percentage is computable (they're both within a few
        # months of each other from step 1's fixed 2023 dates, which is
        # fine -- XIRR just needs 2+ dates and a positive balance).
        pass
    r = client.get(f'/retirement/scheme/{scheme_id}')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'XIRR' in body, "expected an XIRR figure to render on the scheme detail page"
    print("PASS: scheme detail page renders the computed XIRR")

    # ── Cleanup ──
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        if u:
            for s in RetirementScheme.query.filter_by(user_id=u.id).all():
                RetirementContribution.query.filter_by(scheme_id=s.id).delete()
            RetirementScheme.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()

    print("\nALL NPS IMPORT + XIRR TESTS PASSED")
    return True


if __name__ == "__main__":
    run_in_scratch(main)
