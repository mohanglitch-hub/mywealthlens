"""
Batch 4 — mutual fund NAV fallback source (Sep 2026).

AMFI's bulk NAVAll.txt was the only NAV source for mutual funds --
if amfiindia.com is ever down, slow, or changes its file format, every
mutual fund in a refresh run used to fail outright. price_refresh.py
now falls back to mfapi.in (a free, per-scheme mirror of the same AMFI
data) when the bulk file can't be fetched or parsed.

This test never makes a real network call (this environment's outbound
access doesn't reach api.mfapi.in or amfiindia.com anyway, and a test
that depends on either site being up on any given day would be flaky
regardless) -- it monkeypatches price_refresh.fetch_amfi_nav_map() and
price_refresh.fetch_mfapi_nav() directly and checks refresh_all_prices()
routes around a simulated AMFI outage correctly:

  1. AMFI works normally -> nav_source == "amfi", fallback never touched.
  2. AMFI fails, mfapi.in fallback resolves every scheme -> NAVs still
     get updated, nav_source == "mfapi_fallback", amfi_error is still
     recorded (so the CLI/UI can mention AMFI was down even though the
     run still succeeded).
  3. AMFI fails AND the fallback resolves nothing (simulating "no
     internet at all") -> every fund with an amfi_code is counted as
     failed (not silently dropped, which was the old, unreported gap
     this also fixes), every fund without one is still "no_code", and
     nothing raises.

Run from the project root:
    py tests\\test_price_refresh_fallback.py       (Windows)
    python3 tests/test_price_refresh_fallback.py   (Mac/Linux)

Runs against a disposable scratch copy of the project (see
_scratch_env.py) -- never your real instance/mywealthlens.db. Exits
with status 0 if every check passes, 1 otherwise.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_env import run_in_scratch

TEST_EMAIL = "price_fallback_test@example.com"
PASSWORD = "TestPass123!"


def main():
    import price_refresh
    from app import app, db
    from models import User, MutualFund

    with app.app_context():
        existing = User.query.filter_by(email=TEST_EMAIL).first()
        if existing:
            MutualFund.query.filter_by(user_id=existing.id).delete()
            db.session.delete(existing)
            db.session.commit()

        # No HTTP requests in this test (refresh_all_prices is called
        # directly), so the password value itself is never checked --
        # just needs to satisfy the column's NOT NULL constraint.
        u = User(name="Price Fallback Test", email=TEST_EMAIL, password="unused-in-this-test")
        db.session.add(u)
        db.session.commit()
        user_id = u.id

        mf1 = MutualFund(user_id=user_id, scheme="Fund One", amfi_code="100001",
                          units=100, nav=10.0, value=1000, invested=900)
        mf2 = MutualFund(user_id=user_id, scheme="Fund Two", amfi_code="100002",
                          units=50, nav=20.0, value=1000, invested=950)
        mf3 = MutualFund(user_id=user_id, scheme="Fund No Code", amfi_code=None,
                          units=10, nav=5.0, value=50, invested=50)
        db.session.add_all([mf1, mf2, mf3])
        db.session.commit()
        mf1_id, mf2_id = mf1.id, mf2.id

    # ── 1. AMFI works normally -- fallback never invoked ──
    with app.app_context():
        real_fallback_calls = []
        orig_fallback = price_refresh.fetch_nav_map_via_fallback
        price_refresh.fetch_nav_map_via_fallback = lambda codes: real_fallback_calls.append(codes) or {}
        price_refresh.fetch_amfi_nav_map = lambda: {"100001": 11.5, "100002": 21.0}
        try:
            summary = price_refresh.refresh_all_prices(db, user_id=user_id)
        finally:
            price_refresh.fetch_nav_map_via_fallback = orig_fallback

        assert summary["nav_source"] == "amfi", summary
        assert summary["amfi_error"] is None, summary
        assert summary["fallback_attempted"] is False
        assert real_fallback_calls == [], "fallback must not be called when AMFI succeeds"
        assert summary["mfs_updated"] == 2, summary
        assert summary["mfs_no_code"] == 1, summary
        mf1 = MutualFund.query.get(mf1_id)
        assert mf1.nav == 11.5, mf1.nav
    print("PASS: AMFI success path never touches the fallback")

    # ── 2. AMFI fails, fallback resolves every scheme ──
    with app.app_context():
        def failing_amfi():
            raise price_refresh.PriceRefreshError("simulated AMFI outage")

        def working_fallback(codes):
            assert codes == {"100001", "100002"}, codes
            return {"100001": 12.25, "100002": 22.5}

        price_refresh.fetch_amfi_nav_map = failing_amfi
        price_refresh.fetch_nav_map_via_fallback = working_fallback
        summary = price_refresh.refresh_all_prices(db, user_id=user_id)

        assert summary["amfi_error"] == "simulated AMFI outage", summary
        assert summary["fallback_attempted"] is True
        assert summary["nav_source"] == "mfapi_fallback", summary
        assert summary["mfs_updated"] == 2, summary
        assert summary["mfs_no_code"] == 1, summary
        assert summary["mfs_failed"] == 0, summary
        mf1 = MutualFund.query.get(mf1_id)
        mf2 = MutualFund.query.get(mf2_id)
        assert mf1.nav == 12.25, mf1.nav
        assert mf2.nav == 22.5, mf2.nav
    print("PASS: AMFI failure correctly falls back to mfapi.in and still updates NAVs")

    # ── 3. Both sources fail (simulated total outage) ──
    with app.app_context():
        def failing_amfi():
            raise price_refresh.PriceRefreshError("simulated total outage")

        def failing_fallback(codes):
            return {}

        price_refresh.fetch_amfi_nav_map = failing_amfi
        price_refresh.fetch_nav_map_via_fallback = failing_fallback

        mf1_before = MutualFund.query.get(mf1_id).nav
        summary = price_refresh.refresh_all_prices(db, user_id=user_id)

        assert summary["amfi_error"] == "simulated total outage", summary
        assert summary["fallback_attempted"] is True
        assert summary["nav_source"] is None, summary
        # Every fund WITH a code is now correctly reported as failed
        # (previously these were silently dropped from every counter).
        assert summary["mfs_failed"] == 2, summary
        assert summary["mfs_no_code"] == 1, summary
        assert summary["mfs_updated"] == 0, summary
        # Existing NAVs are left untouched, not zeroed out, on total failure.
        mf1_after = MutualFund.query.get(mf1_id).nav
        assert mf1_after == mf1_before, (mf1_before, mf1_after)
    print("PASS: total NAV-source outage is fully and correctly reported, nothing raises, no data is clobbered")

    # ── Cleanup ──
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        if u:
            MutualFund.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()

    print("\nALL PRICE-REFRESH FALLBACK TESTS PASSED")
    return True


if __name__ == "__main__":
    run_in_scratch(main)
