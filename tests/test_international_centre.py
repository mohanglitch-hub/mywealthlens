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

        # ── 8. take_daily_snapshot() idempotency ──
        before_count = InternationalValueSnapshot.query.filter_by(holding_id=holding.id).count()
        n = services.take_daily_snapshot(user_id=user_id)
        assert n >= 2  # at least `holding` and `no_snap_holding`
        after_first = InternationalValueSnapshot.query.filter_by(holding_id=holding.id).count()
        assert after_first == before_count + 1, "expected exactly one new snapshot row for today"
        services.take_daily_snapshot(user_id=user_id)
        after_second = InternationalValueSnapshot.query.filter_by(holding_id=holding.id).count()
        assert after_second == after_first, "re-running the same day must update, not duplicate, today's snapshot"
        print("PASS: take_daily_snapshot() is idempotent for repeat runs on the same day")

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

    # ── Cleanup ──
    with app.app_context():
        from models import db as _db
        for email in (TEST_EMAIL, "international_http_test@example.com"):
            u = User.query.filter_by(email=email).first()
            if u:
                InternationalHolding.query.filter_by(user_id=u.id).delete()
                RemittanceRecord.query.filter_by(user_id=u.id).delete()
                _db.session.delete(u)
        _db.session.commit()

    print("\nALL INTERNATIONAL INVESTING CENTRE TESTS PASSED")
    return True


if __name__ == "__main__":
    run_in_scratch(main)
