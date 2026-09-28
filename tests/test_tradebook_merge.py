"""
Batch 5 -- tradebook import robustness: merge-by-default re-uploads,
comma-formatted number parsing, and the explicit "start over" wipe
option (Sep 2026).

Context: re-uploading a tradebook CSV used to always wipe every
existing tradebook-sourced stock and rebuild from just the new file.
That's a real data-loss bug because most Indian brokers only let you
export ONE financial year's tradebook at a time -- a second upload for
a later year would silently destroy the first year's transaction
history (and with it, any real long-term XIRR). Mohan's call (Sep
2026): merge by default, natural-key deduplicated, with an explicit
opt-in checkbox to wipe and start over for correcting a bad import.

Covers:
  1. _parse_tradebook_csv() handles comma-formatted quantity/price
     ("1,234.50") and a stray "Rs."/"₹" prefix without dropping the row.
  2. Uploading a second tradebook file (different financial year, no
     overlap) via the real HTTP route MERGES with the first upload's
     holdings instead of replacing them -- both years' transactions
     survive.
  3. Uploading the exact same file again is fully deduplicated -- no
     new transactions added, existing holdings/ids untouched, no
     "goal link broken" warning fires (nothing was actually replaced).
  4. Checking "start over" (wipe_existing=on) discards prior history
     and rebuilds from just that one file, matching the old behavior.

Run from the project root:
    py tests\\test_tradebook_merge.py       (Windows)
    python3 tests/test_tradebook_merge.py   (Mac/Linux)

Runs against a disposable scratch copy of the project (see
_scratch_env.py) -- never your real instance/mywealthlens.db. Exits
with status 0 if every check passes, 1 otherwise.
"""
import re
import sys
import os
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_env import run_in_scratch

TEST_EMAIL = "tradebook_merge_test@example.com"
PASSWORD = "TestPass123!"


def get_csrf(page_bytes):
    m = re.search(rb'name="csrf_token" value="([^"]+)"', page_bytes)
    return m.group(1).decode() if m else None


def main():
    from app import app, db, _parse_tradebook_csv
    from models import User, Stock, StockTransaction

    # ── 1. Comma-formatted numbers and a Rs./₹ prefix parse correctly ──
    csv_text = (
        "Symbol,ISIN,Trade Date,Trade Type,Quantity,Price\n"
        "RELIANCE,INE002A01018,01-04-2023,BUY,\"1,000\",\"Rs. 2,450.75\"\n"
        "TCS,INE467B01029,05-04-2023,BUY,10,\"₹3,200\"\n"
    )
    rows, skipped = _parse_tradebook_csv(csv_text)
    assert skipped == 0, f"expected no skipped rows, got {skipped}"
    assert len(rows) == 2, rows
    reliance = next(r for r in rows if r['symbol'] == 'RELIANCE')
    assert reliance['quantity'] == 1000, reliance
    assert reliance['price'] == 2450.75, reliance
    tcs = next(r for r in rows if r['symbol'] == 'TCS')
    assert tcs['quantity'] == 10, tcs
    assert tcs['price'] == 3200, tcs
    print("PASS: comma-formatted quantity/price and Rs./₹ prefixes parse correctly")

    # ── 2/3/4. End-to-end merge/dedupe/wipe behavior via the real HTTP route ──
    with app.app_context():
        existing = User.query.filter_by(email=TEST_EMAIL).first()
        if existing:
            Stock.query.filter_by(user_id=existing.id).delete()
            db.session.delete(existing)
            db.session.commit()

    client = app.test_client()
    page = client.get('/signup')
    r = client.post('/signup', data={
        'csrf_token': get_csrf(page.data),
        'name': 'Tradebook Merge Test', 'email': TEST_EMAIL,
        'password': PASSWORD, 'confirm_password': PASSWORD,
    }, follow_redirects=True)
    assert r.status_code == 200

    def upload(csv_bytes, wipe=False):
        upload_page = client.get('/upload')
        csrf = get_csrf(upload_page.data)
        data = {
            'csrf_token': csrf,
            'csv_file': (io.BytesIO(csv_bytes), 'tradebook.csv'),
        }
        if wipe:
            data['wipe_existing'] = 'on'
        return client.post('/upload/tradebook', data=data,
                            content_type='multipart/form-data', follow_redirects=True)

    fy23_csv = (
        "Symbol,ISIN,Trade Date,Trade Type,Quantity,Price\n"
        "RELIANCE,INE002A01018,01-04-2023,BUY,10,2400\n"
        "RELIANCE,INE002A01018,15-06-2023,BUY,5,2500\n"
    ).encode('utf-8')

    r = upload(fy23_csv)
    assert r.status_code == 200
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        stocks = Stock.query.filter_by(user_id=u.id, source='tradebook').all()
        assert len(stocks) == 1, stocks
        reliance_id_1 = stocks[0].id
        txns = StockTransaction.query.filter_by(stock_id=reliance_id_1).all()
        assert len(txns) == 2, len(txns)
    print("PASS: first tradebook upload creates the expected holding + transactions")

    # A second, non-overlapping financial year's file (a different broker
    # export, a different stock) -- must MERGE, not replace.
    fy24_csv = (
        "Symbol,ISIN,Trade Date,Trade Type,Quantity,Price\n"
        "RELIANCE,INE002A01018,10-04-2024,BUY,3,2900\n"
        "TCS,INE467B01029,12-04-2024,BUY,4,3600\n"
    ).encode('utf-8')

    r = upload(fy24_csv)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Added 2 new transaction' in body, body
    assert 'lost their old holding link' not in body, \
        "a merge with no overlap shouldn't touch existing Stock ids or fire the goal-link warning"
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        stocks = {s.name: s for s in Stock.query.filter_by(user_id=u.id, source='tradebook').all()}
        assert set(stocks.keys()) == {'RELIANCE', 'TCS'}, stocks.keys()
        reliance_txns = StockTransaction.query.filter_by(stock_id=stocks['RELIANCE'].id).all()
        assert len(reliance_txns) == 3, \
            f"expected FY23's 2 transactions + FY24's 1 new one = 3, got {len(reliance_txns)}"
        assert stocks['RELIANCE'].quantity == 18, stocks['RELIANCE'].quantity  # 10+5+3
        tcs_txns = StockTransaction.query.filter_by(stock_id=stocks['TCS'].id).all()
        assert len(tcs_txns) == 1, len(tcs_txns)
    print("PASS: second (non-overlapping) tradebook upload MERGES with existing history instead of wiping it")

    # Re-uploading the exact same FY24 file again -- fully deduplicated,
    # nothing new, existing holdings/ids left untouched.
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        reliance_id_before = Stock.query.filter_by(user_id=u.id, name='RELIANCE').first().id

    r = upload(fy24_csv)
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Nothing new in this file' in body, body
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        reliance = Stock.query.filter_by(user_id=u.id, name='RELIANCE').first()
        assert reliance.id == reliance_id_before, \
            "a fully-duplicate re-upload must not touch existing holdings/ids at all"
        assert StockTransaction.query.filter_by(stock_id=reliance.id).count() == 3
    print("PASS: re-uploading an identical file is fully deduplicated and leaves existing data untouched")

    # "Start over" checkbox -- discards everything, rebuilds from just this file.
    r = upload(fy23_csv, wipe=True)
    assert r.status_code == 200
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        stocks = Stock.query.filter_by(user_id=u.id, source='tradebook').all()
        assert len(stocks) == 1 and stocks[0].name == 'RELIANCE', \
            "wipe=True must discard TCS entirely and rebuild only from the FY23 file"
        assert stocks[0].quantity == 15, stocks[0].quantity  # 10+5, FY24's txns gone
    print("PASS: 'start over' checkbox correctly wipes prior history and rebuilds from just the new file")

    # ── Cleanup ──
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        if u:
            Stock.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()

    print("\nALL TRADEBOOK MERGE/DEDUPE TESTS PASSED")
    return True


if __name__ == "__main__":
    run_in_scratch(main)
