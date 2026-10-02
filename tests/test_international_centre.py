"""
International Investing Centre — new module (Sep 2026, Batch 8 idea).

Covers:
  1. Ticker-based holding creation: quantity*avg_cost -> current_value_native,
     and native->USD conversion via the (monkeypatched) FX rate.
  2. Manually-valued holding (Foreign Bank Account): current_value_native
     entered directly, still gets a USD conversion.
  3. Transactions: BUY/SELL recompute invested_native correctly; XIRR
     computed once there's at least one buy and a distinct-sign value.
  4. refresh_holding(): live price + FX both update the holding without
     ever zeroing out a previously-known value on failure.
  5. Archive -> Restore -> (must be archived to) Delete Permanently
     lifecycle, including that delete cascades transactions/snapshots
     via session.delete() (not a bulk query.delete(), which was the
     source of Batch 5's tradebook orphaning bug).
  6. Remittances: INR->USD conversion at save time, and LRS cumulative
     status (ok/warning/exceeded) across a financial year.
  7. Schedule FA summary: a holding WITH snapshots spanning most of the
     calendar year is "data_complete"; one with none falls back to
     current value and is flagged incomplete.
  8. take_daily_snapshot(): idempotent for the same day (re-running
     updates, not duplicates).
  9. End-to-end HTTP: add a holding, add a transaction, view the
     detail page (renders XIRR), log a remittance, view the Schedule
     FA report page.
  12. Document Vault (Batch 9.3, Sep 2026): save/fetch/delete document
      metadata with ownership (IDOR) checks, secure_file_path() rejects
      path traversal, delete_holding_permanently() cascade-deletes
      document rows, and end-to-end HTTP covering upload (valid +
      rejected file type), download (exact bytes), preview, the
      standalone Document Vault page, delete, and that permanently
      deleting a holding removes its documents' physical files from
      disk (not just the DB rows).

This test never makes a real network call — international_centre.
services.fetch_fx_rate and .fetch_ticker_price are monkeypatched
directly, same pattern as tests/test_price_refresh_fallback.py.

Run from the project root:
    py tests\\test_international_centre.py       (Windows)
    python3 tests/test_international_centre.py   (Mac/Linux)

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

TEST_EMAIL = "international_centre_test@example.com"
PASSWORD = "TestPass123!"


def get_csrf(page_bytes):
    m = re.search(rb'name="csrf_token" value="([^"]+)"', page_bytes)
    return m.group(1).decode() if m else None


def main():
    from app import app, db
    from models import User
    from international_centre.models import (
        InternationalHolding, InternationalTransaction, RemittanceRecord,
        InternationalValueSnapshot, InternationalAssetType, InternationalTxnType,
        RemittancePurpose, LRS_ANNUAL_LIMIT_USD,
    )
    from international_centre import services
    from wealth.timezone_utils import today_ist

    # Deterministic FX: 1 USD = 83.0 INR (so INR->USD rate = 1/83).
    def fake_fetch_fx_rate(from_currency, to_currency="INR", on_date=None):
        from_currency = from_currency.upper()
        to_currency = to_currency.upper()
        if from_currency == to_currency:
            return 1.0, on_date or date.today()
        if from_currency == "USD" and to_currency == "USD":
            return 1.0, date.today()
        if from_currency == "INR" and to_currency == "USD":
            return 1 / 83.0, on_date or date.today()
        if from_currency == "USD" and to_currency == "INR":
            return 83.0, on_date or date.today()
        if to_currency == "USD":
            return 1.0, date.today()  # treat every other native currency as already-USD-equivalent for simplicity
        raise services.FxRateError("no rate in this fake")

    services.fetch_fx_rate = fake_fetch_fx_rate
    # currency_display.usd_to_inr() (used by portfolio_inr_value() and the
    # main dashboard's net worth wiring, Sep 2026) goes through
    # fx_rates.fetch_fx_rate independently of services.fetch_fx_rate above
    # — same fake, same rate, so both paths agree in this test.
    import fx_rates
    fx_rates.fetch_fx_rate = fake_fetch_fx_rate

    with app.app_context():
        existing = User.query.filter_by(email=TEST_EMAIL).first()
        if existing:
            InternationalHolding.query.filter_by(user_id=existing.id).delete()
            RemittanceRecord.query.filter_by(user_id=existing.id).delete()
            db.session.delete(existing)
            db.session.commit()

        u = User(name="Intl Test", email=TEST_EMAIL, password="unused-in-this-test")
        db.session.add(u)
        db.session.commit()
        user_id = u.id

        # ── 1. Ticker-based holding creation ──
        holding, _err = services.create_holding(user_id, {
            "asset_type": InternationalAssetType.US_STOCK,
            "name": "Apple Inc.", "ticker": "AAPL", "country": "United States",
            "native_currency": "USD", "quantity": "10", "avg_cost_native": "150",
        })
        assert _err is None, _err
        assert holding.current_value_native == 1500.0, holding.current_value_native
        assert holding.usd_value == 1500.0, holding.usd_value  # USD->USD, rate 1.0
        assert holding.invested_native == 1500.0, holding.invested_native
        print("PASS: ticker-based holding creation computes value from quantity*avg_cost")

        # ── 2. Manually-valued holding, non-USD currency ──
        bank_holding, _err = services.create_holding(user_id, {
            "asset_type": InternationalAssetType.FOREIGN_BANK_ACCOUNT,
            "name": "Chase Checking", "native_currency": "USD",
            "current_value_native": "5000",
        })
        assert _err is None, _err
        assert bank_holding.current_value_native == 5000.0
        assert bank_holding.usd_value == 5000.0
        print("PASS: manually-valued (non-ticker) holding stores current_value_native directly")

        # ── 3. Transactions -> invested_native + XIRR ──
        services.add_transaction(holding, {
            "date": "2024-01-01", "txn_type": "BUY", "quantity": "10", "price_native": "150", "amount_native": "1500",
        })
        services.add_transaction(holding, {
            "date": "2024-06-01", "txn_type": "SELL", "quantity": "2", "price_native": "180", "amount_native": "360",
        })
        db.session.refresh(holding)
        assert holding.invested_native == 1500.0 - 360.0, holding.invested_native
        assert holding.xirr is not None, "expected a computable XIRR from a buy + sell + current value"
        print(f"PASS: BUY/SELL transactions recompute invested_native ({holding.invested_native}) and XIRR ({holding.xirr}%)")

        # ── 4. refresh_holding(): live price + FX ──
        services.fetch_ticker_price = lambda ticker: 200.0
        # re-bind the name services.py actually calls (imported at module load)
        import international_centre.services as svc_mod
        svc_mod.fetch_ticker_price = lambda ticker: 200.0

        services.refresh_holding(holding)
        db.session.commit()
        assert holding.live_price_native == 200.0, holding.live_price_native
        assert holding.current_value_native == 2000.0, holding.current_value_native  # 10 * 200
        assert holding.usd_value == 2000.0, holding.usd_value
        print("PASS: refresh_holding() updates live price, current_value_native, and usd_value")

        # A failed price fetch must NOT zero out the last known price/value.
        svc_mod.fetch_ticker_price = lambda ticker: None
        services.refresh_holding(holding)
        db.session.commit()
        assert holding.live_price_native == 200.0, "a failed refresh must keep the last known price"
        assert holding.current_value_native == 2000.0
        print("PASS: a failed price refresh keeps the last known price/value instead of zeroing it")

        # ── 5. Archive -> Restore -> Delete lifecycle ──
        bank_id = bank_holding.id
        services.archive_holding(bank_holding)
        assert bank_holding.archived is True
        assert services.get_holdings(user_id, archived=False) == [h for h in services.get_holdings(user_id, archived=False)]
        active_names = {h.name for h in services.get_holdings(user_id, archived=False)}
        assert "Chase Checking" not in active_names
        services.restore_holding(bank_holding)
        assert bank_holding.archived is False
        services.archive_holding(bank_holding)

        # add a transaction + snapshot to prove cascade-delete really works
        services.add_transaction(bank_holding, {
            "date": "2024-01-01", "txn_type": "DIVIDEND", "amount_native": "10",
        })
        db.session.add(InternationalValueSnapshot(holding_id=bank_id, date=date(2024, 1, 1), usd_value=5000.0))
        db.session.commit()
        assert InternationalTransaction.query.filter_by(holding_id=bank_id).count() == 1
        assert InternationalValueSnapshot.query.filter_by(holding_id=bank_id).count() == 1

        services.delete_holding_permanently(bank_holding)
        assert db.session.get(InternationalHolding, bank_id) is None
        assert InternationalTransaction.query.filter_by(holding_id=bank_id).count() == 0, \
            "delete_holding_permanently must cascade-delete its transactions"
        assert InternationalValueSnapshot.query.filter_by(holding_id=bank_id).count() == 0, \
            "delete_holding_permanently must cascade-delete its snapshots"
        print("PASS: archive/restore/delete lifecycle works, and permanent delete correctly cascades child rows")

        # ── 6. Remittances + LRS status ──
        r1 = services.add_remittance(user_id, {
            "date": "2025-06-01", "amount_inr": "8300000",  # ~ $100,000 at rate 1/83
            "purpose": RemittancePurpose.INVESTMENT_SECURITIES,
        })
        assert abs(r1.amount_usd - 100000.0) < 1.0, r1.amount_usd
        print(f"PASS: remittance amount_usd computed correctly from the INR->USD rate (${r1.amount_usd:,.2f})")

        status = services.get_lrs_status(user_id, anchor_date=date(2025, 8, 1))
        assert status["fy_label"] == "FY 2025-26", status["fy_label"]
        assert abs(status["total_usd"] - 100000.0) < 1.0, status
        assert status["status"] == "ok", status["status"]  # 100k / 250k = 40%, below the 80% warning threshold

        r2 = services.add_remittance(user_id, {
            "date": "2025-09-01", "amount_inr": "16600000",  # ~ $200,000 more -> $300,000 total, over the cap
            "purpose": RemittancePurpose.INVESTMENT_SECURITIES,
        })
        status2 = services.get_lrs_status(user_id, anchor_date=date(2025, 9, 15))
        assert status2["status"] == "exceeded", status2["status"]
        assert status2["remaining_usd"] == 0.0, status2["remaining_usd"]
        print(f"PASS: LRS cumulative status correctly flags ok -> exceeded across the same financial year ({status2['pct_used']}% used)")

        services.delete_remittance(r1)
        services.delete_remittance(r2)

        # ── 7. Schedule FA summary ──
        year = 2024
        # Holding WITH snapshots spanning most of the year -> "complete".
        db.session.add_all([
            InternationalValueSnapshot(holding_id=holding.id, date=date(year, 1, 5), usd_value=1500.0),
            InternationalValueSnapshot(holding_id=holding.id, date=date(year, 7, 1), usd_value=2500.0),  # peak
            InternationalValueSnapshot(holding_id=holding.id, date=date(year, 12, 28), usd_value=2000.0),
        ])
        db.session.commit()

        fa = services.get_schedule_fa_summary(user_id, year)
        row = next(r for r in fa["rows"] if r["holding"].id == holding.id)
        assert row["opening_usd"] == 1500.0, row
        assert row["peak_usd"] == 2500.0, row
        assert row["closing_usd"] == 2000.0, row
        assert row["data_complete"] is True, row
        print("PASS: Schedule FA opening/peak/closing correctly derived from a full year of snapshots")

        # A second holding with NO snapshots at all -> falls back to
        # current value, flagged incomplete.
        no_snap_holding, _err = services.create_holding(user_id, {
            "asset_type": InternationalAssetType.FOREIGN_REAL_ESTATE,
            "name": "Condo, Austin TX", "country": "United States",
            "native_currency": "USD", "current_value_native": "300000",
        })
        assert _err is None, _err
        fa2 = services.get_schedule_fa_summary(user_id, year)
        row2 = next(r for r in fa2["rows"] if r["holding"].id == no_snap_holding.id)
        assert row2["data_complete"] is False, row2
        assert row2["opening_usd"] == row2["peak_usd"] == row2["closing_usd"] == 300000.0, row2
        assert fa2["any_incomplete"] is True
        print("PASS: a holding with no tracked snapshots falls back to current value and is flagged incomplete")

        # ── 8. take_daily_snapshot() idempotency + Batch 9.1 refresh fix ──
        # Before the fix, take_daily_snapshot() recorded whatever
        # usd_value a holding already had, without refreshing it first
        # -- a stale ticker price would get snapshotted day after day.
        # Bump the fake live price and confirm the snapshot job itself
        # picks up the NEW price, not the stale one from step 4.
        svc_mod.fetch_ticker_price = lambda ticker: 250.0
        before_count = InternationalValueSnapshot.query.filter_by(holding_id=holding.id).count()
        n = services.take_daily_snapshot(user_id=user_id)
        assert n >= 2  # at least `holding` and `no_snap_holding`
        db.session.refresh(holding)
        assert holding.live_price_native == 250.0, \
            "take_daily_snapshot() must refresh live price/FX before snapshotting (Batch 9.1)"
        assert holding.usd_value == 2500.0, holding.usd_value  # 10 units * $250
        today_snap = InternationalValueSnapshot.query.filter_by(holding_id=holding.id, date=today_ist()).first()
        assert today_snap.usd_value == 2500.0, \
            "today's snapshot must reflect the freshly-refreshed value, not a stale one"
        after_first = InternationalValueSnapshot.query.filter_by(holding_id=holding.id).count()
        assert after_first == before_count + 1, "expected exactly one new snapshot row for today"
        services.take_daily_snapshot(user_id=user_id)
        after_second = InternationalValueSnapshot.query.filter_by(holding_id=holding.id).count()
        assert after_second == after_first, "re-running the same day must update, not duplicate, today's snapshot"
        print("PASS: take_daily_snapshot() refreshes price/FX before snapshotting (Batch 9.1), and is idempotent for repeat runs on the same day")

    # ── 9. End-to-end HTTP ──
    client = app.test_client()
    page = client.get('/signup')
    with app.app_context():
        User.query.filter_by(email="international_http_test@example.com").delete()
        db.session.commit()
    r = client.post('/signup', data={
        'csrf_token': get_csrf(page.data),
        'name': 'Intl HTTP Test', 'email': 'international_http_test@example.com',
        'password': PASSWORD, 'confirm_password': PASSWORD,
    }, follow_redirects=True)
    assert r.status_code == 200

    add_page = client.get('/international/holdings/add')
    assert add_page.status_code == 200
    csrf = get_csrf(add_page.data)
    r = client.post('/international/holdings/add', data={
        'csrf_token': csrf, 'asset_type': InternationalAssetType.US_ETF,
        'name': 'Vanguard S&P 500 ETF', 'ticker': 'VOO', 'country': 'United States',
        'native_currency': 'USD', 'quantity': '5', 'avg_cost_native': '400',
    }, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        from models import db as _db
        u2 = User.query.filter_by(email="international_http_test@example.com").first()
        h = InternationalHolding.query.filter_by(user_id=u2.id, name='Vanguard S&P 500 ETF').first()
        assert h is not None
        h_id = h.id

    detail_page = client.get(f'/international/holdings/{h_id}')
    assert detail_page.status_code == 200
    body = detail_page.get_data(as_text=True)
    assert 'Vanguard' in body and 'ETF' in body  # name has "&" -- HTML-escaped in the rendered page, so check substrings
    print("PASS: add-holding form and holding-detail page work end-to-end over real HTTP")

    txn_csrf = get_csrf(detail_page.data)
    r = client.post(f'/international/holdings/{h_id}/transactions/add', data={
        'csrf_token': txn_csrf, 'date': '2024-03-01', 'txn_type': 'BUY',
        'quantity': '5', 'price_native': '400', 'amount_native': '2000',
    }, follow_redirects=True)
    assert r.status_code == 200
    detail_page2 = client.get(f'/international/holdings/{h_id}')
    assert 'XIRR' in detail_page2.get_data(as_text=True)
    print("PASS: transaction add via real HTTP form works")

    # Batch 9.2: transaction edit (was add/delete only) + branded
    # delete-confirmation modal replacing plain browser confirm().
    body2 = detail_page2.get_data(as_text=True)
    assert 'icConfirmAction' in body2 and 'icConfirmModalOverlay' in body2, \
        "holding_detail.html must include the branded confirm modal, not a plain confirm() dialog"
    with app.app_context():
        added_txn = InternationalTransaction.query.filter_by(holding_id=h_id, amount_native=2000.0).first()
        assert added_txn is not None
        txn_id = added_txn.id

    edit_csrf = get_csrf(detail_page2.data)
    r = client.post(f'/international/transactions/{txn_id}/edit', data={
        'csrf_token': edit_csrf, 'date': '2024-03-02', 'txn_type': 'BUY',
        'quantity': '5', 'price_native': '420', 'amount_native': '2100',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        from models import db as _db2
        edited_txn = _db2.session.get(InternationalTransaction, txn_id)
        assert edited_txn.amount_native == 2100.0, edited_txn.amount_native
        assert edited_txn.price_native == 420.0, edited_txn.price_native
    print("PASS: transaction edit via real HTTP form works (was add/delete only)")

    remit_page = client.get('/international/remittances')
    assert remit_page.status_code == 200
    remit_csrf = get_csrf(remit_page.data)
    r = client.post('/international/remittances/add', data={
        'csrf_token': remit_csrf, 'date': '2025-05-01', 'amount_inr': '415000',
        'purpose': RemittancePurpose.INVESTMENT_SECURITIES,
    }, follow_redirects=True)
    assert r.status_code == 200
    body = client.get('/international/remittances').get_data(as_text=True)
    assert '415,000' in body or '₹415,000' in body or 'FY 20' in body
    print("PASS: remittance logging via real HTTP form works")

    fa_page = client.get('/international/schedule-fa')
    assert fa_page.status_code == 200
    assert 'Schedule FA' in fa_page.get_data(as_text=True)
    print("PASS: Schedule FA report page renders")

    # ── 10. Nominees (Sep 2026) + Family Centre wiring ──
    from werkzeug.datastructures import MultiDict
    from family_centre.routes import _build_people, _coverage_gaps

    with app.app_context():
        u3 = User.query.filter_by(email="international_nominee_test@example.com").first()
        if u3:
            InternationalHolding.query.filter_by(user_id=u3.id).delete()
            db.session.delete(u3)
            db.session.commit()
        u3 = User(name="Nominee Test", email="international_nominee_test@example.com", password="unused")
        db.session.add(u3)
        db.session.commit()
        n_user_id = u3.id

        nom_holding, err = services.create_holding(n_user_id, {
            "asset_type": InternationalAssetType.FOREIGN_BANK_ACCOUNT,
            "name": "Singapore Savings", "native_currency": "USD",
            "current_value_native": "10000",
        })
        assert err is None, err

        # No nominee yet -> Family Centre's Coverage Gaps should flag it.
        gaps = _coverage_gaps(n_user_id)
        assert any(g["source"] == "International Investing" and g["item_name"] == "Singapore Savings"
                   for g in gaps), gaps
        print("PASS: holding with no nominee shows up in Family Centre's Coverage Gaps")

        # Over-100% nominee split is rejected, nothing saved.
        over_md = MultiDict([
            ("nominee_name[]", "A"), ("nominee_percentage[]", "70"),
            ("nominee_name[]", "B"), ("nominee_percentage[]", "40"),
        ])
        result, err2 = services.update_holding(nom_holding, {
            "name": "Singapore Savings", "native_currency": "USD", "current_value_native": "10000",
        }, multi_data=over_md)
        assert result is None and err2 is not None, (result, err2)
        assert nom_holding.nominees.count() == 0, "a rejected submission must not partially save"
        print("PASS: nominee percentages over 100% are rejected without partial saves")

        # Valid split saves, and now feeds both Coverage Gaps and People view.
        ok_md = MultiDict([
            ("nominee_name[]", "Priya Sharma"), ("nominee_relationship[]", "Daughter"), ("nominee_percentage[]", "100"),
        ])
        result2, err3 = services.update_holding(nom_holding, {
            "name": "Singapore Savings", "native_currency": "USD", "current_value_native": "10000",
        }, multi_data=ok_md)
        assert err3 is None, err3
        assert result2.total_nominees_percentage == 100.0

        gaps2 = _coverage_gaps(n_user_id)
        assert not any(g["source"] == "International Investing" for g in gaps2), gaps2
        print("PASS: a fully-assigned nominee clears the Coverage Gap")

        people = _build_people(n_user_id)
        priya = next((p for p in people if p["display_name"] == "Priya Sharma"), None)
        assert priya is not None, "nominee should appear in Family Centre's People view"
        entry = next(e for e in priya["entries"] if e["source"] == "International Investing")
        assert entry["item_name"] == "Singapore Savings" and entry["percentage"] == 100.0
        assert entry["value_at_stake"] == 830000.0, entry  # 10000 USD * 83.0 INR/USD
        print("PASS: nominee appears in Family Centre's People view with correct INR value at stake")

        InternationalHolding.query.filter_by(user_id=n_user_id).delete()
        db.session.delete(u3)
        db.session.commit()

    # ── 11. Net worth dashboard wiring (Sep 2026) ──
    with app.app_context():
        u4 = User.query.filter_by(email="international_networth_test@example.com").first()
        if u4:
            InternationalHolding.query.filter_by(user_id=u4.id).delete()
            db.session.delete(u4)
            db.session.commit()
        u4 = User(name="NetWorth Test", email="international_networth_test@example.com", password="unused")
        db.session.add(u4)
        db.session.commit()
        nw_user_id = u4.id

        nw_holding, err = services.create_holding(nw_user_id, {
            "asset_type": InternationalAssetType.FOREIGN_BANK_ACCOUNT,
            "name": "London Account", "native_currency": "USD",
            "current_value_native": "1000",
        })
        assert err is None, err
        assert nw_holding.usd_value == 1000.0

        totals = services.portfolio_totals(nw_user_id)
        assert totals["total_usd"] == 1000.0 and totals["holdings_count"] == 1, totals

        inr_value = services.portfolio_inr_value(nw_user_id)
        assert inr_value == 83000.0, inr_value  # 1000 USD * 83.0 INR/USD (fake rate)
        print("PASS: portfolio_inr_value() correctly bridges USD total into INR via usd_to_inr()")

        # portfolio_inr_value() never returns None, even with zero holdings.
        u5 = User(name="Empty Portfolio", email="international_empty_test@example.com", password="unused")
        db.session.add(u5)
        db.session.commit()
        assert services.portfolio_inr_value(u5.id) == 0.0
        print("PASS: portfolio_inr_value() returns 0.0 (never None) for a user with no holdings")

        InternationalHolding.query.filter_by(user_id=nw_user_id).delete()
        db.session.delete(u4)
        db.session.delete(u5)
        db.session.commit()

    # ── 12. Document Vault (Batch 9.3, Sep 2026) ──
    from international_centre.models import InternationalHoldingDocument, DocumentType
    from international_centre import utils as intl_utils
    from io import BytesIO

    with app.app_context():
        u6 = User.query.filter_by(email="international_vault_test@example.com").first()
        if u6:
            for h in InternationalHolding.query.filter_by(user_id=u6.id).all():
                for d in h.documents:
                    intl_utils.delete_document_file(d.file_path)
            InternationalHolding.query.filter_by(user_id=u6.id).delete()
            db.session.delete(u6)
            db.session.commit()
        u6 = User(name="Vault Test", email="international_vault_test@example.com", password="unused")
        db.session.add(u6)
        db.session.commit()
        v_user_id = u6.id

        v_holding, err = services.create_holding(v_user_id, {
            "asset_type": InternationalAssetType.FOREIGN_BANK_ACCOUNT,
            "name": "Zurich Savings", "native_currency": "USD",
            "current_value_native": "20000",
        })
        assert err is None, err

        # Service-level: save + fetch metadata (file saved to a real
        # scratch path via utils.save_document_file, matching what the
        # upload route actually does).
        class _FakeUpload:
            filename = "statement.pdf"
            def save(self, path):
                with open(path, "wb") as fh:
                    fh.write(b"%PDF-1.4 fake pdf bytes for testing")

        stored_name, file_path, file_size = intl_utils.save_document_file(_FakeUpload(), v_holding.id)
        assert os.path.exists(file_path), "save_document_file must actually write the file to disk"
        assert intl_utils.secure_file_path(file_path, v_holding.id), \
            "a file saved via save_document_file must pass its own secure_file_path check"

        doc = services.save_document_metadata(
            db, v_holding, v_user_id, doc_type=DocumentType.ACCOUNT_STATEMENT,
            original_name="statement.pdf", stored_name=stored_name, file_path=file_path,
            file_size=file_size, notes="Year-end statement",
        )
        assert doc.id is not None
        assert doc.display_name == "statement.pdf"  # no title given -> falls back to original_name
        assert doc.is_encrypted is False and doc.iv is None
        print("PASS: save_document_metadata() persists a document row with the file actually on disk")

        # secure_file_path must reject a path traversal attempt.
        assert not intl_utils.secure_file_path("/etc/passwd", v_holding.id), \
            "secure_file_path must reject a path outside the holding's own document directory"
        assert not intl_utils.secure_file_path(
            os.path.join(os.path.dirname(file_path), "..", "999", "x.pdf"), v_holding.id
        ), "secure_file_path must reject a path traversal into another holding's directory"
        print("PASS: secure_file_path() rejects paths outside the holding's own document directory")

        # get_vault_documents / vault_summary
        vault_docs = services.get_vault_documents(v_user_id)
        assert len(vault_docs) == 1 and vault_docs[0].id == doc.id
        vault_docs_q = services.get_vault_documents(v_user_id, q="statement")
        assert len(vault_docs_q) == 1
        vault_docs_miss = services.get_vault_documents(v_user_id, q="nonexistent-term-xyz")
        assert len(vault_docs_miss) == 0
        summary = services.vault_summary(v_user_id)
        assert summary["total"] == 1
        assert any(a["asset_type"] == InternationalAssetType.FOREIGN_BANK_ACCOUNT and a["count"] == 1
                   for a in summary["by_asset_type"]), summary
        print("PASS: get_vault_documents()/vault_summary() list and filter documents correctly")

        # delete_document enforces ownership (IDOR check) before removing metadata.
        ok, delerr = services.delete_document(db, doc, user_id=999999)
        assert ok is False and delerr is not None, "delete_document must refuse a mismatched user_id"
        assert db.session.get(InternationalHoldingDocument, doc.id) is not None
        print("PASS: delete_document() refuses to delete a document belonging to a different user")

        # delete_holding_permanently cascades document ROWS via the ORM
        # relationship (file itself is the route's job -- see routes.py's
        # delete_holding, tested over HTTP below).
        v_holding_id = v_holding.id
        services.archive_holding(v_holding)
        services.delete_holding_permanently(v_holding)
        assert db.session.get(InternationalHolding, v_holding_id) is None
        assert InternationalHoldingDocument.query.filter_by(holding_id=v_holding_id).count() == 0, \
            "delete_holding_permanently must cascade-delete document rows"
        print("PASS: delete_holding_permanently() cascade-deletes document metadata rows")

        intl_utils.delete_document_file(file_path)  # clean up the scratch file itself
        db.session.delete(u6)
        db.session.commit()

    # ── 12b. Document Vault end-to-end HTTP ──
    dv_page = client.get('/international/holdings/add')
    dv_csrf = get_csrf(dv_page.data)
    r = client.post('/international/holdings/add', data={
        'csrf_token': dv_csrf, 'asset_type': InternationalAssetType.FOREIGN_BANK_ACCOUNT,
        'name': 'Singapore DBS', 'native_currency': 'USD', 'current_value_native': '5000',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        u7 = User.query.filter_by(email="international_http_test@example.com").first()
        dv_holding = InternationalHolding.query.filter_by(user_id=u7.id, name='Singapore DBS').first()
        assert dv_holding is not None
        dv_holding_id = dv_holding.id

    detail_for_upload = client.get(f'/international/holdings/{dv_holding_id}')
    upload_csrf = get_csrf(detail_for_upload.data)
    r = client.post(f'/international/holdings/{dv_holding_id}/documents/upload', data={
        'csrf_token': upload_csrf, 'doc_type': DocumentType.ACCOUNT_STATEMENT,
        'doc_title': 'DBS Statement', 'doc_notes': 'Test upload',
        'document': (BytesIO(b'%PDF-1.4 fake pdf'), 'dbs_statement.pdf'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200
    body_upload = r.get_data(as_text=True)
    assert 'DBS Statement' in body_upload
    print("PASS: document upload via real HTTP multipart form works and shows on the holding detail page")

    with app.app_context():
        uploaded_doc = InternationalHoldingDocument.query.filter_by(holding_id=dv_holding_id).first()
        assert uploaded_doc is not None
        assert uploaded_doc.original_name == 'dbs_statement.pdf'
        doc_id = uploaded_doc.id

    dl = client.get(f'/international/documents/{doc_id}/download')
    assert dl.status_code == 200
    assert dl.data == b'%PDF-1.4 fake pdf'
    print("PASS: document download serves back the exact uploaded bytes")

    pv = client.get(f'/international/documents/{doc_id}/preview')
    assert pv.status_code == 200
    print("PASS: PDF document preview route works")

    # Wrong file type is rejected by validate_document before ever touching disk.
    bad_csrf = get_csrf(client.get(f'/international/holdings/{dv_holding_id}').data)
    r = client.post(f'/international/holdings/{dv_holding_id}/documents/upload', data={
        'csrf_token': bad_csrf, 'doc_type': DocumentType.OTHER_DOCUMENTS,
        'document': (BytesIO(b'not a real exe but wrong extension'), 'malware.exe'),
    }, content_type='multipart/form-data', follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert InternationalHoldingDocument.query.filter_by(
            holding_id=dv_holding_id, original_name='malware.exe').count() == 0, \
            "a disallowed file extension must be rejected, not saved"
    print("PASS: disallowed file extension (.exe) is rejected by validate_document")

    # Document Vault standalone page renders and lists the uploaded document.
    vault_page = client.get('/international/documents')
    assert vault_page.status_code == 200
    vault_body = vault_page.get_data(as_text=True)
    assert 'DBS Statement' in vault_body
    assert 'icConfirmAction' in vault_body, \
        "Document Vault delete must use the branded confirm modal, not a plain confirm() dialog"
    print("PASS: standalone Document Vault page renders and lists documents across holdings")

    # Delete via the vault (next=vault) redirects back to the vault, not
    # the holding detail page.
    del_csrf = get_csrf(vault_page.data)
    r = client.post(f'/international/documents/{doc_id}/delete', data={
        'csrf_token': del_csrf, 'next': 'vault',
    }, follow_redirects=True)
    assert r.status_code == 200
    assert 'Document deleted' in r.get_data(as_text=True) or '/international/documents' in r.request.path
    with app.app_context():
        assert db.session.get(InternationalHoldingDocument, doc_id) is None, \
            "document metadata row must be gone after delete"
    print("PASS: document delete via real HTTP form works and removes the metadata row")

    # Archiving then permanently deleting the holding must also remove
    # the physical file from disk (routes.py's delete_holding cleanup —
    # service-level cascade is already covered above, this proves the
    # route's file cleanup on top of it).
    detail_page3 = client.get(f'/international/holdings/{dv_holding_id}')
    upload_csrf2 = get_csrf(detail_page3.data)
    client.post(f'/international/holdings/{dv_holding_id}/documents/upload', data={
        'csrf_token': upload_csrf2, 'doc_type': DocumentType.OTHER_DOCUMENTS,
        'document': (BytesIO(b'to be permanently deleted'), 'temp.pdf'),
    }, content_type='multipart/form-data', follow_redirects=True)
    with app.app_context():
        temp_doc = InternationalHoldingDocument.query.filter_by(
            holding_id=dv_holding_id, original_name='temp.pdf').first()
        assert temp_doc is not None
        temp_doc_path = temp_doc.file_path
        assert os.path.exists(temp_doc_path)

    archive_csrf = get_csrf(client.get(f'/international/holdings/{dv_holding_id}').data)
    client.post(f'/international/holdings/{dv_holding_id}/archive', data={'csrf_token': archive_csrf}, follow_redirects=True)
    del_holding_page = client.get(f'/international/holdings/{dv_holding_id}')
    del_holding_csrf = get_csrf(del_holding_page.data)
    r = client.post(f'/international/holdings/{dv_holding_id}/delete', data={'csrf_token': del_holding_csrf}, follow_redirects=True)
    assert r.status_code == 200
    assert not os.path.exists(temp_doc_path), \
        "permanently deleting a holding must remove its documents' physical files from disk, not just the DB rows"
    print("PASS: permanently deleting a holding removes its documents' physical files from disk (not just DB rows)")

    # ── 13. TCS on LRS remittances (Batch 9.4, reworked Batch 10.1, Oct 2026) ──
    # Rules are now a dated table (international_centre/tcs_rules.py): the
    # aggregate threshold is ₹7L before 1 Apr 2025 and ₹10L from then on, and
    # the rate depends on purpose (education/medical 5% -> 2% from 1 Apr 2026,
    # loan-funded education 0.5% -> nil from 1 Apr 2025, everything else 20%).
    from international_centre import tcs_rules

    # 13.0 — pure rule table, no database involved.
    assert tcs_rules.threshold_for(date(2020, 9, 30)) is None, "no TCS on LRS before 1 Oct 2020"
    assert tcs_rules.threshold_for(date(2020, 10, 1)) == 700_000
    assert tcs_rules.threshold_for(date(2025, 3, 31)) == 700_000
    assert tcs_rules.threshold_for(date(2025, 4, 1)) == 1_000_000
    assert tcs_rules.threshold_for(date(2026, 4, 1)) == 1_000_000
    _t, _r = tcs_rules.rule_for(date(2023, 9, 30))
    assert _r[tcs_rules.GENERAL] == 0.05, "general rate was 5% until 30 Sep 2023"
    _t, _r = tcs_rules.rule_for(date(2023, 10, 1))
    assert _r[tcs_rules.GENERAL] == 0.20, "general rate became 20% on 1 Oct 2023"
    _t, _r = tcs_rules.rule_for(date(2026, 3, 31))
    assert _r[tcs_rules.EDU_MEDICAL] == 0.05 and _r[tcs_rules.EDU_LOAN] == 0.0
    _t, _r = tcs_rules.rule_for(date(2026, 4, 1))
    assert _r[tcs_rules.EDU_MEDICAL] == 0.02
    assert tcs_rules.category_for(True, False, True) == tcs_rules.EDU_LOAN
    assert tcs_rules.category_for(True, False, False) == tcs_rules.EDU_MEDICAL
    assert tcs_rules.category_for(False, True, False) == tcs_rules.EDU_MEDICAL
    assert tcs_rules.category_for(False, False, True) == tcs_rules.GENERAL, "loan flag is meaningless outside education"
    print("PASS: tcs_rules picks the right threshold/rates on both sides of each effective date")

    with app.app_context():
        u7 = User.query.filter_by(email="international_tcs_test@example.com").first()
        if u7:
            RemittanceRecord.query.filter_by(user_id=u7.id).delete()
            db.session.delete(u7)
            db.session.commit()
        u7 = User(name="TCS Test", email="international_tcs_test@example.com", password="unused")
        db.session.add(u7)
        db.session.commit()
        tcs_user_id = u7.id
        GEN = RemittancePurpose.INVESTMENT_SECURITIES

        # FY 2026-27: threshold ₹10L. 5L + 4L = 9L is still under it.
        r_below = services.add_remittance(tcs_user_id, {"date": "2026-05-01", "amount_inr": "500000", "purpose": GEN})
        r_still = services.add_remittance(tcs_user_id, {"date": "2026-06-01", "amount_inr": "400000", "purpose": GEN})
        assert r_below.tcs_amount_inr == 0.0 and r_still.tcs_amount_inr == 0.0, (r_below.tcs_amount_inr, r_still.tcs_amount_inr)
        print("PASS: ₹9L aggregate in FY 2026-27 collects no TCS (threshold is ₹10L, not the old ₹7L)")

        # 3L more -> 12L aggregate, 2L above the threshold, general rate 20%.
        r_cross = services.add_remittance(tcs_user_id, {"date": "2026-07-01", "amount_inr": "300000", "purpose": GEN})
        assert r_cross.tcs_amount_inr == 40_000.0, r_cross.tcs_amount_inr
        print("PASS: only the portion above ₹10L is taxed, at 20% for a general purpose (₹40,000)")

        r_above = services.add_remittance(tcs_user_id, {"date": "2026-08-01", "amount_inr": "100000", "purpose": GEN})
        assert r_above.tcs_amount_inr == 20_000.0, r_above.tcs_amount_inr
        print("PASS: a remittance entirely above the threshold is taxed in full at 20%")

        # Purpose-specific rates for the same FY, already over the threshold.
        r_edu = services.add_remittance(tcs_user_id, {"date": "2026-09-01", "amount_inr": "200000", "purpose": RemittancePurpose.EDUCATION})
        r_edu_loan = services.add_remittance(tcs_user_id, {"date": "2026-09-02", "amount_inr": "200000", "purpose": RemittancePurpose.EDUCATION, "education_loan_funded": "on"})
        r_med = services.add_remittance(tcs_user_id, {"date": "2026-09-03", "amount_inr": "200000", "purpose": RemittancePurpose.MEDICAL})
        assert r_edu.tcs_amount_inr == 4_000.0, r_edu.tcs_amount_inr           # 2%
        assert r_edu_loan.education_loan_funded is True
        assert r_edu_loan.tcs_amount_inr == 0.0, r_edu_loan.tcs_amount_inr      # loan-funded: nil
        assert r_med.tcs_amount_inr == 4_000.0, r_med.tcs_amount_inr            # 2%
        print("PASS: FY 2026-27 education/medical collect 2% and loan-funded education collects nothing, above the threshold")

        # A loan tick on a NON-education purpose is ignored.
        r_ignored = services.add_remittance(tcs_user_id, {"date": "2026-09-04", "amount_inr": "100000", "purpose": GEN, "education_loan_funded": "on"})
        assert r_ignored.education_loan_funded is False and r_ignored.tcs_amount_inr == 20_000.0
        print("PASS: the education-loan flag is ignored for non-education purposes")

        status_tcs = services.get_lrs_status(tcs_user_id, anchor_date=date(2026, 10, 1))
        expected_total = 40_000 + 20_000 + 4_000 + 0 + 4_000 + 20_000
        assert status_tcs["total_tcs_inr"] == expected_total, (status_tcs["total_tcs_inr"], expected_total)
        assert status_tcs["tcs_threshold_inr"] == 1_000_000 and status_tcs["tcs_free_remaining_inr"] == 0.0
        assert status_tcs["tcs_rule"]["edu_medical_pct"] == 2.0
        print(f"PASS: get_lrs_status() reports total TCS (₹{status_tcs['total_tcs_inr']:,.0f}), threshold and rule summary")

        # Earlier financial years keep the rules that applied THEN.
        r_fy25 = services.add_remittance(tcs_user_id, {"date": "2025-01-10", "amount_inr": "800000", "purpose": GEN})
        assert r_fy25.tcs_amount_inr == 20_000.0, r_fy25.tcs_amount_inr  # FY 2024-25: 1L over ₹7L at 20%
        r_fy26_a = services.add_remittance(tcs_user_id, {"date": "2025-06-01", "amount_inr": "900000", "purpose": GEN})
        assert r_fy26_a.tcs_amount_inr == 0.0, r_fy26_a.tcs_amount_inr    # FY 2025-26: under ₹10L (would have been taxed at ₹7L)
        r_fy26_e = services.add_remittance(tcs_user_id, {"date": "2025-07-01", "amount_inr": "300000", "purpose": RemittancePurpose.EDUCATION})
        assert r_fy26_e.tcs_amount_inr == 10_000.0, r_fy26_e.tcs_amount_inr  # 2L above ₹10L at 5% (rate cut to 2% only from Apr 2026)
        r_fy26_l = services.add_remittance(tcs_user_id, {"date": "2025-07-02", "amount_inr": "300000", "purpose": RemittancePurpose.EDUCATION, "education_loan_funded": "on"})
        assert r_fy26_l.tcs_amount_inr == 0.0, r_fy26_l.tcs_amount_inr
        r_fy22 = services.add_remittance(tcs_user_id, {"date": "2021-12-01", "amount_inr": "900000", "purpose": GEN})
        assert r_fy22.tcs_amount_inr == 10_000.0, r_fy22.tcs_amount_inr  # FY 2021-22: 2L over ₹7L at the then-5%
        r_fy19 = services.add_remittance(tcs_user_id, {"date": "2019-12-01", "amount_inr": "900000", "purpose": GEN})
        assert r_fy19.tcs_amount_inr == 0.0, r_fy19.tcs_amount_inr        # no TCS on LRS yet
        print("PASS: each financial year is taxed under the rules that applied on its own dates (7L/10L, 5%/20%/2%, pre-2020 none)")

        # Order of entry must not matter: recompute is chronological.
        for _old in RemittanceRecord.query.filter_by(user_id=tcs_user_id).all():
            db.session.delete(_old)  # per-object delete (not bulk) so the session's identity map stays in sync
        db.session.commit()
        late = services.add_remittance(tcs_user_id, {"date": "2026-12-01", "amount_inr": "600000", "purpose": GEN})
        early = services.add_remittance(tcs_user_id, {"date": "2026-05-01", "amount_inr": "700000", "purpose": GEN})
        db.session.refresh(late)
        # Chronologically: May 7L (under 10L, nil), then Dec 6L -> 13L, 3L above -> ₹60,000.
        assert early.tcs_amount_inr == 0.0 and late.tcs_amount_inr == 60_000.0, (early.tcs_amount_inr, late.tcs_amount_inr)
        print("PASS: TCS is assigned in date order even when remittances are entered out of order")

        # Deleting a remittance recomputes the rest of its year.
        services.delete_remittance(early)
        db.session.refresh(late)
        assert late.tcs_amount_inr == 0.0, late.tcs_amount_inr  # 6L alone is under the threshold now
        print("PASS: deleting a remittance recomputes TCS for the rest of that financial year")

        # recompute_all_tcs repairs tampered/stale values and is idempotent.
        late.tcs_amount_inr = 12345.0
        db.session.commit()
        n = services.recompute_all_tcs(tcs_user_id)
        db.session.refresh(late)
        assert n == 1 and late.tcs_amount_inr == 0.0
        assert services.recompute_all_tcs(tcs_user_id) == 1 and late.tcs_amount_inr == 0.0
        print("PASS: recompute_all_tcs() corrects stale stored values and is idempotent")

        for _old in RemittanceRecord.query.filter_by(user_id=tcs_user_id).all():
            db.session.delete(_old)
        db.session.delete(u7)
        db.session.commit()

    # ── 14. DTAA / Form 67 dividend withholding summary (Batch 9.5, Sep 2026) ──
    with app.app_context():
        u8 = User.query.filter_by(email="international_dtaa_test@example.com").first()
        if u8:
            InternationalHolding.query.filter_by(user_id=u8.id).delete()
            db.session.delete(u8)
            db.session.commit()
        u8 = User(name="DTAA Test", email="international_dtaa_test@example.com", password="unused")
        db.session.add(u8)
        db.session.commit()
        dtaa_user_id = u8.id

        div_holding, err = services.create_holding(dtaa_user_id, {
            "asset_type": InternationalAssetType.US_STOCK,
            "name": "Microsoft Corp.", "ticker": "MSFT", "country": "United States",
            "native_currency": "USD", "quantity": "20", "avg_cost_native": "300",
        })
        assert err is None, err

        # Dividend WITH gross/withheld detail entered.
        services.add_transaction(div_holding, {
            "date": "2026-06-15", "txn_type": "DIVIDEND",
            "amount_native": "80", "gross_amount_native": "100", "tax_withheld_native": "20",
        })
        # A second dividend in the same FY, no gross/withheld given at
        # all -> must fall back to amount_native as the gross figure,
        # with zero withheld (never crash on the missing optional data).
        services.add_transaction(div_holding, {
            "date": "2026-09-01", "txn_type": "DIVIDEND", "amount_native": "50",
        })
        db.session.commit()

        dtaa = services.get_dtaa_summary(dtaa_user_id, fy_start_year=2026)
        assert dtaa["fy_label"] == "FY 2026-27", dtaa["fy_label"]
        row = next(r for r in dtaa["rows"] if r["holding"].id == div_holding.id)
        assert row["gross_native"] == 150.0, row  # 100 + 50
        assert row["withheld_native"] == 20.0, row
        assert row["net_native"] == 130.0, row  # 80 + 50
        # USD-native holding -> fx_rate_used is 1.0 (fake rate), so the
        # INR bridge is just usd_to_inr() at the fake 83.0 INR/USD rate.
        assert row["gross_inr"] == 12450.0, row  # 150 * 83.0
        assert row["withheld_inr"] == 1660.0, row  # 20 * 83.0
        print("PASS: get_dtaa_summary() aggregates gross/withheld/net dividend figures per holding, with a fallback for dividends missing the optional detail")

        # Validator: withheld cannot exceed gross.
        from international_centre.validators import validate_transaction
        bad_errors = validate_transaction({
            "date": "2026-06-15", "txn_type": "DIVIDEND", "amount_native": "80",
            "gross_amount_native": "100", "tax_withheld_native": "150",
        })
        assert any("cannot exceed" in e for e in bad_errors), bad_errors
        print("PASS: validate_transaction() rejects tax withheld greater than gross dividend")

        # Plain session.delete() per holding, NOT a bulk Query.delete()
        # -- a bulk delete skips the ORM cascade="all, delete-orphan" on
        # transactions, leaving orphaned rows that a later test's newly
        # created holding can silently "inherit" via SQLite rowid reuse
        # (the same class of bug Batch 5 fixed for domestic Stock/
        # StockTransaction -- see this project's own history notes).
        for h in InternationalHolding.query.filter_by(user_id=dtaa_user_id).all():
            db.session.delete(h)
        db.session.delete(u8)
        db.session.commit()

    # ── 15. LTCG/STCG capital gains classification (Batch 9.6, Sep 2026) ──
    with app.app_context():
        from international_centre.utils import is_long_term
        from international_centre.services import classify_capital_gains

        # is_long_term(): exactly-24-months boundary must NOT be long-term
        # (the rule is "more than 24 months"), one day past it must be.
        assert is_long_term(date(2024, 1, 15), date(2026, 1, 16)) is True, \
            "1 day past the 24-month mark must be long-term"
        assert is_long_term(date(2024, 1, 15), date(2026, 1, 15)) is False, \
            "exactly 24 months must NOT be long-term (rule is 'more than 24 months')"
        assert is_long_term(date(2024, 1, 15), date(2025, 6, 1)) is False, \
            "well under 24 months must be short-term"
        print("PASS: is_long_term() applies the >24-calendar-month boundary correctly, including the exact-boundary edge case")

        u9 = User.query.filter_by(email="international_cg_test@example.com").first()
        if u9:
            InternationalHolding.query.filter_by(user_id=u9.id).delete()
            db.session.delete(u9)
            db.session.commit()
        u9 = User(name="CapGains Test", email="international_cg_test@example.com", password="unused")
        db.session.add(u9)
        db.session.commit()
        cg_user_id = u9.id

        cg_holding, err = services.create_holding(cg_user_id, {
            "asset_type": InternationalAssetType.US_STOCK,
            "name": "Amazon.com Inc.", "ticker": "AMZN", "country": "United States",
            "native_currency": "USD", "quantity": "10", "avg_cost_native": "100",
        })
        assert err is None, err
        # Two FIFO lots: an older long-term-eligible lot, then a newer
        # short-term one -- a SELL spanning both must split into two
        # correctly-classified gain records.
        services.add_transaction(cg_holding, {
            "date": "2023-01-01", "txn_type": "BUY", "quantity": "10", "price_native": "100", "amount_native": "1000",
        })
        services.add_transaction(cg_holding, {
            "date": "2026-01-01", "txn_type": "BUY", "quantity": "10", "price_native": "150", "amount_native": "1500",
        })
        services.add_transaction(cg_holding, {
            "date": "2026-06-01", "txn_type": "SELL", "quantity": "15", "price_native": "200", "amount_native": "3000",
        })
        db.session.commit()

        gains = classify_capital_gains(cg_holding)
        assert len(gains) == 2, gains
        ltcg_row = next(g for g in gains if g["classification"] == "LTCG")
        stcg_row = next(g for g in gains if g["classification"] == "STCG")
        assert ltcg_row["quantity"] == 10.0, ltcg_row  # all of the 2023 lot (held > 24mo by 2026-06-01)
        assert ltcg_row["acquisition_date"] == date(2023, 1, 1), ltcg_row
        assert ltcg_row["gain_native"] == 10 * (200 - 100), ltcg_row
        assert stcg_row["quantity"] == 5.0, stcg_row  # remaining 5 units drawn from the 2026 lot (< 24mo)
        assert stcg_row["gain_native"] == 5 * (200 - 150), stcg_row
        print("PASS: classify_capital_gains() FIFO-matches a SELL spanning two lots into correctly-classified LTCG/STCG records")

        cg_summary = services.get_capital_gains_summary(cg_user_id, fy_start_year=2026)
        assert cg_summary["fy_label"] == "FY 2026-27", cg_summary["fy_label"]
        assert cg_summary["ltcg_total_usd"] == 1000.0, cg_summary  # (200-100)*10 * rate 1.0
        assert cg_summary["stcg_total_usd"] == 250.0, cg_summary  # (200-150)*5 * rate 1.0
        print("PASS: get_capital_gains_summary() correctly totals LTCG/STCG gains for the FY, bridged to USD")

        for h in InternationalHolding.query.filter_by(user_id=cg_user_id).all():
            db.session.delete(h)
        db.session.delete(u9)
        db.session.commit()

    # ── 16. HTTP: TCS column, dividend fields, DTAA/capital-gains report pages ──
    remit_page2 = client.get('/international/remittances')
    remit_csrf2 = get_csrf(remit_page2.data)
    # A remittance large enough on its own to cross the ₹10L threshold,
    # against the international_http_test user used throughout section 9.
    r = client.post('/international/remittances/add', data={
        'csrf_token': remit_csrf2, 'date': '2026-05-10', 'amount_inr': '1500000',
        'purpose': RemittancePurpose.INVESTMENT_SECURITIES,
    }, follow_redirects=True)
    assert r.status_code == 200
    remit_body = client.get('/international/remittances').get_data(as_text=True)
    assert 'Estimated TCS This FY' in remit_body, "remittances page must show the TCS summary tile once TCS has been collected"
    print("PASS: TCS column + summary tile render on the remittances page via real HTTP")

    # Dividend transaction with gross/withheld via real HTTP form.
    detail_page4 = client.get(f'/international/holdings/{h_id}')
    div_csrf = get_csrf(detail_page4.data)
    r = client.post(f'/international/holdings/{h_id}/transactions/add', data={
        'csrf_token': div_csrf, 'date': '2026-06-01', 'txn_type': 'DIVIDEND',
        'amount_native': '80', 'gross_amount_native': '100', 'tax_withheld_native': '20',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        div_txn = InternationalTransaction.query.filter_by(holding_id=h_id, txn_type='DIVIDEND').first()
        assert div_txn is not None
        assert div_txn.gross_amount_native == 100.0, div_txn.gross_amount_native
        assert div_txn.tax_withheld_native == 20.0, div_txn.tax_withheld_native
    print("PASS: dividend gross/withheld fields submit and persist via real HTTP form")

    dtaa_page = client.get('/international/dtaa-summary')
    assert dtaa_page.status_code == 200
    assert 'DTAA' in dtaa_page.get_data(as_text=True)
    print("PASS: DTAA / Form 67 summary report page renders")

    cg_page = client.get('/international/capital-gains')
    assert cg_page.status_code == 200
    assert 'Capital Gains' in cg_page.get_data(as_text=True)
    print("PASS: Capital Gains (LTCG/STCG) report page renders")

    # ── 17. RSU/ESPP vesting tranches (Batch 9.7, Sep 2026) ──
    with app.app_context():
        from international_centre.models import VestingTranche
        from international_centre.validators import validate_vesting_tranche

        u10 = User.query.filter_by(email="international_vesting_test@example.com").first()
        if u10:
            InternationalHolding.query.filter_by(user_id=u10.id).delete()
            db.session.delete(u10)
            db.session.commit()
        u10 = User(name="Vesting Test", email="international_vesting_test@example.com", password="unused")
        db.session.add(u10)
        db.session.commit()
        vest_user_id = u10.id

        rsu_holding, err = services.create_holding(vest_user_id, {
            "asset_type": InternationalAssetType.RSU_ESPP,
            "name": "Acme Corp RSU", "ticker": "ACME", "country": "United States",
            "native_currency": "USD", "quantity": "0", "avg_cost_native": "0",
        })
        assert err is None, err
        assert rsu_holding.is_rsu_espp is True

        # RSU tranche -- free grant, no purchase price.
        rsu_tranche = services.add_vesting_tranche(rsu_holding, {
            "plan_type": "RSU", "vest_date": "2025-06-01", "quantity": "100", "fmv_native": "50",
        })
        assert rsu_tranche.perquisite_value_native == 5000.0, rsu_tranche.perquisite_value_native  # 50*100, no purchase price
        assert rsu_tranche.cost_basis_native == 5000.0, rsu_tranche.cost_basis_native
        print("PASS: an RSU tranche's perquisite value is its full FMV (no purchase price to net out)")

        # ESPP tranche -- discounted purchase.
        espp_tranche = services.add_vesting_tranche(rsu_holding, {
            "plan_type": "ESPP", "vest_date": "2025-07-01", "quantity": "50",
            "fmv_native": "60", "purchase_price_native": "51",
        })
        assert espp_tranche.perquisite_value_native == 450.0, espp_tranche.perquisite_value_native  # (60-51)*50
        assert espp_tranche.cost_basis_native == 3000.0, espp_tranche.cost_basis_native  # FMV*qty, not price paid*qty
        print("PASS: an ESPP tranche's perquisite value is just the discount, but its capital-gains cost basis is still full FMV")

        # Validator: purchase price can't exceed FMV.
        bad_vest_errors = validate_vesting_tranche({
            "plan_type": "ESPP", "vest_date": "2025-07-01", "quantity": "50",
            "fmv_native": "60", "purchase_price_native": "70",
        })
        assert any("cannot exceed" in e for e in bad_vest_errors), bad_vest_errors
        print("PASS: validate_vesting_tranche() rejects a purchase price above the FMV")

        # get_vesting_perquisite_summary() aggregates both tranches for the FY.
        perq_summary = services.get_vesting_perquisite_summary(vest_user_id, fy_start_year=2025)
        assert perq_summary["fy_label"] == "FY 2025-26", perq_summary["fy_label"]
        assert len(perq_summary["rows"]) == 2, perq_summary["rows"]
        assert perq_summary["total_perquisite_inr"] == round((5000.0 + 450.0) * 83.0, 2), perq_summary
        print(f"PASS: get_vesting_perquisite_summary() totals both tranches' perquisite value for the FY (₹{perq_summary['total_perquisite_inr']:,.0f})")

        # update_vesting_tranche() / delete_vesting_tranche()
        services.update_vesting_tranche(espp_tranche, {
            "plan_type": "ESPP", "vest_date": "2025-07-01", "quantity": "50",
            "fmv_native": "60", "purchase_price_native": "55",
        })
        assert espp_tranche.perquisite_value_native == 250.0, espp_tranche.perquisite_value_native  # (60-55)*50
        print("PASS: update_vesting_tranche() recomputes perquisite_value_native from the edited fields")

        # FIFO integration with classify_capital_gains() (Batch 9.6): a
        # SELL must draw from vesting tranches exactly like a BUY lot,
        # cost-based at FMV -- oldest lot (the RSU tranche) first.
        services.add_transaction(rsu_holding, {
            "date": "2026-08-01", "txn_type": "SELL", "quantity": "120", "price_native": "70", "amount_native": "8400",
        })
        db.session.commit()

        gains = services.classify_capital_gains(rsu_holding)
        assert len(gains) == 2, gains
        rsu_gain = next(g for g in gains if g["acquisition_date"] == date(2025, 6, 1))
        espp_gain = next(g for g in gains if g["acquisition_date"] == date(2025, 7, 1))
        assert rsu_gain["quantity"] == 100.0, rsu_gain  # fully consumes the older (RSU) lot first -- FIFO
        assert rsu_gain["cost_basis_native"] == 5000.0, rsu_gain  # 100 * FMV 50, not a price "paid"
        assert rsu_gain["classification"] == "STCG", rsu_gain  # 2025-06-01 -> 2026-08-01 is under 24 months
        assert espp_gain["quantity"] == 20.0, espp_gain  # remaining 20 units drawn from the ESPP lot
        assert espp_gain["cost_basis_native"] == 1200.0, espp_gain  # 20 * FMV 60 -- FMV, not the 55/unit actually paid
        print("PASS: classify_capital_gains() FIFO-matches a SELL against vesting tranches (cost-based at FMV) exactly like a BUY lot, oldest first")

        for h in InternationalHolding.query.filter_by(user_id=vest_user_id).all():
            db.session.delete(h)
        db.session.delete(u10)
        db.session.commit()

    # ── 18. HTTP: vesting tranche CRUD, perquisite report, dashboard/LRS-history charts ──
    vest_add_page = client.get('/international/holdings/add')
    vest_csrf0 = get_csrf(vest_add_page.data)
    r = client.post('/international/holdings/add', data={
        'csrf_token': vest_csrf0, 'asset_type': InternationalAssetType.RSU_ESPP,
        'name': 'Globex Corp RSU', 'ticker': 'GLBX', 'country': 'United States',
        'native_currency': 'USD', 'quantity': '1', 'avg_cost_native': '1',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        vest_h = InternationalHolding.query.filter_by(user_id=u2.id, name='Globex Corp RSU').first()
        assert vest_h is not None
        vest_h_id = vest_h.id

    vest_detail = client.get(f'/international/holdings/{vest_h_id}')
    assert 'Vesting Tranches' in vest_detail.get_data(as_text=True)
    vest_add_csrf = get_csrf(vest_detail.data)
    r = client.post(f'/international/holdings/{vest_h_id}/vesting/add', data={
        'csrf_token': vest_add_csrf, 'plan_type': 'RSU', 'vest_date': '2026-05-01',
        'quantity': '25', 'fmv_native': '80',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        added_tranche = VestingTranche.query.filter_by(holding_id=vest_h_id).first()
        assert added_tranche is not None
        assert added_tranche.fmv_native == 80.0
        tranche_id = added_tranche.id
    print("PASS: vesting tranche add via real HTTP form works")

    vest_detail2 = client.get(f'/international/holdings/{vest_h_id}')
    vest_edit_csrf = get_csrf(vest_detail2.data)
    r = client.post(f'/international/vesting/{tranche_id}/edit', data={
        'csrf_token': vest_edit_csrf, 'plan_type': 'RSU', 'vest_date': '2026-05-02',
        'quantity': '25', 'fmv_native': '85',
    }, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        edited_tranche = db.session.get(VestingTranche, tranche_id)
        assert edited_tranche.fmv_native == 85.0, edited_tranche.fmv_native
    print("PASS: vesting tranche edit via real HTTP form works")

    perq_page = client.get('/international/vesting-perquisite')
    assert perq_page.status_code == 200
    assert 'Perquisite' in perq_page.get_data(as_text=True)
    print("PASS: RSU/ESPP Perquisite report page renders")

    vest_detail3 = client.get(f'/international/holdings/{vest_h_id}')
    vest_del_csrf = get_csrf(vest_detail3.data)
    r = client.post(f'/international/vesting/{tranche_id}/delete', data={'csrf_token': vest_del_csrf}, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assert db.session.get(VestingTranche, tranche_id) is None
    print("PASS: vesting tranche delete via real HTTP form works")

    # Dashboard now carries Asset Allocation / Currency Exposure charts
    # (Batch 9.8), and remittances carries the multi-year LRS history.
    dash_page = client.get('/international/')
    dash_body = dash_page.get_data(as_text=True)
    assert 'assetAllocationChart' in dash_body and 'currencyExposureChart' in dash_body
    print("PASS: International dashboard renders the Asset Allocation and Currency Exposure charts")

    lrs_hist_body = client.get('/international/remittances').get_data(as_text=True)
    assert 'LRS History' in lrs_hist_body and 'lrsHistoryChart' in lrs_hist_body
    print("PASS: Remittances page renders the multi-year LRS History section")

    with app.app_context():
        history = services.get_lrs_history(u2.id, years=5)
        assert len(history) == 5, history
        # Every year is present even with zero remittances -- a quiet
        # year must show as $0, not be skipped -- and years are oldest
        # first, ending with the current FY.
        assert all("total_usd" in yr and "fy_label" in yr for yr in history)
        assert history[-1]["fy_start"] > history[0]["fy_start"], history
    print("PASS: get_lrs_history() returns all requested years oldest-first, including years with zero remittances")

    # ── 19. Holdings list: search / filter / sort / flags (Batch 10.2, Oct 2026) ──
    from international_centre.models import InternationalTimeline, TimelineEvent
    from datetime import datetime as _dt

    with app.app_context():
        u9 = User.query.filter_by(email="international_list_test@example.com").first()
        if u9:
            for _h in InternationalHolding.query.filter_by(user_id=u9.id).all():
                db.session.delete(_h)
            db.session.delete(u9)
            db.session.commit()
        u9 = User(name="List Test", email="international_list_test@example.com", password="unused")
        other = User(name="Other", email="international_list_other@example.com", password="unused")
        db.session.add_all([u9, other])
        db.session.commit()
        list_uid, other_uid = u9.id, other.id

        def mk(uid, **kw):
            data = {"native_currency": "USD", **kw}
            h, err = services.create_holding(uid, data)
            assert err is None, err
            return h

        h_aapl = mk(list_uid, asset_type=InternationalAssetType.US_STOCK, name="Apple Inc.", ticker="AAPL",
                    country="United States", broker_or_institution="Interactive Brokers", quantity="10", avg_cost_native="100")
        h_vod = mk(list_uid, asset_type=InternationalAssetType.US_STOCK, name="Vodafone Group", ticker="VOD",
                   country="United Kingdom", native_currency="GBP", quantity="100", avg_cost_native="10")
        h_bank = mk(list_uid, asset_type=InternationalAssetType.FOREIGN_BANK_ACCOUNT, name="Chase Checking",
                    country="United States", current_value_native="5000")
        h_other_user = mk(other_uid, asset_type=InternationalAssetType.US_STOCK, name="Apple Secret", ticker="AAPL",
                          country="United States", quantity="1", avg_cost_native="1")
        services.update_holding  # (exists)

        # Make Apple clearly the biggest, with a 50% gain.
        h_aapl.live_price_native = 150.0
        h_aapl.current_value_native = 1500.0
        h_aapl.usd_value = 1500.0
        h_aapl.price_updated_at = _dt.utcnow()          # fresh price
        h_vod.price_updated_at = _dt.utcnow() - timedelta(days=10)  # stale price
        h_vod.usd_value = 1300.0
        h_bank.usd_value = 5000.0
        db.session.commit()

        rows, facets = services.search_holdings(list_uid)
        assert [r["holding"].name for r in rows] == ["Chase Checking", "Apple Inc.", "Vodafone Group"], [r["holding"].name for r in rows]
        assert facets["countries"] == ["United Kingdom", "United States"] and facets["currencies"] == ["GBP", "USD"]
        print("PASS: search_holdings() defaults to value high->low and builds filter facets from the user's own holdings")

        assert [r["holding"].name for r in services.search_holdings(list_uid, sort="name_az")[0]] == ["Apple Inc.", "Chase Checking", "Vodafone Group"]
        assert [r["holding"].name for r in services.search_holdings(list_uid, sort="value_low")[0]][0] == "Vodafone Group"
        print("PASS: sort options reorder the list")

        assert [r["holding"].name for r in services.search_holdings(list_uid, q="interactive")[0]] == ["Apple Inc."], "search must match broker"
        assert [r["holding"].name for r in services.search_holdings(list_uid, q="vod")[0]] == ["Vodafone Group"], "search must match ticker"
        assert [r["holding"].name for r in services.search_holdings(list_uid, q="UNITED KINGDOM")[0]] == ["Vodafone Group"], "search is case-insensitive"
        assert services.search_holdings(list_uid, q="zzz-no-match")[0] == []
        assert len(services.search_holdings(list_uid, asset_type=InternationalAssetType.US_STOCK)[0]) == 2
        assert [r["holding"].name for r in services.search_holdings(list_uid, currency="GBP")[0]] == ["Vodafone Group"]
        assert len(services.search_holdings(list_uid, country="United States")[0]) == 2
        print("PASS: search matches name/ticker/country/broker case-insensitively and type/country/currency filters work")

        assert "Apple Secret" not in [r["holding"].name for r in services.search_holdings(list_uid, q="apple")[0]]
        assert [r["holding"].name for r in services.search_holdings(other_uid)[0]] == ["Apple Secret"]
        print("PASS: search_holdings() never returns another user's holdings")

        stale_names = {r["holding"].name for r in services.search_holdings(list_uid, flag="stale")[0]}
        assert stale_names == {"Vodafone Group"}, stale_names  # bank value was just set => fresh; Apple fresh price
        gap_names = {r["holding"].name for r in services.search_holdings(list_uid, flag="no_nominee")[0]}
        assert gap_names == {"Apple Inc.", "Vodafone Group", "Chase Checking"}
        print("PASS: 'needs a refresh' and 'nominee gaps' flags select the right holdings")

        # gain sort: only holdings with a gain figure rank; the bank account (no P&L) goes last
        gain_rows = services.search_holdings(list_uid, sort="gain_high")[0]
        assert gain_rows[0]["holding"].name == "Apple Inc." and gain_rows[-1]["holding"].name == "Chase Checking", [r["holding"].name for r in gain_rows]
        print("PASS: gain sort ranks holdings with a gain figure first and leaves bank accounts (no P&L) last")

        # ── Alerts ──
        alerts = services.get_alerts(list_uid)
        msgs = " | ".join(a["message"] for a in alerts)
        assert "older than" in msgs, msgs               # stale price
        assert "no nominee" in msgs, msgs               # nominee gaps
        assert all(a["level"] in ("danger", "warning", "info") and a["link"][0].startswith("international_centre.") for a in alerts)
        # LRS alert appears when the cap is near.
        services.add_remittance(list_uid, {"date": date.today().isoformat(), "amount_inr": str(int(LRS_ANNUAL_LIMIT_USD * 83 * 0.9)),
                                           "purpose": RemittancePurpose.INVESTMENT_SECURITIES})
        lrs_alerts = [a for a in services.get_alerts(list_uid) if "LRS" in a["message"]]
        assert lrs_alerts and lrs_alerts[0]["level"] == "warning", lrs_alerts
        # An unconvertible holding raises a danger alert.
        h_vod.fx_rate_used = None
        db.session.commit()
        assert any(a["level"] == "danger" and "couldn't be converted" in a["message"] for a in services.get_alerts(list_uid))
        assert [r["holding"].name for r in services.search_holdings(list_uid, flag="unconverted")[0]] == ["Vodafone Group"]
        print("PASS: dashboard alerts cover LRS usage, stale prices, nominee gaps and unconverted holdings")

    # HTTP: list page
    client_l = app.test_client()
    pg = client_l.get('/signup')
    client_l.post('/signup', data={'csrf_token': get_csrf(pg.data), 'name': 'List HTTP', 'email': 'international_list_http@example.com',
                                   'password': PASSWORD, 'confirm_password': PASSWORD}, follow_redirects=True)
    add_csrf = get_csrf(client_l.get('/international/holdings/add').data)
    for nm, tk, ctry in [("Microsoft Corp", "MSFT", "United States"), ("Nestle SA", "NESN", "Switzerland")]:
        client_l.post('/international/holdings/add', data={'csrf_token': add_csrf, 'asset_type': InternationalAssetType.US_STOCK,
                      'name': nm, 'ticker': tk, 'country': ctry, 'native_currency': 'USD', 'quantity': '2', 'avg_cost_native': '100'},
                      follow_redirects=True)
        add_csrf = get_csrf(client_l.get('/international/holdings/add').data)

    page_all = client_l.get('/international/holdings')
    assert page_all.status_code == 200
    body_all = page_all.get_data(as_text=True)
    assert 'Microsoft Corp' in body_all and 'Nestle SA' in body_all and 'Apple Secret' not in body_all and 'Chase Checking' not in body_all
    page_f = client_l.get('/international/holdings?q=nestle&sort=name_az')
    body_f = page_f.get_data(as_text=True)
    assert 'Nestle SA' in body_f and 'Microsoft Corp' not in body_f and 'Active filters' in body_f and '(filtered)' in body_f
    page_none = client_l.get('/international/holdings?q=zzzz')
    assert 'No holdings match your search' in page_none.get_data(as_text=True)
    # Hostile / garbage parameters fall back safely instead of erroring.
    assert client_l.get('/international/holdings?sort=__import__&flag=drop&asset_type=%00&currency=%27').status_code == 200
    print("PASS: Holdings page renders, filters by search, shows filter chips and the empty state, and survives junk parameters via real HTTP")

    dash_l = client_l.get('/international/').get_data(as_text=True)
    assert 'Tax &amp; Compliance' in dash_l and 'Schedule FA Report' in dash_l and 'Top Holdings' in dash_l
    assert 'asset_type' in dash_l and 'currency' in dash_l and 'icListUrl' in dash_l
    assert 'ic-banner' in dash_l, "stale-price / nominee banners should show for freshly added holdings"
    print("PASS: dashboard shows grouped Tax & Compliance menu, alert banners, Top Holdings, and chart click-through wiring")

    # ── 20. Holding detail: P&L, freshness, value history, timeline (Batch 10.3, Oct 2026) ──
    with app.app_context():
        u10 = User.query.filter_by(email="international_detail_test@example.com").first()
        if u10:
            for _h in InternationalHolding.query.filter_by(user_id=u10.id).all():
                db.session.delete(_h)
            db.session.delete(u10)
            db.session.commit()
        u10 = User(name="Detail Test", email="international_detail_test@example.com", password="unused")
        db.session.add(u10)
        db.session.commit()
        det_uid = u10.id

        hd, err = services.create_holding(det_uid, {
            "asset_type": InternationalAssetType.US_STOCK, "name": "Detail Corp", "ticker": "DTL",
            "country": "United States", "native_currency": "USD", "quantity": "10", "avg_cost_native": "100",
        })
        assert err is None
        events = [e.event_type for e in services.get_timeline(hd)]
        assert events == [TimelineEvent.CREATED], events
        print("PASS: creating a holding writes a 'Holding Created' timeline entry")

        services.add_transaction(hd, {"date": "2026-01-10", "txn_type": "BUY", "quantity": "10", "price_native": "100", "amount_native": "1000"})
        hd.live_price_native, hd.current_value_native, hd.usd_value = 150.0, 1500.0, 1500.0
        services.add_transaction(hd, {"date": "2026-03-10", "txn_type": "DIVIDEND", "amount_native": "30"})
        pnl = services.holding_pnl(hd)
        # invested = buys - sells = 1000 (dividends are separate); value 1500 -> +500 (+50%)
        assert pnl["show"] and pnl["gain_native"] == 500.0 and pnl["gain_pct"] == 50.0 and pnl["dividends_native"] == 30.0, pnl
        print("PASS: holding_pnl() = current value - net invested (+500, +50%), with dividends reported separately")

        services.add_transaction(hd, {"date": "2026-04-10", "txn_type": "SELL", "quantity": "5", "price_native": "150", "amount_native": "750"})
        hd.live_price_native, hd.current_value_native = 75.0, 750.0  # keep price consistent with quantity (10) so a later no-op edit really is a no-op
        services.recompute_holding_financials(hd)
        pnl2 = services.holding_pnl(hd)
        # invested = 1000 - 750 = 250; value 750 -> +500 (realised + unrealised together)
        assert pnl2["invested_native"] == 250.0 and pnl2["gain_native"] == 500.0 and pnl2["gain_pct"] == 200.0, pnl2
        print("PASS: after a partial sale, gain still equals realised + unrealised (sale proceeds net off the cost)")

        bank, _ = services.create_holding(det_uid, {"asset_type": InternationalAssetType.FOREIGN_BANK_ACCOUNT, "name": "Detail Bank",
                                                    "native_currency": "USD", "current_value_native": "100"})
        assert services.holding_pnl(bank)["show"] is False
        print("PASS: no gain/loss is shown for a foreign bank account")

        # Staleness
        hd.price_updated_at = None
        assert services.get_staleness(hd)["status"] == "never"
        hd.price_updated_at = _dt.utcnow() - timedelta(days=2)
        assert services.get_staleness(hd)["status"] == "fresh"
        hd.price_updated_at = _dt.utcnow() - timedelta(days=6)
        assert services.get_staleness(hd)["status"] == "stale" and services.get_staleness(hd)["age_days"] == 6
        bank.value_updated_at = _dt.utcnow() - timedelta(days=91)
        assert services.get_staleness(bank)["status"] == "stale" and services.get_staleness(bank)["kind"] == "value"
        bank.value_updated_at = _dt.utcnow() - timedelta(days=89)
        assert services.get_staleness(bank)["status"] == "fresh"
        print("PASS: staleness — ticker prices go stale after 5 days, manual values after 90, never-refreshed is flagged")
        db.session.commit()

        # Value history needs snapshots from the daily job
        assert services.get_value_history(hd)["points"] == []
        for i, v in enumerate([1000.0, 1100.0, 1250.0]):
            db.session.add(InternationalValueSnapshot(holding_id=hd.id, date=date(2026, 5, 1) + timedelta(days=i), usd_value=v))
        db.session.commit()
        vh = services.get_value_history(hd)
        assert [p["usd_value"] for p in vh["points"]] == [1000.0, 1100.0, 1250.0]
        assert vh["change_usd"] == 250.0 and vh["change_pct"] == 25.0, vh
        print("PASS: get_value_history() returns snapshots oldest-first with the change since the first point")

        # Timeline: edits, nominees, transactions, archive/restore, refresh
        n_before = len(services.get_timeline(hd, limit=100))
        same = {"name": "Detail Corp", "ticker": "DTL", "country": "United States", "native_currency": "USD",
                "quantity": "10", "avg_cost_native": "100", "notes": ""}
        services.update_holding(hd, same)
        assert len(services.get_timeline(hd, limit=100)) == n_before, "saving without changes must not log anything"
        services.update_holding(hd, {**same, "country": "Canada", "notes": "hello"})
        latest = services.get_timeline(hd, limit=1)[0]
        assert latest.event_type == TimelineEvent.UPDATED and "country: United States → Canada" in latest.description and "notes edited" in latest.description, latest.description
        print("PASS: edits log exactly what changed, and a no-op save logs nothing")

        from werkzeug.datastructures import MultiDict
        services.update_holding(hd, {**same, "country": "Canada", "notes": "hello"},
                                multi_data=MultiDict([("nominee_name[]", "Asha"), ("nominee_relationship[]", "Spouse"), ("nominee_percentage[]", "60")]))
        assert services.get_timeline(hd, limit=1)[0].event_type == TimelineEvent.NOMINEE_UPDATED
        assert "60%" in services.get_timeline(hd, limit=1)[0].description
        print("PASS: changing nominees logs a 'Nominees Updated' entry")

        services.archive_holding(hd)
        services.restore_holding(hd)
        top2 = [e.event_type for e in services.get_timeline(hd, limit=2)]
        assert top2 == [TimelineEvent.RESTORED, TimelineEvent.ARCHIVED], top2
        types_all = {e.event_type for e in services.get_timeline(hd, limit=100)}
        assert {TimelineEvent.TRANSACTION_ADDED, TimelineEvent.CREATED} <= types_all
        print("PASS: transactions, archive and restore all appear on the timeline")

        # Manual refresh logs only when the value actually moved.
        import international_centre.services as _svc
        _svc.fetch_ticker_price = lambda t: 200.0
        services.refresh_holding(hd, log_event=True)
        db.session.commit()
        assert services.get_timeline(hd, limit=1)[0].event_type == TimelineEvent.VALUE_REFRESHED
        n_after = len(services.get_timeline(hd, limit=100))
        services.refresh_holding(hd, log_event=True)  # same price again -> nothing moved
        db.session.commit()
        assert len(services.get_timeline(hd, limit=100)) == n_after
        services.refresh_holding(hd)                  # bulk/scheduled path never logs
        assert len(services.get_timeline(hd, limit=100)) == n_after
        print("PASS: manual refresh logs only when the value moved; scheduled refreshes never flood the timeline")

        # Permanent delete removes its timeline rows (no orphans)
        hid = hd.id
        services.archive_holding(hd)
        services.delete_holding_permanently(hd)
        assert InternationalTimeline.query.filter_by(holding_id=hid).count() == 0
        print("PASS: permanently deleting a holding deletes its timeline (no orphaned audit rows)")

        # editing a manual value resets its freshness clock
        bank.value_updated_at = _dt.utcnow() - timedelta(days=200)
        db.session.commit()
        services.update_holding(bank, {"name": "Detail Bank", "native_currency": "USD", "current_value_native": "150"})
        assert services.get_staleness(bank)["status"] == "fresh"
        print("PASS: changing a manually-valued holding's value resets its freshness")

    # HTTP: detail page shows everything and the education-loan checkbox round-trips
    with app.app_context():
        hh = InternationalHolding.query.filter_by(user_id=db.session.get(User, 1).id if False else u2.id, name='Vanguard S&P 500 ETF').first()
        det_id = hh.id
    detail_html = client.get(f'/international/holdings/{det_id}').get_data(as_text=True)
    assert 'Timeline (' in detail_html and 'Holding Created' in detail_html
    assert 'Gain / Loss' in detail_html and 'Value History (USD)' in detail_html
    print("PASS: holding detail page renders the Gain/Loss card, freshness line, Value History and Timeline via real HTTP")

    r = client.post(f'/international/holdings/{det_id}/refresh', data={'csrf_token': get_csrf(client.get(f'/international/holdings/{det_id}').data)}, follow_redirects=True)
    assert r.status_code == 200

    rem_csrf = get_csrf(client.get('/international/remittances').data)
    r = client.post('/international/remittances/add', data={'csrf_token': rem_csrf, 'date': '2026-09-01', 'amount_inr': '300000',
                    'purpose': RemittancePurpose.EDUCATION, 'education_loan_funded': 'on'}, follow_redirects=True)
    assert r.status_code == 200
    rem_body = client.get('/international/remittances').get_data(as_text=True)
    assert '(loan-funded)' in rem_body and 'icLoanField' in rem_body and 'Remaining before TCS applies' in rem_body or 'Estimated TCS This' in rem_body
    r = client.post('/international/remittances/add', data={'csrf_token': get_csrf(client.get('/international/remittances').data),
                    'date': '2026-09-02', 'amount_inr': '1000', 'purpose': RemittancePurpose.MEDICAL}, follow_redirects=True)
    assert 'Please select a valid purpose' not in r.get_data(as_text=True)
    print("PASS: education-loan checkbox and the Medical purpose work end-to-end via real HTTP")

    # cleanup of the new sections' users
    with app.app_context():
        for email in ("international_list_test@example.com", "international_list_other@example.com",
                      "international_detail_test@example.com", "international_list_http@example.com"):
            _u = User.query.filter_by(email=email).first()
            if _u:
                for _h in InternationalHolding.query.filter_by(user_id=_u.id).all():
                    db.session.delete(_h)
                for _r in RemittanceRecord.query.filter_by(user_id=_u.id).all():
                    db.session.delete(_r)
                db.session.delete(_u)
        db.session.commit()

    # ── 21. Report exports: PDF + CSV (Batch 10.4, Oct 2026) ──
    import io as _io
    import pdfplumber
    from international_centre import export as rexp
    from international_centre.models import VestingTranche

    with app.app_context():
        for _em in ("international_export_a@example.com", "international_export_b@example.com"):
            _u = User.query.filter_by(email=_em).first()
            if _u:
                for _h in InternationalHolding.query.filter_by(user_id=_u.id).all():
                    db.session.delete(_h)
                for _r in RemittanceRecord.query.filter_by(user_id=_u.id).all():
                    db.session.delete(_r)
                db.session.delete(_u)
        db.session.commit()
        ua = User(name="Export A", email="international_export_a@example.com", password="unused")
        ub = User(name="Export B", email="international_export_b@example.com", password="unused")
        db.session.add_all([ua, ub])
        db.session.commit()
        exp_a, exp_b = ua.id, ub.id

        def mkh(uid, **kw):
            h, err = services.create_holding(uid, {"native_currency": "USD", **kw})
            assert err is None, err
            return h

        h_odd = mkh(exp_a, asset_type=InternationalAssetType.US_STOCK, name="AT&T <Inc> \u20b9 \u682a\u5f0f\u4f1a\u793e",
                    ticker="TXX", country="United States", quantity="10", avg_cost_native="100")
        h_formula = mkh(exp_a, asset_type=InternationalAssetType.US_STOCK, name="=SUM(1+1)", ticker="FRM",
                        country="United States", quantity="5", avg_cost_native="10")
        h_rsu = mkh(exp_a, asset_type=InternationalAssetType.RSU_ESPP, name="Employer Corp", ticker="EMP",
                    country="United States", quantity="10", avg_cost_native="50")
        h_secret = mkh(exp_b, asset_type=InternationalAssetType.US_STOCK, name="User B Secret Holding", ticker="SEC",
                       country="United States", quantity="1", avg_cost_native="1")
        for h in (h_odd, h_formula, h_rsu, h_secret):
            h.fx_rate_used = 1.0
            h.usd_value = h.current_value_native
        db.session.commit()

        services.add_transaction(h_odd, {"date": "2025-06-01", "txn_type": "BUY", "quantity": "10", "price_native": "100", "amount_native": "1000"})
        services.add_transaction(h_odd, {"date": "2025-12-01", "txn_type": "SELL", "quantity": "5", "price_native": "150", "amount_native": "750"})
        services.add_transaction(h_odd, {"date": "2025-09-15", "txn_type": "DIVIDEND", "amount_native": "0", "gross_amount_native": "20", "tax_withheld_native": "5"})
        services.add_vesting_tranche(h_rsu, {"plan_type": "RSU", "vest_date": "2025-08-10", "quantity": "10", "fmv_native": "60", "purchase_price_native": "0"})
        services.add_remittance(exp_a, {"date": "2025-05-10", "amount_inr": "400000", "purpose": RemittancePurpose.INVESTMENT_SECURITIES})
        services.add_remittance(exp_a, {"date": "2025-07-10", "amount_inr": "700000", "purpose": RemittancePurpose.EDUCATION, "education_loan_funded": "on"})
        services.add_remittance(exp_b, {"date": "2025-05-11", "amount_inr": "987654", "purpose": RemittancePurpose.OTHER})

        # ── pure helpers ──
        assert rexp.pdf_safe("\u20b9 5 \u2192 6 \u2265 4 \u682a") == "Rs. 5 -> 6 >= 4 ?", rexp.pdf_safe("\u20b9 5 \u2192 6 \u2265 4 \u682a")
        assert rexp.pdf_safe(None) == "" and rexp.pdf_safe("Caf\u00e9") == "Caf\u00e9"
        assert rexp.csv_safe("=SUM(1+1)") == "'=SUM(1+1)" and rexp.csv_safe("+1") == "'+1" and rexp.csv_safe("@x") == "'@x"
        assert rexp.csv_safe("-5 Fund") == "'-5 Fund" and rexp.csv_safe("Normal") == "Normal" and rexp.csv_safe(12.5) == 12.5
        print("PASS: export text helpers neutralise rupee/arrow/non-Latin glyphs and spreadsheet-formula injection")

        assert rexp.clamp_period("2025", "fy") == 2025
        for junk in ("abc", "99999999", "-4", "0", "1800", ""):
            assert rexp.clamp_period(junk, "fy") == rexp.default_period("fy"), junk
        assert rexp.clamp_period(None, "year") == rexp.default_period("year")
        print("PASS: clamp_period() falls back to the default for junk and absurd year values")

        # ── every report: PDF + CSV build, agree with the on-screen service, and stay user-scoped ──
        periods = {"schedule_fa": 2025, "capital_gains": 2025, "dtaa": 2025, "vesting": 2025, "lrs": 2025, "dividends": 2025}
        for key, period in periods.items():
            rep = rexp.build_report(key, exp_a, period)
            assert rep.rows, f"{key} should have rows for the seeded data"
            assert all(len(r) == len(rep.columns) for r in rep.rows), key
            for tr in (rep.totals or []):
                assert len(tr) == len(rep.columns), f"{key} totals row misaligned"
            csv_bytes = rexp.to_csv_bytes(rep)
            assert csv_bytes.startswith(b"\xef\xbb\xbf"), "CSV must start with a UTF-8 BOM so Excel reads it right"
            import re as _re
            assert not _re.search(r"\d\.\d{7,}", csv_bytes.decode("utf-8-sig")), f"{key}: float noise in CSV"
            text = csv_bytes.decode("utf-8-sig")
            assert "User B Secret" not in text and "987654" not in text, f"{key}: another user's data leaked into the export"
            pdf = rexp.to_pdf_bytes(rep, "Export A")
            assert pdf[:5] == b"%PDF-", key
            with pdfplumber.open(_io.BytesIO(pdf)) as doc:
                pdf_text = "\n".join((pg.extract_text() or "") for pg in doc.pages)
            assert "\u20b9" not in pdf_text and "\u20b9".encode() not in pdf, f"{key}: raw rupee glyph in PDF"
            assert "not a filing document" in pdf_text.lower() or "tracking aid" in pdf_text.lower(), key
            assert rep.title.split()[0] in pdf_text, key
        print("PASS: all six reports build, produce a valid PDF + BOM'd CSV, align totals, and never include another user's data")

        # numbers in the CSV are the SAME figures the page's service computes
        fa_page = services.get_schedule_fa_summary(exp_a, 2025)
        fa_csv = list(__import__("csv").reader(_io.StringIO(rexp.to_csv_bytes(rexp.build_report("schedule_fa", exp_a, 2025)).decode("utf-8-sig"))))
        assert len(fa_csv) == 1 + len(fa_page["rows"]) + 1, "header + one row per holding + totals"
        assert fa_csv[0][0] == "Holding" and fa_csv[0][4] == "Opening (USD)"
        by_name = {r[0]: r for r in fa_csv[1:-1]}
        for row in fa_page["rows"]:
            nm = row["holding"].name
            nm_csv = "'" + nm if nm[:1] in "=+-@" else nm
            assert float(by_name[nm_csv][6]) == row["closing_usd"], nm
        lrs_page = services.get_lrs_status(exp_a, anchor_date=date(2025, 4, 1))
        lrs_csv = list(__import__("csv").reader(_io.StringIO(rexp.to_csv_bytes(rexp.build_report("lrs", exp_a, 2025)).decode("utf-8-sig"))))
        assert float(lrs_csv[-1][1]) == lrs_page["total_inr"] and float(lrs_csv[-1][3]) == lrs_page["total_tcs_inr"]
        assert lrs_csv[1][4] == RemittancePurpose.INVESTMENT_SECURITIES and "(loan-funded)" in lrs_csv[2][4]
        assert lrs_csv[1][0] < lrs_csv[2][0], "LRS export is oldest first"
        print("PASS: CSV figures equal the page's own service figures (Schedule FA closing values; LRS INR and TCS totals)")

        # ── CSV injection + markup safety in a real file ──
        fa_text = rexp.to_csv_bytes(rexp.build_report("schedule_fa", exp_a, 2025)).decode("utf-8-sig")
        assert "'=SUM(1+1)" in fa_text and ",=SUM(1+1)" not in fa_text and not fa_text.startswith("=")
        pdf_fa = rexp.to_pdf_bytes(rexp.build_report("schedule_fa", exp_a, 2025), "O'Brien & <Co>")
        with pdfplumber.open(_io.BytesIO(pdf_fa)) as doc:
            fa_pdf_text = "\n".join((pg.extract_text() or "") for pg in doc.pages)
        assert "AT&T <Inc>" in fa_pdf_text and "Rs." in fa_pdf_text and "?" in fa_pdf_text, fa_pdf_text[:400]
        assert "O'Brien & <Co>" in fa_pdf_text
        print("PASS: a holding or user name with & < > rupee or Japanese characters neither breaks the PDF nor leaks glyph boxes; formula-looking names are defused in CSV")

        # an empty period still produces a valid document
        empty = rexp.build_report("capital_gains", exp_b, 2019)
        assert empty.rows == [] and rexp.to_pdf_bytes(empty, "X")[:5] == b"%PDF-" and rexp.to_csv_bytes(empty).decode("utf-8-sig").strip() != ""
        print("PASS: a report with no records still exports (PDF says so, CSV has headers)")

    # ── HTTP ──
    cx = app.test_client()
    pg = cx.get('/login')
    # log in as user A through the real login form
    from werkzeug.security import generate_password_hash  # noqa: F401  (kept for parity with other sections if needed)
    with app.app_context():
        from models import User as _U
        ua2 = _U.query.filter_by(email="international_export_a@example.com").first()
    sess_user_id = exp_a

    def login_as(client_obj, uid):
        with client_obj.session_transaction() as sess:
            sess["_user_id"] = str(uid)
            sess["_fresh"] = True

    anon = app.test_client()
    r = anon.get('/international/reports/lrs/export/csv')
    assert r.status_code in (301, 302) and "login" in r.headers.get("Location", "").lower(), "exports must require login"
    print("PASS: report exports require login")

    login_as(cx, exp_a)
    r = cx.get('/international/reports/schedule_fa/export/pdf?year=2025')
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data[:5] == b"%PDF-", (r.status_code, r.mimetype)
    assert "attachment" in r.headers["Content-Disposition"] and "mywealthlens_schedule_fa_2025.pdf" in r.headers["Content-Disposition"]
    assert "no-store" in r.headers.get("Cache-Control", "")
    r = cx.get('/international/reports/capital_gains/export/csv?fy=2025')
    assert r.status_code == 200 and r.mimetype == "text/csv" and r.data.startswith(b"\xef\xbb\xbf")
    assert "FY2025-26" in r.headers["Content-Disposition"] and "no-store" in r.headers.get("Cache-Control", "")
    body = r.data.decode("utf-8-sig")
    assert "LTCG total" in body and "STCG total" in body and "User B" not in body
    print("PASS: PDF and CSV downloads work over real HTTP with attachment filenames and no-store caching")

    for bad in ('/international/reports/nope/export/pdf', '/international/reports/lrs/export/xls', '/international/reports/lrs/export/'):
        assert cx.get(bad).status_code == 404, bad
    for key in ("schedule_fa", "capital_gains", "dtaa", "vesting", "lrs", "dividends"):
        for junk in ("abc", "99999999", "-1", "%27%3B--"):
            qs = "year" if key == "schedule_fa" else "fy"
            rr = cx.get(f'/international/reports/{key}/export/csv?{qs}={junk}')
            assert rr.status_code == 200, (key, junk, rr.status_code)
    print("PASS: unknown reports/formats 404, and junk period parameters fall back instead of erroring")

    login_as(cx, exp_b)
    other = cx.get('/international/reports/lrs/export/csv?fy=2025').data.decode("utf-8-sig")
    assert "987654" in other and "400000" not in other and "700000" not in other
    print("PASS: a second user's export contains only that user's own remittances")

    # page buttons
    login_as(cx, exp_a)
    for url, key, qs in [('/international/schedule-fa?year=2025', 'schedule_fa', 'year=2025'),
                         ('/international/capital-gains?fy=2025', 'capital_gains', 'fy=2025'),
                         ('/international/dtaa-summary?fy=2025', 'dtaa', 'fy=2025'),
                         ('/international/vesting-perquisite?fy=2025', 'vesting', 'fy=2025'),
                         ('/international/remittances', 'lrs', 'fy=')]:
        html = cx.get(url).get_data(as_text=True)
        assert f'/international/reports/{key}/export/pdf' in html and f'/international/reports/{key}/export/csv' in html, url
    print("PASS: every report page shows PDF and CSV download buttons (LRS also per past year)")

    # ── 22. Dividend income view (Batch 10.5) ──
    with app.app_context():
        div = services.get_dividend_income(exp_a, 2025)
        assert len(div["rows"]) == 1 and div["rows"][0]["name"].startswith("AT&T")
        r0 = div["rows"][0]
        assert r0["gross_native"] == 20.0 and r0["withheld_native"] == 5.0 and r0["net_native"] == 15.0 and r0["payments"] == 1, r0
        assert r0["net_usd"] == 15.0 and r0["net_inr"] == round(15.0 * 83.0, 2), r0
        assert div["total_net_usd"] == 15.0 and div["payments"] == 1 and div["withholding_unknown"] == 0
        sep = [m for m in div["months"] if m["label"] == "Sep 2025"][0]
        assert sep["net_usd"] == 15.0 and sum(m["net_usd"] for m in div["months"]) == 15.0 and len(div["months"]) == 12
        assert div["months"][0]["label"] == "Apr 2025" and div["months"][-1]["label"] == "Mar 2026"
        print("PASS: get_dividend_income() totals, gross/withheld/net, USD/INR bridge and the 12 Apr-Mar month buckets are right")

        # a pre-9.5 style dividend (no gross/withholding) is flagged, not silently assumed tax-free
        services.add_transaction(h_formula, {"date": "2025-10-01", "txn_type": "DIVIDEND", "amount_native": "8"})
        div2 = services.get_dividend_income(exp_a, 2025)
        assert div2["withholding_unknown"] == 1 and div2["total_net_usd"] == 23.0 and len(div2["rows"]) == 2
        frm = [r for r in div2["rows"] if r["name"] == "=SUM(1+1)"][0]
        assert frm["gross_native"] == 8.0 and frm["withheld_native"] == 0.0
        print("PASS: a dividend logged without gross/withholding is counted and flagged as 'withholding unknown'")

        # trailing-12-month yield uses today's date and current value
        h_yield = mkh(exp_a, asset_type=InternationalAssetType.US_ETF, name="Yield ETF", ticker="YLD", country="United States", quantity="10", avg_cost_native="100")
        h_yield.fx_rate_used, h_yield.usd_value, h_yield.current_value_native = 1.0, 1000.0, 1000.0
        recent_day = (today_ist() - timedelta(days=30)).isoformat()
        old_day = (today_ist() - timedelta(days=500)).isoformat()
        services.add_transaction(h_yield, {"date": recent_day, "txn_type": "DIVIDEND", "amount_native": "30"})
        services.add_transaction(h_yield, {"date": old_day, "txn_type": "DIVIDEND", "amount_native": "999"})
        db.session.commit()
        fy_now = rexp.default_period("fy")
        div3 = services.get_dividend_income(exp_a, fy_now)
        y = [r for r in div3["rows"] if r["name"] == "Yield ETF"]
        assert y and y[0]["ttm_yield_pct"] == 3.0, y   # 30 over the last year / 1000 value; the 500-day-old 999 is excluded
        assert div3["portfolio_yield_pct"] is not None
        print("PASS: dividend yield counts only the last 12 months, over current value")

        assert services.get_dividend_income(exp_b, 2025)["rows"] == [], "no cross-user dividends"
        h_nofx = mkh(exp_b, asset_type=InternationalAssetType.US_STOCK, name="No Rate Co", ticker="NRC", country="Germany", native_currency="EUR", quantity="1", avg_cost_native="1")
        h_nofx.fx_rate_used = None
        db.session.commit()
        services.add_transaction(h_nofx, {"date": "2025-09-01", "txn_type": "DIVIDEND", "amount_native": "5"})
        d_nofx = services.get_dividend_income(exp_b, 2025)
        assert d_nofx["any_missing_rate"] is True and d_nofx["rows"][0]["net_usd"] is None and d_nofx["total_net_usd"] == 0.0
        print("PASS: a holding with no exchange rate shows blank USD/INR instead of a guessed figure, and is flagged")

    login_as(cx, exp_a)
    dpage = cx.get('/international/dividends?fy=2025')
    dbody = dpage.get_data(as_text=True)
    assert dpage.status_code == 200 and 'Dividend Income' in dbody and 'dividendMonthsChart' in dbody and 'Net Received (USD)' in dbody
    assert '&lt;Inc&gt;' in dbody and '<Inc>' not in dbody, "holding names must be HTML-escaped on the page"
    empty_div = cx.get('/international/dividends?fy=2019').get_data(as_text=True)
    assert 'No dividends recorded for FY 2019-20' in empty_div and 'dividendMonthsChart' not in empty_div
    assert cx.get('/international/dividends?fy=junk').status_code == 200
    print("PASS: Dividends page renders with chart, escapes names, shows the empty state, and survives junk input")

    # ── 23. INR-perspective returns (Batch 10.5) ──
    with app.app_context():
        real_fetch = services.fetch_fx_rate
        calls = []
        dated = {date(2025, 1, 1): 80.0, date(2025, 7, 1): 85.0}

        def dated_fx(frm, to="INR", on_date=None):
            calls.append((frm, to, on_date))
            if frm.upper() == to.upper():
                return 1.0, on_date or date.today()
            if to == "INR" and frm == "USD":
                return (dated.get(on_date, 85.0) if on_date else 90.0), (on_date or date.today())
            raise services.FxRateError("no rate for " + frm)

        services.fetch_fx_rate = dated_fx
        services.clear_fx_caches()
        try:
            h_inr = mkh(exp_a, asset_type=InternationalAssetType.US_STOCK, name="INR View Co", ticker="INRV", country="United States", quantity="10", avg_cost_native="100")
            services.add_transaction(h_inr, {"date": "2025-01-01", "txn_type": "BUY", "quantity": "10", "price_native": "100", "amount_native": "1000"})
            services.add_transaction(h_inr, {"date": "2025-07-01", "txn_type": "DIVIDEND", "amount_native": "50"})
            h_inr.current_value_native = 1500.0
            services.recompute_holding_financials(h_inr)
            db.session.commit()

            res = services.get_inr_return(h_inr)
            assert res["available"], res
            assert res["invested_inr"] == 80_000.0 and res["current_inr"] == 135_000.0, res           # 1000*80 ; 1500*90 (today's rate)
            assert res["gain_inr"] == 55_000.0 and res["gain_pct"] == 68.75, res
            assert res["dividends_inr"] == 4_250.0, res                                                 # 50 * 85 on the dividend's own date
            assert res["xirr_inr"] is not None and res["xirr_native"] is not None
            assert res["xirr_inr"] > res["xirr_native"], "rupee weakened 80->90, so the INR return must beat the USD return"
            assert res["currency_effect_pts"] == round(res["xirr_inr"] - res["xirr_native"], 2) and res["currency_effect_pts"] > 0
            print("PASS: INR return converts each cash flow at its own date's rate (₹80,000 in, ₹1,35,000 now) and shows a positive currency effect")

            n_calls = len(calls)
            services.get_inr_return(h_inr)
            historical_again = [c for c in calls[n_calls:] if c[2] is not None]
            assert historical_again == [], "historical rates are cached and must not be re-fetched"
            print("PASS: historical exchange rates are cached (second call makes no historical lookups)")

            # a weakening-then-recovering path: INR strengthened => negative currency effect
            dated[date(2025, 1, 1)] = 95.0
            services.clear_fx_caches()
            res2 = services.get_inr_return(h_inr)
            assert res2["currency_effect_pts"] < 0 and res2["gain_inr"] == 135_000.0 - 95_000.0, res2
            dated[date(2025, 1, 1)] = 80.0
            services.clear_fx_caches()
            print("PASS: when the rupee strengthens the currency effect turns negative")

            # unusable cases never guess
            h_nobuy = mkh(exp_a, asset_type=InternationalAssetType.US_STOCK, name="No Buys Co", ticker="NBC", country="United States", quantity="1", avg_cost_native="5")
            r_nb = services.get_inr_return(h_nobuy)
            assert r_nb["available"] is False and r_nb["reason"] == "no_buys" and "buy transactions" in r_nb["message"]
            h_bank2 = mkh(exp_a, asset_type=InternationalAssetType.FOREIGN_BANK_ACCOUNT, name="Bank X", current_value_native="100")
            assert services.get_inr_return(h_bank2)["reason"] == "not_applicable"
            h_eur = mkh(exp_a, asset_type=InternationalAssetType.US_STOCK, name="Euro Co", ticker="EUR1", country="Germany", native_currency="EUR", quantity="1", avg_cost_native="10")
            services.add_transaction(h_eur, {"date": "2025-01-01", "txn_type": "BUY", "quantity": "1", "price_native": "10", "amount_native": "10"})
            r_fx = services.get_inr_return(h_eur)
            assert r_fx["available"] is False and r_fx["reason"] == "fx" and "exchange rates" in r_fx["message"]
            print("PASS: no buys / bank account / unavailable exchange rate each return a clear reason instead of a made-up number")

            port = services.get_portfolio_inr_return(exp_a)
            assert port["available"] and port["included"] >= 1 and "Euro Co" in port["excluded"] and "No Buys Co" in port["excluded"]
            assert port["xirr_inr"] is not None
            none_port = services.get_portfolio_inr_return(exp_b)
            assert none_port["available"] is False
            print("PASS: portfolio INR return includes usable holdings and lists the ones it had to leave out")

            # HTTP JSON endpoints. Run on a fresh thread: this block sits inside
            # `with app.app_context()`, and Flask-Login caches the logged-in user on
            # that shared context, so a "second user" request here would still be
            # answered as the first. A new thread gets no inherited app context.
            def http_json_checks():
                login_as(cx, exp_a)
                jr = cx.get(f'/international/holdings/{h_inr.id}/inr-return.json')
                assert jr.status_code == 200 and jr.mimetype == "application/json" and "no-store" in jr.headers.get("Cache-Control", "")
                jd = jr.get_json()
                assert jd["available"] and jd["gain_inr"] == 55_000.0
                assert cx.get('/international/inr-return.json').get_json()["available"] is True
                login_as(cx, exp_b)
                assert cx.get(f'/international/holdings/{h_inr.id}/inr-return.json').status_code == 404, "another user's holding must 404"
                assert anon.get(f'/international/holdings/{h_inr.id}/inr-return.json').status_code in (301, 302)
            from concurrent.futures import ThreadPoolExecutor as _TPE
            with _TPE(max_workers=1) as _pool:
                _pool.submit(http_json_checks).result()
            print("PASS: INR-return JSON endpoints work, are no-store, 404 for another user's holding, and require login")
        finally:
            services.fetch_fx_rate = real_fetch
            services.clear_fx_caches()

    # ── 24. Reminders: Schedule FA, Form 67/44, stale values, US estate-tax note (Batch 10.6, Oct 2026) ──
    from international_centre.models import InternationalReminderAck
    from datetime import datetime as _dt2

    with app.app_context():
        for _em in ("international_rem_a@example.com", "international_rem_b@example.com"):
            _u = User.query.filter_by(email=_em).first()
            if _u:
                for _h in InternationalHolding.query.filter_by(user_id=_u.id).all():
                    db.session.delete(_h)
                InternationalReminderAck.query.filter_by(user_id=_u.id).delete()
                db.session.delete(_u)
        db.session.commit()
        ura = User(name="Rem A", email="international_rem_a@example.com", password="unused")
        urb = User(name="Rem B", email="international_rem_b@example.com", password="unused")
        db.session.add_all([ura, urb])
        db.session.commit()
        rem_a, rem_b = ura.id, urb.id

        def mkr(uid, **kw):
            h, err = services.create_holding(uid, {"native_currency": "USD", **kw})
            assert err is None, err
            return h

        # No holdings at all -> nothing to remind about.
        assert services.get_reminders(rem_a, today=date(2026, 6, 15)) == []

        h_us = mkr(rem_a, asset_type=InternationalAssetType.US_STOCK, name="US Holding", ticker="USH", country="United States", quantity="1", avg_cost_native="1")
        h_us.created_at = _dt2(2024, 1, 1)
        h_us.fx_rate_used = 1.0
        h_us.usd_value = 10_000.0
        h_us.price_updated_at = _dt2.utcnow()
        db.session.commit()

        def fa(today, year=None):
            year = year or (today.year - 1)
            return [r for r in services.get_reminders(rem_a, today=today) if r["key"] == f"schedule_fa:{year}"][0]

        r = fa(date(2026, 3, 1))
        assert r["key"] == "schedule_fa:2025" and r["level"] == "info" and "31 Jul 2026" in r["message"] and "152 days" in r["message"], r["message"]
        r = fa(date(2026, 7, 10))
        assert r["level"] == "warning" and "21 days left" in r["message"], r["message"]
        r = fa(date(2026, 9, 1))
        assert r["level"] == "warning" and "belated" in r["message"] and "31 Dec 2026" in r["message"]
        # After 1 Jan the previous year does NOT vanish: it escalates until marked done.
        r = fa(date(2027, 1, 15), year=2025)
        assert r["level"] == "danger" and "have passed" in r["message"], r
        assert fa(date(2027, 1, 15), year=2026)["level"] == "info", "the new latest year starts as a normal reminder"
        # a user whose first holding was added AFTER the calendar year (and has no earlier-dated activity) has nothing to disclose for it
        h_us.created_at = _dt2(2026, 2, 1)
        db.session.commit()
        assert [x for x in services.get_reminders(rem_a, today=date(2026, 6, 1)) if x["kind"] == "schedule_fa"] == []
        # ...but a holding added to the app late whose TRANSACTIONS are dated earlier still counts
        services.add_transaction(h_us, {"date": "2024-03-01", "txn_type": "BUY", "quantity": "1", "price_native": "1", "amount_native": "1"})
        late = [x["key"] for x in services.get_reminders(rem_a, today=date(2026, 6, 1)) if x["kind"] == "schedule_fa"]
        assert late == ["schedule_fa:2025"] or set(late) == {"schedule_fa:2025", "schedule_fa:2024"}, late
        h_us.created_at = _dt2(2024, 1, 1)
        db.session.commit()
        print("PASS: Schedule FA reminder follows the usual ITR dates (info -> warning -> belated -> danger), keeps the previous year until marked done, and counts holdings recorded late by their transaction dates")

        # Form 67 / Form 44 — only when foreign tax was actually withheld
        assert [x for x in services.get_reminders(rem_a, today=date(2026, 10, 2)) if x["kind"] == "form67"] == []
        services.add_transaction(h_us, {"date": "2025-09-15", "txn_type": "DIVIDEND", "amount_native": "0", "gross_amount_native": "100", "tax_withheld_native": "25"})
        f67 = [x for x in services.get_reminders(rem_a, today=date(2026, 10, 2)) if x["kind"] == "form67"]
        assert len(f67) == 1 and f67[0]["key"] == "form67:2025" and f67[0]["title"].startswith("Form 67")
        assert "31 Mar 2027" in f67[0]["message"] and "BEFORE your return" in f67[0]["message"] and f67[0]["level"] == "warning"
        services.add_transaction(h_us, {"date": "2026-09-15", "txn_type": "DIVIDEND", "amount_native": "0", "gross_amount_native": "100", "tax_withheld_native": "25"})
        f44 = [x for x in services.get_reminders(rem_a, today=date(2027, 8, 1)) if x["kind"] == "form67"]
        assert any(x["key"] == "form67:2026" and x["title"].startswith("Form 44") for x in f44), [x["title"] for x in f44]
        old_gone = [x for x in services.get_reminders(rem_a, today=date(2028, 6, 1)) if x["kind"] == "form67"]
        assert all(x["key"] != "form67:2025" for x in old_gone), "a year whose window is long past stops nagging"
        print("PASS: Form 67 (Form 44 from FY 2026-27) reminder appears only when tax was withheld, names the right form, and old years stop nagging")

        # Stale manual values
        bank = mkr(rem_a, asset_type=InternationalAssetType.FOREIGN_BANK_ACCOUNT, name="Old Bank", current_value_native="500")
        bank.value_updated_at = _dt2.utcnow() - timedelta(days=200)
        fresh_bank = mkr(rem_a, asset_type=InternationalAssetType.FOREIGN_BANK_ACCOUNT, name="Fresh Bank", current_value_native="500")
        db.session.commit()
        stale_items = [x for x in services.get_reminders(rem_a) if x["kind"] == "stale_value"]
        assert [x["title"] for x in stale_items] == ["Update the value of Old Bank"] and stale_items[0]["level"] == "warning"
        assert "200 days ago" in stale_items[0]["message"] and stale_items[0]["ack_key"] is None
        for i in range(6):   # many stale holdings collapse to 5 + a summary line
            hb = mkr(rem_a, asset_type=InternationalAssetType.FOREIGN_BANK_ACCOUNT, name=f"Bulk Bank {i}", current_value_native="1")
            hb.value_updated_at = _dt2.utcnow() - timedelta(days=100 + i)
        db.session.commit()
        stale_items = [x for x in services.get_reminders(rem_a) if x["kind"] == "stale_value"]
        assert len(stale_items) == 6 and stale_items[-1]["key"] == "stale:more" and "2 more" in stale_items[-1]["title"], [x["title"] for x in stale_items]
        assert not any(x["kind"] == "stale_value" for x in services.get_reminders(rem_b))
        print("PASS: stale manually-valued holdings are listed oldest-first, capped at 5 with a '+N more' line, and never shown to another user")

        # US estate-tax awareness note
        exp = services.get_us_situs_exposure(rem_a)
        assert exp["total_usd"] == 10_000.0 and len(exp["holdings"]) == 1 and exp["exemption_usd"] == 60_000
        assert not [x for x in services.get_reminders(rem_a) if x["kind"] == "estate"], "well under the threshold: no note"
        h_us.usd_value = 50_000.0
        db.session.commit()
        est = [x for x in services.get_reminders(rem_a) if x["kind"] == "estate"][0]
        assert est["key"] == "estate:near" and est["level"] == "info" and "approaching" in est["message"] and "$60,000" in est["message"]
        h_us.usd_value = 75_000.0
        db.session.commit()
        est = [x for x in services.get_reminders(rem_a) if x["kind"] == "estate"][0]
        assert est["key"] == "estate:over" and est["level"] == "warning" and "above" in est["message"]
        assert "not tax advice" in est["detail"] and "no India-US estate-tax treaty" in est["detail"]
        print("PASS: US estate-tax note appears at 80% of $60,000 (info) and above it (warning), with the not-advice wording")

        # what counts as US-situs
        h_uk = mkr(rem_a, asset_type=InternationalAssetType.US_ETF, name="UK Fund", ticker="UKF", country="United Kingdom", quantity="1", avg_cost_native="1")
        h_uk.usd_value = 500_000.0
        bank_us = mkr(rem_a, asset_type=InternationalAssetType.FOREIGN_BANK_ACCOUNT, name="US Bank", country="United States", current_value_native="900000")
        bank_us.usd_value = 900_000.0
        h_nocountry = mkr(rem_a, asset_type=InternationalAssetType.US_STOCK, name="No Country Co", ticker="NCC", quantity="1", avg_cost_native="1")
        h_nocountry.usd_value = 1_000.0
        db.session.commit()
        exp2 = services.get_us_situs_exposure(rem_a)
        names = {h.name for h in exp2["holdings"]}
        assert names == {"US Holding", "No Country Co"}, names
        assert [h.name for h in exp2["assumed"]] == ["No Country Co"] and exp2["total_usd"] == 76_000.0
        est2 = [x for x in services.get_reminders(rem_a) if x["kind"] == "estate"][0]
        assert "assumed to be US-listed" in est2["message"]
        print("PASS: only US-situs holdings count (UK-listed fund and US bank deposits excluded; no-country USD stock assumed US and flagged)")

        # acknowledgements
        assert services.valid_ack_key("schedule_fa:2025") and services.valid_ack_key("form67:2026") and services.valid_ack_key("estate:over")
        for bad_key in ("", None, "estate:", "schedule_fa:25", "stale:5", "x; DROP TABLE", "estate:over\n", "form67:2025 "):
            assert services.valid_ack_key(bad_key) is False, repr(bad_key)
        assert services.acknowledge_reminder(rem_a, "estate:nonsense") is False
        assert services.acknowledge_reminder(rem_a, "schedule_fa:2025") is True
        assert services.acknowledge_reminder(rem_a, "schedule_fa:2025") is True    # idempotent
        assert InternationalReminderAck.query.filter_by(user_id=rem_a, reminder_key="schedule_fa:2025").count() == 1
        assert fa(date(2026, 3, 1))["done"] is True
        assert not any(x["kind"] == "schedule_fa" for x in services.get_reminders(rem_b, today=date(2026, 3, 1)))
        services.unacknowledge_reminder(rem_a, "schedule_fa:2025")
        assert fa(date(2026, 3, 1))["done"] is False
        services.acknowledge_reminder(rem_a, "estate:over")
        ordered = services.get_reminders(rem_a)
        assert ordered[-1]["kind"] == "estate" and ordered[-1]["done"] is True, "done items sort to the bottom"
        print("PASS: reminders can be marked done / undone (idempotently, per user) and only whitelisted keys are accepted")

    # HTTP (outside the shared app context)
    cr = app.test_client()
    with app.app_context():
        rid = User.query.filter_by(email="international_rem_a@example.com").first().id
    login_as(cr, rid)
    rpage = cr.get('/international/reminders')
    rbody = rpage.get_data(as_text=True)
    assert rpage.status_code == 200 and 'usual statutory' in rbody and 'Update the value of Old Bank' in rbody
    csrf_r = get_csrf(rpage.data)
    r = cr.post('/international/reminders/done', data={'csrf_token': csrf_r, 'key': 'estate:near'}, follow_redirects=True)
    assert r.status_code == 200
    assert cr.post('/international/reminders/done', data={'csrf_token': csrf_r, 'key': 'garbage'}).status_code == 400
    assert cr.post('/international/reminders/undo', data={'csrf_token': csrf_r, 'key': 'garbage'}).status_code == 400
    assert cr.post('/international/reminders/done', data={'key': 'estate:near'}).status_code in (400, 403), "CSRF token must be required"
    r = cr.post('/international/reminders/undo', data={'csrf_token': csrf_r, 'key': 'estate:over'}, follow_redirects=True)
    assert r.status_code == 200 and 'US estate tax' in r.get_data(as_text=True)
    dash_r = cr.get('/international/').get_data(as_text=True)
    assert 'Reminders Centre' in dash_r and 'Open reminders' in dash_r
    assert anon.get('/international/reminders').status_code in (301, 302)
    print("PASS: Reminders page and dashboard widget work over real HTTP; mark-done/undo need CSRF, reject junk keys, and require login")

    # ── 25. Insurance-style UI parity (Batch 10.7) ──
    cu = app.test_client()
    login_as(cu, rid)
    dash = cu.get('/international/').get_data(as_text=True)
    assert 'iu-stat-grid' in dash and 'Asset Categories' in dash and 'iu-cat' in dash and 'Top Holdings' in dash
    assert 'asset_type=Foreign%20Bank%20Account' in dash or 'asset_type=Foreign+Bank+Account' in dash, "empty categories offer + Add"
    assert 'Recent Activity' in dash and 'Documents Stored' in dash and 'Reminders' in dash
    assert 'prefers-reduced-motion' in dash and 'aria-label="Portfolio summary"' in dash
    print("PASS: dashboard has icon stat cards, asset-category cards with '+ Add' for empty types, Recent Activity, document count, reduced-motion and aria labels")

    add_pg = cu.get('/international/holdings/add?asset_type=Foreign%20Bank%20Account').get_data(as_text=True)
    assert '<option value="Foreign Bank Account" selected>' in add_pg and 'fm-step' in add_pg and 'fm-step-badge' in add_pg
    junk_pg = cu.get('/international/holdings/add?asset_type=%3Cscript%3E').get_data(as_text=True)
    assert '<script>' not in junk_pg.split('<option value="">Select type')[1][:600] and 'selected>' not in junk_pg.split('id="assetTypeSelect"')[1].split('</select>')[0]
    print("PASS: category cards pre-select the asset type on the add form (junk values ignored) and the form has numbered step sections")

    lst = cu.get('/international/holdings').get_data(as_text=True)
    assert 'hlCards' in lst and 'hlBtnCards' in lst and 'localStorage' in lst and 'hl-card' in lst
    det_id_r = None
    with app.app_context():
        det_id_r = InternationalHolding.query.filter_by(user_id=rid, name="US Holding").first().id
    det = cu.get(f'/international/holdings/{det_id_r}').get_data(as_text=True)
    assert 'hd-icon' in det and 'hd-badges' in det and 'icInrCard' in det and 'Return in INR' in det
    arch_id = None
    with app.app_context():
        hh_arch = InternationalHolding.query.filter_by(user_id=rid, name="Fresh Bank").first()
        services.archive_holding(hh_arch)
    arch = cu.get('/international/archived').get_data(as_text=True)
    assert 'ar-card' in arch and 'Fresh Bank' in arch and 'Restore' in arch and 'Delete' in arch
    with app.app_context():
        services.restore_holding(InternationalHolding.query.filter_by(user_id=rid, name="Fresh Bank").first())
    print("PASS: Holdings page has a remembered cards/table toggle; detail page has the icon header and INR card; archived page uses dashed cards with Restore/Delete")

    # every page extends the shared stylesheet components without breaking (no unrendered template syntax)
    for url in ('/international/', '/international/holdings', '/international/archived', '/international/reminders',
                '/international/dividends', '/international/schedule-fa', '/international/capital-gains',
                '/international/dtaa-summary', '/international/vesting-perquisite', '/international/remittances',
                '/international/documents', f'/international/holdings/{det_id_r}', '/international/holdings/add'):
        rr = cu.get(url)
        assert rr.status_code == 200, (url, rr.status_code)
        body_u = rr.get_data(as_text=True)
        assert '{{' not in body_u and '{%' not in body_u, f"unrendered template syntax on {url}"
    print("PASS: all 13 International pages render (HTTP 200) with no unrendered template syntax")

    # ── 26. Export PDFs never contain a raw rupee glyph under the default INR currency ──
    with app.app_context():
        for _key in ("lrs", "dtaa", "vesting", "dividends"):
            _rep = rexp.build_report(_key, exp_a, 2025)
            _pdf = rexp.to_pdf_bytes(_rep, "Glyph Check")
            assert "\u20b9".encode("utf-8") not in _pdf
            assert "\u20b9" not in "".join((pg.extract_text() or "") for pg in pdfplumber.open(_io.BytesIO(_pdf)).pages)
    print("PASS: International report PDFs contain no raw rupee glyph (same rule as the Sep 2026 glyph audit)")

    with app.app_context():
        for _em in ("international_rem_a@example.com", "international_rem_b@example.com",
                    "international_export_a@example.com", "international_export_b@example.com"):
            _u = User.query.filter_by(email=_em).first()
            if _u:
                for _h in InternationalHolding.query.filter_by(user_id=_u.id).all():
                    db.session.delete(_h)
                for _r in RemittanceRecord.query.filter_by(user_id=_u.id).all():
                    db.session.delete(_r)
                InternationalReminderAck.query.filter_by(user_id=_u.id).delete()
                db.session.delete(_u)
        db.session.commit()

    # ── Cleanup ──
    with app.app_context():
        from models import db as _db
        for email in (TEST_EMAIL, "international_http_test@example.com"):
            u = User.query.filter_by(email=email).first()
            if u:
                for h in InternationalHolding.query.filter_by(user_id=u.id).all():
                    for d in h.documents:
                        intl_utils.delete_document_file(d.file_path)
                InternationalHolding.query.filter_by(user_id=u.id).delete()
                RemittanceRecord.query.filter_by(user_id=u.id).delete()
                _db.session.delete(u)
        _db.session.commit()

    print("\nALL INTERNATIONAL INVESTING CENTRE TESTS PASSED")
    return True


if __name__ == "__main__":
    run_in_scratch(main)
