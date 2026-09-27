"""
Batch 6 — Goals module testing across shapes (Sep 2026).

The Goals module had only ever been exercised against one goal shape
(a simple "Marriage" goal: a manually-typed current_savings figure,
flat SIP, no linked holdings, no retirement fields). This test drives
the actual Flask routes (not the bare functions) for three other
shapes, since the dashboard route (goals()), the PDF export, and the
templates are what actually ship:

  1. A RETIREMENT goal — is_retirement_goal=True, with a linked
     retirement_scheme holding, testing glide_path_curve() +
     drawdown_curve() end to end, including the "corpus depletes
     before life expectancy" warning path.
  2. A SHORT-HORIZON goal — target_year == the current year (0 years
     left), the edge case for calculate_goal()'s months=max(years*12,1)
     and glide_path_curve()'s years==0 early return.
  3. A MULTI-HOLDING goal — linked to a mutual fund, a stock, AND a
     fixed deposit (wealth_asset) simultaneously, at partial
     allocation percentages, exercising _goal_linked_value()'s summing
     across holding types and goal_glide.rebalance_suggestion()'s
     equity/debt split across mixed asset_class links.

For each shape: load /goals (the dashboard) and /export/pdf, assert
200 and no server error, and check specific expected content.

Run from the project root:
    py tests\\test_goal_shapes.py       (Windows)
    python3 tests/test_goal_shapes.py   (Mac/Linux)

Runs against a disposable scratch copy of the project (see
_scratch_env.py) -- never your real instance/mywealthlens.db. Exits
with status 0 if every check passes, 1 otherwise.
"""
import re
import sys
import os
import io
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _scratch_env import run_in_scratch

TEST_EMAIL = "goal_shapes_test@example.com"
PASSWORD = "TestPass123!"


def get_csrf(page_bytes):
    m = re.search(rb'name="csrf_token" value="([^"]+)"', page_bytes)
    return m.group(1).decode() if m else None


def _is_greenish(rgb):
    r, g, b = rgb
    return g > r and g > b


def _is_reddish(rgb):
    r, g, b = rgb
    return r > g and r > b


def main():
    import pdfplumber
    import goal_glide
    from app import app, db, _goal_linked_value
    from models import User, Goal, GoalHoldingLink, MutualFund, Stock
    from wealth.models import WealthAsset, WealthAssetCategory
    from retirement_centre.models import RetirementScheme

    # ── Setup: clean slate, signup ──
    with app.app_context():
        existing = User.query.filter_by(email=TEST_EMAIL).first()
        if existing:
            GoalHoldingLink.query.filter(
                GoalHoldingLink.goal_id.in_(
                    db.session.query(Goal.id).filter_by(user_id=existing.id))).delete(synchronize_session=False)
            Goal.query.filter_by(user_id=existing.id).delete()
            MutualFund.query.filter_by(user_id=existing.id).delete()
            Stock.query.filter_by(user_id=existing.id).delete()
            WealthAsset.query.filter_by(user_id=existing.id).delete()
            RetirementScheme.query.filter_by(user_id=existing.id).delete()
            db.session.delete(existing)
            db.session.commit()

    client = app.test_client()
    page = client.get('/signup')
    r = client.post('/signup', data={
        'csrf_token': get_csrf(page.data),
        'name': 'Goal Shapes Test', 'email': TEST_EMAIL,
        'password': PASSWORD, 'confirm_password': PASSWORD,
    }, follow_redirects=True)
    assert r.status_code == 200

    with app.app_context():
        user = User.query.filter_by(email=TEST_EMAIL).first()
        user_id = user.id

        # Holdings to link across the multi-holding + retirement goals
        mf = MutualFund(user_id=user_id, scheme="Test Equity Fund", units=1000, nav=50, value=50000, invested=40000)
        stock = Stock(user_id=user_id, isin="INE000A00001", name="Test Corp Ltd", quantity=100, value=25000, invested=20000)
        fd = WealthAsset(user_id=user_id, category=WealthAssetCategory.BANK_DEPOSITS,
                          name="Test FD", asset_type="Fixed Deposit", current_value=300000, currency="INR")
        nps = RetirementScheme(user_id=user_id, scheme_type='NPS', institution='Test PFM', current_balance=800000)
        db.session.add_all([mf, stock, fd, nps])
        db.session.commit()
        mf_id, stock_id, fd_id, nps_id = mf.id, stock.id, fd.id, nps.id
    print(f"PASS: signed up test user (id={user_id}) and seeded 4 holdings")

    def add_goal(**kwargs):
        page = client.get('/goals')
        csrf = get_csrf(page.data)
        data = {'csrf_token': csrf, **kwargs}
        r = client.post('/goals/add', data=data, follow_redirects=True)
        assert r.status_code == 200, r.status_code
        return r

    def link_holding(goal_id, holding_key, allocation_pct, asset_class):
        page = client.get('/goals')
        csrf = get_csrf(page.data)
        r = client.post(f'/goals/{goal_id}/link', data={
            'csrf_token': csrf, 'holding': holding_key,
            'allocation_pct': str(allocation_pct), 'asset_class': asset_class,
        }, follow_redirects=True)
        assert r.status_code == 200, r.status_code
        return r

    current_year = date.today().year

    # ── Shape 1: Retirement goal ──
    add_goal(name='Retirement Corpus', emoji='🏖️', target_amt='30000000', target_year=str(current_year + 25),
              current_savings='200000', monthly_sip='25000', annual_return='11',
              is_retirement_goal='on', retirement_age='60', life_expectancy='85',
              monthly_expense_today='80000', expense_inflation_pct='6', post_retirement_return_pct='7')
    with app.app_context():
        retirement_goal = Goal.query.filter_by(user_id=user_id, name='Retirement Corpus').first()
        assert retirement_goal is not None
        assert retirement_goal.is_retirement_goal is True
        rg_id = retirement_goal.id
    link_holding(rg_id, f'retirement_scheme:{nps_id}', 100, 'debt')

    r = client.get('/goals')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Retirement Corpus' in body
    assert 'Post-Retirement Plan' in body
    print("PASS: retirement goal renders on dashboard with drawdown section")

    # ── Also test the DEPLETION warning path directly (short life-expectancy
    # window relative to expense) using a second, harsher retirement goal ──
    add_goal(name='Underfunded Retirement', emoji='😬', target_amt='5000000', target_year=str(current_year + 5),
              current_savings='100000', monthly_sip='2000', annual_return='8',
              is_retirement_goal='on', retirement_age='60', life_expectancy='90',
              monthly_expense_today='200000', expense_inflation_pct='6', post_retirement_return_pct='5')
    r = client.get('/goals')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Underfunded Retirement' in body
    assert 'runs out at age' in body, "expected the depletion warning to trigger for a badly underfunded retirement goal"
    print("PASS: retirement goal correctly shows the corpus-depletion warning when underfunded")

    # ── Shape 2: Short-horizon goal (0 years left) ──
    add_goal(name='This Year Trip', emoji='✈️', target_amt='150000', target_year=str(current_year),
              current_savings='140000', monthly_sip='5000', annual_return='6')
    r = client.get('/goals')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'This Year Trip' in body
    print("PASS: short-horizon (0-year) goal renders on dashboard without error")

    with app.app_context():
        short_goal = Goal.query.filter_by(user_id=user_id, name='This Year Trip').first()
        assert short_goal.target_year == current_year

    with app.app_context():
        short_goal = Goal.query.filter_by(user_id=user_id, name='This Year Trip').first()
        curve = goal_glide.glide_path_curve(short_goal, linked_value=0.0)
        assert len(curve) == 1, f"0-year goal should produce exactly 1 glide-curve row, got {len(curve)}"
        drawdown = goal_glide.drawdown_curve(short_goal, curve[-1]['projected_corpus'])
        assert drawdown is None, "non-retirement goal must not produce a drawdown curve"
    print("PASS: short-horizon goal's glide curve has exactly 1 row (year 0), no drawdown")

    # ── Shape 3: Multi-holding goal (3 different holding types) ──
    add_goal(name='House Down Payment', emoji='🏠', target_amt='4000000', target_year=str(current_year + 4),
              current_savings='0', monthly_sip='15000', annual_return='10')
    with app.app_context():
        mh_goal = Goal.query.filter_by(user_id=user_id, name='House Down Payment').first()
        mh_id = mh_goal.id

    link_holding(mh_id, f'mutual_fund:{mf_id}', 50, 'equity')   # 50% of 50000 = 25000, equity
    link_holding(mh_id, f'stock:{stock_id}', 100, 'equity')     # 100% of 25000 = 25000, equity
    link_holding(mh_id, f'wealth_asset:{fd_id}', 40, 'debt')    # 40% of 300000 = 120000, debt

    with app.app_context():
        mh_goal = Goal.query.filter_by(user_id=user_id, name='House Down Payment').first()
        assert len(mh_goal.links) == 3, f"expected 3 links, got {len(mh_goal.links)}"

    with app.app_context():
        mh_goal = Goal.query.filter_by(user_id=user_id, name='House Down Payment').first()
        linked_value, linked_rows = _goal_linked_value(mh_goal)
        expected = 50000 * 0.5 + 25000 * 1.0 + 300000 * 0.4
        assert abs(linked_value - expected) < 0.01, f"expected linked_value={expected}, got {linked_value}"
        assert len(linked_rows) == 3

        rebalance = goal_glide.rebalance_suggestion(mh_goal, linked_rows)
        # equity = 25000+25000=50000, debt=120000, total=170000 -> equity% = 29.4%
        # target (glide_start_equity_pct default 75%) is far off -> should suggest debt->equity
        print(f"  rebalance suggestion: {rebalance}")
        assert rebalance is not None, "expected a rebalance suggestion given the skewed equity/debt split"
        assert rebalance['direction'] == 'debt_to_equity'

    r = client.get('/goals')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'House Down Payment' in body
    assert 'Test Equity Fund' in body or 'Test Corp Ltd' in body or 'Test FD' in body
    print(f"PASS: multi-holding goal correctly sums 3 holding types (linked_value={linked_value:,.0f}) "
          f"and generates a rebalance suggestion")

    # Try to over-allocate the same FD beyond what's left (60% already used
    # elsewhere would leave 60% -- try linking 70% to a second goal, should
    # be rejected)
    add_goal(name='Second FD Goal', emoji='🎯', target_amt='1000000', target_year=str(current_year + 2),
              current_savings='0', monthly_sip='5000', annual_return='7')
    with app.app_context():
        second_goal = Goal.query.filter_by(user_id=user_id, name='Second FD Goal').first()
        sg_id = second_goal.id
    r = link_holding(sg_id, f'wealth_asset:{fd_id}', 70, 'debt')
    body = r.get_data(as_text=True)
    assert 'still' in body.lower() and 'unallocated' in body.lower(), \
        "expected an over-allocation error mentioning what's still unallocated"
    with app.app_context():
        second_goal = Goal.query.filter_by(user_id=user_id, name='Second FD Goal').first()
        assert len(second_goal.links) == 0, "over-allocated link must have been rejected"
    print("PASS: over-allocating an already-partially-linked holding is correctly rejected")

    # A 60% link (exactly what's left) should succeed
    r = link_holding(sg_id, f'wealth_asset:{fd_id}', 60, 'debt')
    with app.app_context():
        second_goal = Goal.query.filter_by(user_id=user_id, name='Second FD Goal').first()
        assert len(second_goal.links) == 1, "linking exactly the remaining 60% should have succeeded"
    print("PASS: linking exactly the remaining allocation succeeds")

    # ── PDF export across all these shapes at once ──
    r = client.get('/export/pdf')
    assert r.status_code == 200, r.status_code
    assert r.mimetype == 'application/pdf'
    pdf_bytes = r.data

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pdf_text = '\n'.join(p.extract_text() or '' for p in pdf.pages)

    for name in ['Retirement Corpus', 'This Year Trip', 'House Down Payment', 'Second FD Goal']:
        assert name in pdf_text, f"expected '{name}' to appear in the PDF export"
    print(f"PASS: PDF export succeeds with all 5 goal shapes present ({len(pdf_bytes)} bytes)")

    # Confirm calculate_goal()'s on_track branch -- which feeds status_color
    # -- actually took BOTH branches across these goals (a stronger check
    # than just looking for the ReportLab color bytes in a compressed
    # stream, which isn't reliably greppable).
    assert 'On Track' in pdf_text, "expected at least one goal to show 'On Track' in the PDF"
    assert 'Shortfall' in pdf_text, "expected at least one goal to show a 'Shortfall' in the PDF"
    print("PASS: PDF export shows both On-Track and Shortfall status goals (status_color's two branches both exercised)")

    # Stronger check still: confirm the ACTUAL rendered text color (not
    # just the words) really differs -- this is the exact bug Batch 6
    # flagged (status_color computed but never applied). Green for On
    # Track (_GREEN = #22c55e), red for Shortfall (_RED = #ef4444).
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        on_track_colors, shortfall_colors = [], []
        for p in pdf.pages:
            for w in p.extract_words(extra_attrs=['non_stroking_color']):
                if w['text'] == 'Track':
                    on_track_colors.append(w.get('non_stroking_color'))
                if w['text'] == 'Shortfall':
                    shortfall_colors.append(w.get('non_stroking_color'))

    assert on_track_colors, "no 'On Track' text found to check color on"
    assert shortfall_colors, "no 'Shortfall' text found to check color on"

    assert all(_is_greenish(c) for c in on_track_colors), f"'On Track' text should render green, got {on_track_colors}"
    assert all(_is_reddish(c) for c in shortfall_colors), f"'Shortfall' text should render red, got {shortfall_colors}"
    print(f"PASS: status_color is genuinely applied -- 'On Track' renders green {on_track_colors[0]}, "
          f"'Shortfall' renders red {shortfall_colors[0]}")

    # ── Cleanup ──
    with app.app_context():
        u = User.query.filter_by(email=TEST_EMAIL).first()
        if u:
            GoalHoldingLink.query.filter(
                GoalHoldingLink.goal_id.in_(
                    db.session.query(Goal.id).filter_by(user_id=u.id))).delete(synchronize_session=False)
            Goal.query.filter_by(user_id=u.id).delete()
            MutualFund.query.filter_by(user_id=u.id).delete()
            Stock.query.filter_by(user_id=u.id).delete()
            WealthAsset.query.filter_by(user_id=u.id).delete()
            RetirementScheme.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)
        db.session.commit()

    print("\nALL GOAL-SHAPE TESTS PASSED")
    return True


if __name__ == "__main__":
    run_in_scratch(main)
