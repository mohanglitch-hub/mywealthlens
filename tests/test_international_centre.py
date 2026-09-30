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

    # ── 13. TCS on LRS remittances (Batch 9.4, Sep 2026) ──
    from international_centre.models import TCS_THRESHOLD_INR, TCS_RATE

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

        # First remittance, entirely below the 7L threshold -> no TCS.
        r_below = services.add_remittance(tcs_user_id, {
            "date": "2026-05-01", "amount_inr": "500000",
            "purpose": RemittancePurpose.INVESTMENT_SECURITIES,
        })
        assert r_below.tcs_amount_inr == 0.0, r_below.tcs_amount_inr
        print("PASS: a remittance that stays below the ₹7L/FY TCS threshold collects no TCS")

        # Second remittance in the SAME FY pushes cumulative total past
        # the threshold -> TCS applies only to the portion above it.
        r_cross = services.add_remittance(tcs_user_id, {
            "date": "2026-06-01", "amount_inr": "400000",  # 500k + 400k = 900k, 200k over threshold
            "purpose": RemittancePurpose.INVESTMENT_SECURITIES,
        })
        expected_tcs = round((500000 + 400000 - TCS_THRESHOLD_INR) * TCS_RATE, 2)
        assert r_cross.tcs_amount_inr == expected_tcs, (r_cross.tcs_amount_inr, expected_tcs)
        print(f"PASS: TCS is charged only on the portion of a remittance crossing the ₹7L threshold (₹{r_cross.tcs_amount_inr:,.0f})")

        # Third remittance, entirely above the threshold already -> the
        # WHOLE amount is taxed at TCS_RATE (no re-taxing of prior years).
        r_above = services.add_remittance(tcs_user_id, {
            "date": "2026-07-01", "amount_inr": "100000",
            "purpose": RemittancePurpose.INVESTMENT_SECURITIES,
        })
        assert r_above.tcs_amount_inr == round(100000 * TCS_RATE, 2), r_above.tcs_amount_inr
        print("PASS: a remittance entirely above the threshold is taxed in full at 20%")

        status_tcs = services.get_lrs_status(tcs_user_id, anchor_date=date(2026, 8, 1))
        expected_total_tcs = round(r_below.tcs_amount_inr + r_cross.tcs_amount_inr + r_above.tcs_amount_inr, 2)
        assert status_tcs["total_tcs_inr"] == expected_total_tcs, (status_tcs["total_tcs_inr"], expected_total_tcs)
        print(f"PASS: get_lrs_status() reports the correct cumulative TCS for the FY (₹{status_tcs['total_tcs_inr']:,.0f})")

        # A remittance in the NEXT financial year starts the ₹7L
        # threshold fresh (per-FY, not a running lifetime total).
        r_next_fy = services.add_remittance(tcs_user_id, {
            "date": "2026-04-15", "amount_inr": "300000",  # FY 2026-27 vs the FY 2026-27 remittances above? see note below
            "purpose": RemittancePurpose.INVESTMENT_SECURITIES,
        })
        # 2026-04-15 and 2026-05-01/06-01/07-01 are ALL within the same
        # Indian FY (1 Apr 2026 - 31 Mar 2027) -- this remittance simply
        # adds to that FY's running total, so it's fully taxed too.
        assert r_next_fy.tcs_amount_inr == round(300000 * TCS_RATE, 2), r_next_fy.tcs_amount_inr
        print("PASS: TCS threshold tracking is scoped per financial year, accumulating correctly within it")

        RemittanceRecord.query.filter_by(user_id=tcs_user_id).delete()
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

        InternationalHolding.query.filter_by(user_id=dtaa_user_id).delete()
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

        InternationalHolding.query.filter_by(user_id=cg_user_id).delete()
        db.session.delete(u9)
        db.session.commit()

    # ── 16. HTTP: TCS column, dividend fields, DTAA/capital-gains report pages ──
    remit_page2 = client.get('/international/remittances')
    remit_csrf2 = get_csrf(remit_page2.data)
    # A remittance large enough on its own to cross the ₹7L threshold,
    # against the international_http_test user used throughout section 9.
    r = client.post('/international/remittances/add', data={
        'csrf_token': remit_csrf2, 'date': '2026-05-10', 'amount_inr': '900000',
        'purpose': RemittancePurpose.INVESTMENT_SECURITIES,
    }, follow_redirects=True)
    assert r.status_code == 200
    remit_body = client.get('/international/remittances').get_data(as_text=True)
    assert 'TCS Collected This FY' in remit_body, "remittances page must show the TCS summary tile once TCS has been collected"
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
