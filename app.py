from flask import Flask, render_template, redirect, url_for, request, flash, session, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import bcrypt, pdfplumber, io, re, os, secrets, csv, yfinance as yf
import casparser
from datetime import datetime as dt, timedelta, date as _date_cls
from models import (db, User, MutualFund, MutualFundTransaction, Stock, StockTransaction,
                     Goal, GoalHoldingLink, NetWorthHistory)
from cas_xirr import scheme_xirr as _scheme_xirr, portfolio_xirr as _portfolio_xirr
from stock_xirr import stock_xirr as _stock_xirr, portfolio_stock_xirr as _portfolio_stock_xirr
import goal_glide
from insurance_centre import insurance_bp
from retirement_centre import retirement_bp
from wealth import wealth_bp
from family_centre import family_bp
from backup import backup_bp
from cashflow_centre import cashflow_bp
from wealth.services import WealthStatisticsService
from wealth.models import WealthAssetCategory, WealthAsset
from retirement_centre.models import RetirementScheme


def format_date(d, fmt="%d %b %Y"):
    """Format a date/datetime for display. Returns '—' if None. Own
    copy rather than importing wealth.utils.format_date — matches
    this project's established per-module convention (see
    wealth/utils.py's own top-of-file note on this)."""
    if not d:
        return "—"
    try:
        if isinstance(d, dt):
            return d.strftime(fmt)
        return dt.strptime(str(d)[:10], "%Y-%m-%d").strftime(fmt)
    except Exception:
        return str(d)
from insurance_centre.models import InsuranceDocument, InsurancePolicy

app = Flask(__name__)

def _get_or_create_secret_key():
    """
    Loads SECRET_KEY from instance/secret_key.txt, generating a new
    random one on first run if the file doesn't exist yet.

    Why this approach: the previous SECRET_KEY was a hardcoded literal
    string ("mywealthlens-dev-secret-change-in-production") that had
    been sitting in this repo's git history during every window it
    was made public — meaning anyone who saw the repo during those
    windows could forge a valid session cookie or CSRF token for ANY
    user, no password needed. That key is retired for good, not
    reused here.

    instance/ is already excluded from git (see .gitignore, added
    after the Phase H database-exposure cleanup), so a file stored
    there never gets committed — this generates itself automatically
    on first run and then persists across restarts, with no manual
    environment-variable setup required on a self-hosted single
    Windows machine.
    """
    os.makedirs(app.instance_path, exist_ok=True)
    key_path = os.path.join(app.instance_path, "secret_key.txt")
    if os.path.exists(key_path):
        with open(key_path, "r") as f:
            key = f.read().strip()
            if key:
                return key
    # First run, or file was empty/corrupted — generate a fresh one.
    key = secrets.token_hex(32)
    with open(key_path, "w") as f:
        f.write(key)
    return key

app.config["SECRET_KEY"] = _get_or_create_secret_key()
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///mywealthlens.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB upload limit
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["WTF_CSRF_TIME_LIMIT"] = 3600
# Production-readiness audit (Sep 2026): session cookies must be marked
# Secure (browser only sends them over HTTPS) once this app is actually
# deployed behind real HTTPS — but hard-coding True would silently break
# login during local development, since Mohan's own machine serves plain
# http://127.0.0.1 with no TLS. MWL_HTTPS=1 is the single flag to flip
# once a real deployment with HTTPS is in place (same env-var convention
# already used for HOST/PORT at the bottom of this file).
app.config["SESSION_COOKIE_SECURE"] = os.environ.get('MWL_HTTPS', '0') == '1'
db.init_app(app)

csrf = CSRFProtect(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    # Production-readiness audit (Sep 2026): previously default_limits=[]
    # meant only the 2 explicitly-decorated routes (signup, login) had
    # any rate limit at all — every other POST route across the whole
    # app (57+, including every goal link/unlink/archive, every CAS/
    # CDSL/tradebook upload, every delete) was completely open to abuse.
    # This applies a sane ceiling to EVERY route automatically (Flask-
    # Limiter's default_limits cover all routes registered on `app`,
    # including blueprint routes, once bound here) — routes that need a
    # tighter limit (auth, bulk imports) still override it individually
    # below/in their own files, same as signup/login already did.
    default_limits=["200 per hour", "50 per 15 minutes"],
    storage_uri="memory://"
    # NOTE: memory:// keeps counts in this one process's RAM — correct
    # for today's single dev-server process, but once this runs behind a
    # real WSGI server with multiple worker processes, each worker gets
    # its own separate counter and the limits stop being accurate. Move
    # storage_uri to a shared store (Redis) as part of the production
    # deployment step, once real hosting is chosen.
)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access MyWealthLens."

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

@app.before_request
def refresh_session():
    session.permanent = True
    app.permanent_session_lifetime = timedelta(minutes=30)


with app.app_context():
    db.create_all()

def safe_float(val, default=0.0):
    try:
        return float(str(val).strip())
    except (TypeError, ValueError):
        return default
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/signup', methods=['GET', 'POST'])
@limiter.limit('10 per hour')
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if not name or not email or not password:
            flash('All fields are required.', 'error')
            return render_template('signup.html')
        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('signup.html')
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return render_template('signup.html')
        if User.query.filter_by(email=email).first():
            flash('An account with this email already exists.', 'error')
            return render_template('signup.html')
        hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        user = User(name=name, email=email, password=hashed)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash(f'Welcome to MyWealthLens, {name}!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit('5 per 15 minutes', methods=['POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        if user and bcrypt.checkpw(password.encode('utf-8'), user.password.encode('utf-8')):
            login_user(user)
            flash(f'Welcome back, {user.name}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter_by(email=email).first()
        flash('If an account exists for that email, a reset link has been sent.', 'success')
        return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')

def _save_snapshot(user_id, cat_totals, mfs, stocks, liabilities_total):
    """
    Save one net worth snapshot per day.

    Phase H: previously sourced its per-category figures from the
    legacy Asset model (now retired) and its liability figure from
    the Loan model (which had no CRUD UI and was always empty — so
    liabilities never actually reduced this total). Now sourced from
    WealthAsset (via WealthStatisticsService.category_breakdown(),
    passed in as cat_totals) and WealthLiability, the two authoritative
    Wealth tables. The NetWorthHistory table/columns themselves are
    untouched — existing historical rows remain exactly as stored
    (Section 14 of the Phase H spec: historical snapshots are
    immutable). Going forward, the existing category columns hold the
    closest equivalent under the new Wealth taxonomy:
      gold        -> Precious Metals
      realestate  -> Real Estate
      debt        -> Bank & Deposits + Investments (fixed-income-like)
      cash        -> not distinguished under the new taxonomy; kept at 0
      other       -> Vehicles + Business + Other
    """
    from datetime import date as _date
    today = _date.today()
    existing = NetWorthHistory.query.filter_by(
        user_id=user_id, snapshot_date=today).first()
    if existing:
        return
    equity = sum(m.value for m in mfs) + sum(s.value for s in stocks)
    gold   = cat_totals.get(WealthAssetCategory.PRECIOUS_METALS, 0)
    re_val = cat_totals.get(WealthAssetCategory.REAL_ESTATE, 0)
    debt   = (cat_totals.get(WealthAssetCategory.BANK_DEPOSITS, 0)
              + cat_totals.get(WealthAssetCategory.INVESTMENTS, 0))
    cash   = 0
    other  = (cat_totals.get(WealthAssetCategory.VEHICLES, 0)
              + cat_totals.get(WealthAssetCategory.BUSINESS, 0)
              + cat_totals.get(WealthAssetCategory.OTHER, 0))
    liab   = liabilities_total
    total  = equity + debt + gold + re_val + cash + other - liab
    snap   = NetWorthHistory(
        user_id=user_id, snapshot_date=today, total=total,
        equity=equity, debt=debt, gold=gold,
        realestate=re_val, cash=cash, other=other, liabilities=liab)
    db.session.add(snap)
    db.session.commit()

@app.route('/dashboard')
@login_required
def dashboard():
    # Phase H: asset totals now come exclusively from WealthAsset via
    # WealthStatisticsService — the same authoritative service the
    # Wealth Net Worth page uses — instead of the retired legacy
    # Asset model. MutualFund/Stock (CAS-imported holdings) are a
    # separate, unrelated feature and are untouched.
    mfs    = MutualFund.query.filter_by(user_id=current_user.id).all()
    stocks = Stock.query.filter_by(user_id=current_user.id).all()

    wstats = WealthStatisticsService(current_user.id)
    cat_totals = {b['category']: b['total'] for b in wstats.category_breakdown()}

    real_estate_value     = cat_totals.get(WealthAssetCategory.REAL_ESTATE, 0)
    precious_metals_value = cat_totals.get(WealthAssetCategory.PRECIOUS_METALS, 0)
    vehicles_value        = cat_totals.get(WealthAssetCategory.VEHICLES, 0)
    bank_deposits_value   = cat_totals.get(WealthAssetCategory.BANK_DEPOSITS, 0)
    investments_value     = cat_totals.get(WealthAssetCategory.INVESTMENTS, 0)
    business_value        = cat_totals.get(WealthAssetCategory.BUSINESS, 0)
    other_value            = cat_totals.get(WealthAssetCategory.OTHER, 0)

    mf_value    = sum(m.value for m in mfs)
    stock_value = sum(s.value for s in stocks)

    # Matches the old dashboard's "Total" exactly in spirit: sum of all
    # holdings, no liability subtraction here (the old dashboard never
    # subtracted liabilities from this hero figure either — see the
    # Wealth Net Worth page for the liability-adjusted figure).
    wealth_assets_total = wstats.total_assets()
    total_value  = wealth_assets_total + mf_value + stock_value
    asset_count  = wstats.asset_count() + len(mfs) + len(stocks)

    # Auto daily snapshot — now sourced from WealthAsset + WealthLiability
    if total_value > 0:
        _save_snapshot(current_user.id, cat_totals, mfs, stocks,
                       wstats.total_liabilities())

    # ── Upcoming Commitments (Section: consolidated dashboard view) ──
    # Pulls together anything with a genuinely tracked, near-term due
    # date across modules. Insurance renewal_date already has a
    # mature, tested days-to-renewal/status system (built and
    # verified during the earlier audit) — reused as-is here, not
    # reimplemented. Retirement (SIP schedules) and Wealth Liabilities
    # (EMI due dates) have no equivalent tracked date anywhere in
    # their models — confirmed by direct inspection before building
    # this — so those sections are shown honestly as "not tracked
    # yet" rather than silently omitted or faked.
    active_policies_with_renewal = InsurancePolicy.query.filter_by(
        user_id=current_user.id, is_archived=False
    ).filter(InsurancePolicy.renewal_date.isnot(None)).all()
    has_any_renewal_dates = len(active_policies_with_renewal) > 0
    upcoming_renewals = [
        p for p in active_policies_with_renewal
        if p.renewal_status in ("overdue", "due_soon")
    ]
    upcoming_renewals.sort(key=lambda p: p.renewal_date)

    # History for stacked area chart
    history = NetWorthHistory.query.filter_by(user_id=current_user.id)\
        .order_by(NetWorthHistory.snapshot_date).limit(365).all()
    history_data = [{
        'date':        h.snapshot_date.strftime('%d %b %Y'),
        'total':       h.total,
        'equity':      h.equity,
        'debt':        h.debt,
        'gold':        h.gold,
        'realestate':  h.realestate,
        'cash':        h.cash,
        'other':       h.other,
    } for h in history]

    # ── Unified cross-module Recent Activity (Wealth, Insurance,
    # Retirement, Family Centre merged into one feed) — replaces the
    # four separate per-module "Recent Activity" widgets, their
    # per-item equivalents (Insurance's Policy Timeline, Retirement's
    # per-scheme Activity section), and Family Centre's old standalone
    # Audit Trail page. See activity.py.
    import activity as activity_module
    recent_activity = activity_module.get_unified_activity(current_user.id, days=10)[:5]

    return render_template('dashboard.html',
        user=current_user, total=total_value,
        real_estate=real_estate_value, precious_metals=precious_metals_value,
        vehicles=vehicles_value, bank_deposits=bank_deposits_value,
        investments=investments_value, business=business_value, other=other_value,
        mf=mf_value, stocks=stock_value, mf_count=len(mfs),
        stock_count=len(stocks), mutual_funds=mfs, stock_list=stocks,
        asset_count=asset_count, history_data=history_data,
        upcoming_renewals=upcoming_renewals,
        has_any_renewal_dates=has_any_renewal_dates,
        recent_activity=recent_activity,
        format_date=format_date)


@app.route('/activity')
@login_required
def activity_full():
    """Full unified activity feed — the "View All" destination from
    the Dashboard's Recent Activity widget. Same underlying data
    (get_unified_activity), just unsliced."""
    import activity as activity_module
    all_activity = activity_module.get_unified_activity(current_user.id, days=10)
    return render_template('activity.html', all_activity=all_activity, format_date=format_date)


@app.route('/assets')
@login_required
def assets():
    # Phase H: the standalone legacy Assets module has been retired.
    # Wealth Assets (/wealth/assets) is now the sole authoritative
    # Assets system. This redirect protects any existing bookmarks.
    return redirect(url_for('wealth.assets_listing'))

@app.route('/preferences')
@login_required
def preferences():
    try:
        db.session.execute(db.text('SELECT 1'))
        db_connected, db_status = True, 'Healthy'
    except Exception:
        db_connected, db_status = False, 'Error'
    try:
        doc_count = InsuranceDocument.query.filter_by(user_id=current_user.id).count()
    except Exception:
        doc_count = 'Not Available'
    system_health = {
        'db_connected': db_connected,
        'db_status': db_status,
        'privacy_mode': 'Local Only',
        'version': 'v1.0.0',
        'documents_stored': doc_count,
    }

    from backup import services as backup_services
    backup_settings = backup_services.get_settings(current_user.id)
    backup_last_display = None
    if backup_settings.get('last_backup_at'):
        parsed_dt = dt.fromisoformat(backup_settings['last_backup_at'])
        size_mb = (backup_settings.get('last_backup_size') or 0) / 1024 / 1024
        backup_last_display = parsed_dt.strftime('%d %b %Y, %I:%M %p') + f' UTC ({size_mb:.1f} MB)'

    return render_template(
        'preferences.html', user=current_user, system_health=system_health,
        backup_settings=backup_settings, backup_has_passphrase=backup_services.has_passphrase(),
        backup_last_display=backup_last_display,
    )

@app.route('/account')
@login_required
def account():
    return redirect(url_for('preferences'))

@app.route('/settings')
@login_required
def settings():
    return redirect(url_for('preferences'))

def extract_pdf_text(file_bytes, password=None):
    try:
        pdf_file = io.BytesIO(file_bytes)
        kwargs = {'password': password} if password else {}
        full_text = ''
        with pdfplumber.open(pdf_file, **kwargs) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text += text + chr(10)
        return full_text if full_text.strip() else None
    except Exception as e:
        app.logger.warning('PDF extraction failed: %s', e)
        return None

def _scheme_date(d):
    """casparser's TransactionData.date is Union[date, str] — normalise
    both to a plain date, or None if unparseable."""
    if isinstance(d, _date_cls):
        return d
    try:
        return dt.strptime(str(d)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _decimal_to_float(v):
    return float(v) if v is not None else None


def import_detailed_cas(cas_data, user_id):
    """
    Upload CAS rebuild (see migrate_add_mf_transactions.py). Replaces
    the old hand-rolled regex parser: casparser has already done the
    hard part (folio/scheme/transaction extraction, running-balance
    reconciliation — see cas_data.parse_warnings). This function's job
    is just mapping casparser's typed CASData into our own tables and
    computing XIRR from the resulting transaction rows.

    Wipes and re-imports ALL of this user's mutual_fund /
    mutual_fund_transaction rows — same "re-upload overwrites
    everything" behaviour the old parser had (a DETAILED CAS is
    cumulative, so this is safe and simple; see the FAQ note we found
    on comparable tools — re-uploading is the intended way to refresh).

    Only schemes with a positive closing balance become a holding
    (fully redeemed/closed folios are skipped, matching the old
    parser's behaviour — not a regression, a known follow-up).

    Returns (holdings_count, transactions_count, portfolio_xirr_pct,
    affected_goal_names) — the last covers Goals-page audit Critical #2:
    every re-upload wipes and reinserts with brand-new MutualFund ids,
    so any GoalHoldingLink pointing at the old ids would otherwise go
    silently stale. We snapshot the old ids before the wipe and clean
    up any links to them, rather than trying to fuzzy-re-match by
    scheme name/ISIN (rejected — too easy to silently attach a goal to
    the WRONG fund; a cleared link the user re-links in 10 seconds is
    safer than a wrong one nobody notices).
    """
    stale_mf_ids = [row.id for row in
                     MutualFund.query.filter_by(user_id=user_id).with_entities(MutualFund.id).all()]
    MutualFundTransaction.query.filter_by(user_id=user_id).delete()
    MutualFund.query.filter_by(user_id=user_id).delete()
    affected_goal_names = _cleanup_goal_links_for_deleted_holdings(
        user_id, 'mutual_fund', stale_mf_ids)
    db.session.flush()

    all_transactions_for_portfolio = []
    total_current_value = 0.0
    holdings_count = 0
    transactions_count = 0

    for folio in cas_data.folios:
        for scheme in folio.schemes:
            units = _decimal_to_float(scheme.close)
            if not units or units <= 0:
                continue
            nav = _decimal_to_float(scheme.valuation.nav)
            value = _decimal_to_float(scheme.valuation.value) or 0.0
            invested = _decimal_to_float(scheme.valuation.cost)

            mf = MutualFund(
                user_id=user_id, folio=folio.folio, amc=folio.amc,
                scheme=scheme.scheme, isin=scheme.isin, amfi_code=scheme.amfi,
                units=units, nav=nav, value=value, invested=invested,
                source='cams',
            )
            db.session.add(mf)
            db.session.flush()  # need mf.id for the transaction rows below

            scheme_txns = []
            for t in scheme.transactions:
                txn_date = _scheme_date(t.date)
                if txn_date is None:
                    continue
                row = MutualFundTransaction(
                    user_id=user_id, mutual_fund_id=mf.id,
                    folio=folio.folio, scheme=scheme.scheme,
                    date=txn_date,
                    txn_type=t.type.value if hasattr(t.type, "value") else str(t.type),
                    description=t.description,
                    amount=_decimal_to_float(t.amount),
                    units=_decimal_to_float(t.units),
                    nav=_decimal_to_float(t.nav),
                    balance_units=_decimal_to_float(t.balance),
                )
                db.session.add(row)
                scheme_txns.append(row)
                transactions_count += 1

            mf.xirr = _scheme_xirr(scheme_txns, value)
            all_transactions_for_portfolio.extend(scheme_txns)
            total_current_value += value
            holdings_count += 1

    portfolio_rate = _portfolio_xirr(all_transactions_for_portfolio, total_current_value)
    db.session.commit()
    return holdings_count, transactions_count, portfolio_rate, affected_goal_names

def _clean_stock_name(raw_name):
    """Remove PDF artifacts, page numbers, dates and junk from holding names."""
    name = raw_name.strip()
    # Remove trailing numbers, dates, balance figures
    name = re.sub(r'\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}.*$', '', name)  # dates
    name = re.sub(r'\s+\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*$', '', name)  # trailing numbers
    name = re.sub(r'\s+[A-Z]{2}\d+.*$', '', name)  # ISIN-like artifacts
    name = re.sub(r'\s{2,}', ' ', name)  # collapse spaces
    # Remove common PDF noise words at end
    noise = ['DEMAT', 'NSDL', 'CDSL', 'DP ID', 'CLIENT ID', 'CIN', 'PAN', 'ISIN']
    for word in noise:
        name = re.sub(rf'\s+{word}.*$', '', name, flags=re.IGNORECASE)
    name = name.strip(" -|/\\.,")
    return name[:80] if name else 'Unknown'

def parse_cdsl_pdf(text):
    holdings = []
    if 'STATEMENT OF HOLDINGS' in text:
        start = text.find('STATEMENT OF HOLDINGS')
        text = text[start:]
    isin_re = re.compile(r'(IN[A-Z0-9]{10})')
    # Match integers AND decimals
    int_re  = re.compile(r'\b(\d+)\b')
    dec_re  = re.compile(r'(\d+\.\d+)')
    lines_list = [l.strip() for l in text.split(chr(10)) if l.strip()]
    isin_positions = []
    for idx, line in enumerate(lines_list):
        m = isin_re.search(line)
        if m:
            isin_positions.append((idx, m.group(1), line))
    for pos_idx, (idx, isin, line) in enumerate(isin_positions):
        if pos_idx + 1 < len(isin_positions):
            next_idx = isin_positions[pos_idx + 1][0]
        else:
            next_idx = min(idx + 8, len(lines_list))
        block_lines = lines_list[idx:next_idx]
        block = ' '.join(block_lines)

        # ── Fix Bug 2: SGB quantity ──
        # SGBs are identified by ISIN starting with IN0 or name containing SGB/GOLD BOND
        is_sgb = ('SGB' in block.upper() or 'GOLD BOND' in block.upper()
                  or 'SOVEREIGN' in block.upper())

        # Get all decimal numbers (quantity and value are decimals in CDSL)
        dec_nums = [float(n) for n in dec_re.findall(block)]
        # Get all integers (SGB units are integers)
        int_nums = [int(n) for n in int_re.findall(block)
                    if 0 < int(n) < 100000]

        if not dec_nums:
            continue

        value = max(dec_nums)

        if is_sgb:
            # For SGBs: quantity is the number of bonds (integer, usually small like 1,2,3)
            # Pick the smallest positive integer that makes sense as unit count
            sgb_qty_candidates = [n for n in int_nums if 0 < n <= 1000]
            quantity = float(min(sgb_qty_candidates)) if sgb_qty_candidates else 1.0
        else:
            # Normal stocks: quantity is decimal, smaller than value
            qty_candidates = [n for n in dec_nums if 0 < n < value]
            if not qty_candidates:
                continue
            quantity = qty_candidates[0]

        # ── Fix Bug 1: Clean stock/MF name ──
        raw_name = line.replace(isin, '').strip()
        name = _clean_stock_name(raw_name)

        price = round(value / quantity, 2) if quantity > 0 else 0
        holdings.append({'isin': isin, 'name': name,
            'quantity': quantity, 'price': price, 'value': value})

    # Deduplicate by ISIN
    seen = set()
    unique = []
    for h in holdings:
        if h['isin'] not in seen:
            seen.add(h['isin'])
            unique.append(h)
    return unique

def fetch_live_price_by_isin(isin, name):
    try:
        t = yf.Ticker(isin)
        price = float(t.fast_info.last_price)
        if price and price > 0:
            return isin, round(price, 2)
    except:
        pass
    try:
        ticker_guess = re.sub(r'[^A-Z0-9]', '', name.upper()[:10]) + '.NS'
        t = yf.Ticker(ticker_guess)
        price = float(t.fast_info.last_price)
        if price and price > 0:
            return ticker_guess, round(price, 2)
    except:
        pass
    return None, None
@app.route('/upload')
@login_required
def upload():
    mf_count = MutualFund.query.filter_by(user_id=current_user.id).count()
    st_count = Stock.query.filter_by(user_id=current_user.id).count()
    return render_template('upload.html', mf_count=mf_count, st_count=st_count)

@app.route('/upload/cams', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def upload_cams():
    pdf_file = request.files.get('pdf_file')
    password = request.form.get('password', '').strip().upper()
    if not pdf_file or pdf_file.filename == '':
        flash('Please select a PDF file.', 'error')
        return redirect(url_for('upload'))
    try:
        file_bytes = pdf_file.read()
    except Exception:
        flash('Could not read the uploaded file.', 'error')
        return redirect(url_for('upload'))

    try:
        cas_data = casparser.read_cas_pdf(io.BytesIO(file_bytes), password, output='dict')
    except casparser.exceptions.IncorrectPasswordError:
        flash('Incorrect password. Please check it and try again.', 'error')
        return redirect(url_for('upload'))
    except casparser.exceptions.ParserException as e:
        app.logger.warning('CAS parse failed: %s', e)
        flash('Could not read this PDF as a CAMS/KFintech CAS statement. '
              'Please check the file and try again.', 'error')
        return redirect(url_for('upload'))
    except Exception as e:
        app.logger.warning('CAS parse failed (unexpected): %s', e)
        flash('Could not read the PDF. Please check the password and try again.', 'error')
        return redirect(url_for('upload'))

    if not hasattr(cas_data, 'folios'):
        # NSDLCASData (a CDSL/NSDL demat statement) landed on the wrong
        # upload form — it has .accounts, not .folios.
        flash('This looks like a CDSL/NSDL demat statement, not a CAMS/KFintech '
              'mutual fund statement. Use the Stocks upload for that instead.', 'error')
        return redirect(url_for('upload'))

    if not cas_data.folios:
        flash('No mutual fund holdings found in this PDF.', 'error')
        return redirect(url_for('upload'))

    holdings_count, transactions_count, portfolio_rate, affected_goal_names = import_detailed_cas(cas_data, current_user.id)

    if holdings_count == 0:
        flash('No active mutual fund holdings found in this PDF (all folios may be zero-balance).', 'error')
        return redirect(url_for('upload'))

    if cas_data.cas_type == 'SUMMARY':
        flash(f'Imported {holdings_count} mutual fund holdings, but this was a SUMMARY '
              f'statement — it has no transaction history, so XIRR isn’t available. '
              f'Re-download from camsonline.com with Statement Type set to “Detailed” '
              f'to get real returns.', 'warning')
    else:
        xirr_msg = f' Portfolio XIRR: {portfolio_rate}%.' if portfolio_rate is not None else ''
        flash(f'Successfully imported {holdings_count} mutual fund holdings '
              f'({transactions_count} transactions).{xirr_msg}', 'success')

    if cas_data.parse_warnings:
        flash(f'{len(cas_data.parse_warnings)} scheme(s) had data that didn’t fully '
              f'reconcile against the statement’s own running balance — double-check '
              f'those holdings before relying on their numbers.', 'warning')

    if affected_goal_names:
        goal_list = ', '.join(affected_goal_names)
        flash(f'This re-upload replaced your mutual fund holdings with new entries, so '
              f'{len(affected_goal_names)} goal(s) lost their old holding link(s): {goal_list}. '
              f'Their shortfall now reflects that — re-link the right holding(s) on the Goals page.', 'warning')

    return redirect(url_for('upload'))

@app.route('/upload/cdsl', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def upload_cdsl():
    pdf_file = request.files.get('pdf_file')
    password = request.form.get('password', '').strip()
    if not pdf_file or pdf_file.filename == '':
        flash('Please select a PDF file.', 'error')
        return redirect(url_for('upload'))
    try:
        file_bytes = pdf_file.read()
    except Exception:
        flash('Could not read the uploaded file.', 'error')
        return redirect(url_for('upload'))
    text = extract_pdf_text(file_bytes, password if password else None)
    if not text:
        flash('Could not read the PDF. Please check the password and try again.', 'error')
        return redirect(url_for('upload'))
    holdings = parse_cdsl_pdf(text)
    if not holdings:
        flash('No stock holdings found in this PDF.', 'error')
        return redirect(url_for('upload'))
    stale_stock_ids = [row.id for row in
                        Stock.query.filter_by(user_id=current_user.id).with_entities(Stock.id).all()]
    Stock.query.filter_by(user_id=current_user.id).delete()
    affected_goal_names = _cleanup_goal_links_for_deleted_holdings(
        current_user.id, 'stock', stale_stock_ids)
    live_fetched = 0
    for h in holdings:
        ticker, live_price = fetch_live_price_by_isin(h['isin'], h['name'])
        if live_price:
            value = round(h['quantity'] * live_price, 2)
            live_fetched += 1
        else:
            live_price = h['price']
            value = h['value']
        stock = Stock(user_id=current_user.id, isin=h['isin'], name=h['name'],
            quantity=h['quantity'], buy_price=h['price'], live_price=live_price,
            value=value, ticker=ticker, source='cdsl',
            price_updated_at=dt.utcnow() if live_price else None)
        db.session.add(stock)
    db.session.commit()
    flash(f'Imported {len(holdings)} stocks. Live prices fetched for {live_fetched}.', 'success')
    if affected_goal_names:
        goal_list = ', '.join(affected_goal_names)
        flash(f'This re-upload replaced your stock holdings with new entries, so '
              f'{len(affected_goal_names)} goal(s) lost their old holding link(s): {goal_list}. '
              f'Their shortfall now reflects that — re-link the right holding(s) on the Goals page.', 'warning')
    return redirect(url_for('upload'))

@app.route('/upload/delete-mf', methods=['POST'])
@login_required
@limiter.limit('20 per hour')
def delete_all_mf():
    stale_mf_ids = [row.id for row in
                     MutualFund.query.filter_by(user_id=current_user.id).with_entities(MutualFund.id).all()]
    MutualFund.query.filter_by(user_id=current_user.id).delete()
    affected_goal_names = _cleanup_goal_links_for_deleted_holdings(
        current_user.id, 'mutual_fund', stale_mf_ids)
    db.session.commit()
    flash('All mutual fund data cleared.', 'success')
    if affected_goal_names:
        goal_list = ', '.join(affected_goal_names)
        flash(f'This also removed {len(affected_goal_names)} goal(s)\' link(s) to that data: {goal_list} — '
              f'their shortfall has been updated.', 'warning')
    return redirect(url_for('upload'))

@app.route('/upload/delete-stocks', methods=['POST'])
@login_required
@limiter.limit('20 per hour')
def delete_all_stocks():
    stale_stock_ids = [row.id for row in
                        Stock.query.filter_by(user_id=current_user.id).with_entities(Stock.id).all()]
    Stock.query.filter_by(user_id=current_user.id).delete()
    affected_goal_names = _cleanup_goal_links_for_deleted_holdings(
        current_user.id, 'stock', stale_stock_ids)
    db.session.commit()
    flash('All stock data cleared.', 'success')
    if affected_goal_names:
        goal_list = ', '.join(affected_goal_names)
        flash(f'This also removed {len(affected_goal_names)} goal(s)\' link(s) to that data: {goal_list} — '
              f'their shortfall has been updated.', 'warning')
    return redirect(url_for('upload'))


# ── Broker tradebook import ──────────────────────────────────────────────────
# Header names vary by broker — this is deliberately a flexible
# substring matcher (built against Zerodha's console tradebook export,
# the most common format for Indian retail investors) rather than a
# hard-coded column list, so tradebooks from other brokers with
# differently-cased or differently-ordered but similarly-named columns
# still have a reasonable chance of matching. Ambiguous or missing
# columns fail loudly (see import_tradebook()) rather than guessing.
_TRADEBOOK_COLUMN_HINTS = {
    'symbol':   ['tradingsymbol', 'symbol', 'scrip', 'stock name', 'company'],
    'isin':     ['isin'],
    'date':     ['trade_date', 'trade date', 'date'],
    'type':     ['trade_type', 'transaction_type', 'txn_type', 'type', 'side'],
    'quantity': ['quantity', 'qty'],
    'price':    ['price', 'rate', 'trade_price'],
}


def _match_tradebook_columns(fieldnames):
    """Returns {field: actual_csv_column_name} for each of the 6 needed
    fields, or raises ValueError naming what's missing."""
    normalized = {f.strip().lower(): f for f in fieldnames if f}
    matched = {}
    missing = []
    for field, hints in _TRADEBOOK_COLUMN_HINTS.items():
        found = None
        for hint in hints:
            for norm, original in normalized.items():
                if hint in norm:
                    found = original
                    break
            if found:
                break
        if found:
            matched[field] = found
        else:
            missing.append(field)
    if missing:
        raise ValueError(
            f"Could not find a column for: {', '.join(missing)}. "
            f"Columns found in file: {', '.join(fieldnames)}"
        )
    return matched


def _parse_tradebook_csv(file_text):
    """Returns a list of dicts: {symbol, isin, date, txn_type, quantity,
    price, amount}, one per valid row. Rows that fail to parse (bad
    date, non-numeric quantity/price, unrecognised BUY/SELL value) are
    skipped and counted, not silently included with wrong data."""
    reader = csv.DictReader(file_text.splitlines())
    if not reader.fieldnames:
        raise ValueError("File doesn't look like a CSV (no header row found).")
    cols = _match_tradebook_columns(reader.fieldnames)

    rows = []
    skipped = 0
    for raw in reader:
        try:
            symbol = (raw.get(cols['symbol']) or '').strip()
            isin = (raw.get(cols['isin']) or '').strip() or None
            date_str = (raw.get(cols['date']) or '').strip()
            type_str = (raw.get(cols['type']) or '').strip().upper()
            qty = float(raw.get(cols['quantity']) or 0)
            price = float(raw.get(cols['price']) or 0)

            if type_str in ('BUY', 'B'):
                txn_type = 'BUY'
            elif type_str in ('SELL', 'S'):
                txn_type = 'SELL'
            else:
                skipped += 1
                continue

            txn_date = None
            for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y', '%Y/%m/%d'):
                try:
                    txn_date = dt.strptime(date_str[:10], fmt).date()
                    break
                except ValueError:
                    continue
            if txn_date is None or not symbol or qty <= 0:
                skipped += 1
                continue

            rows.append({
                'symbol': symbol, 'isin': isin, 'date': txn_date,
                'txn_type': txn_type, 'quantity': qty, 'price': price,
                'amount': round(qty * price, 2),
            })
        except (ValueError, TypeError):
            skipped += 1
            continue
    return rows, skipped


def import_tradebook(rows, user_id):
    """
    Rebuilds this user's TRADEBOOK-sourced stock holdings from a list
    of parsed BUY/SELL rows (see _parse_tradebook_csv()). Mirrors
    import_detailed_cas()'s wipe-and-rebuild approach: only rows with
    source='tradebook' are wiped first, so stocks imported via the
    separate CDSL/NSDL CAS upload (source='cdsl') are left untouched —
    the two import paths are independent, matching how a real investor
    might use CDSL for a snapshot balance and a broker tradebook for
    the transaction history behind it.

    Grouped by (symbol, isin): closing quantity = sum(BUY qty) -
    sum(SELL qty). Only positive-quantity positions become a holding
    (a fully-sold position is skipped, same convention as mutual
    funds). Live price is fetched the same way the CDSL importer does;
    falls back to the last transaction's price if that fails.

    Returns (holdings_count, transactions_count, portfolio_rate,
    affected_goal_names) — see import_detailed_cas()'s docstring for
    why the last element exists (Goals-page audit, Sep 2026): new stock
    rows get new ids on every re-upload, so any GoalHoldingLink to the
    old tradebook-sourced ids is explicitly cleaned up here rather than
    left to go stale.
    """
    stale_stock_ids = [row.id for row in
        Stock.query.filter_by(user_id=user_id, source='tradebook').with_entities(Stock.id).all()]
    Stock.query.filter_by(user_id=user_id, source='tradebook').delete()
    affected_goal_names = _cleanup_goal_links_for_deleted_holdings(
        user_id, 'stock', stale_stock_ids)
    db.session.flush()

    groups = {}
    for r in rows:
        key = (r['symbol'], r['isin'])
        groups.setdefault(key, []).append(r)

    all_transactions_for_portfolio = []
    total_current_value = 0.0
    holdings_count = 0
    transactions_count = 0

    for (symbol, isin), txns in groups.items():
        txns.sort(key=lambda r: r['date'])
        net_qty = sum(t['quantity'] if t['txn_type'] == 'BUY' else -t['quantity'] for t in txns)
        if net_qty <= 0:
            continue

        last_price = txns[-1]['price'] or 0
        ticker, live_price = fetch_live_price_by_isin(isin or symbol, symbol)
        price = live_price or last_price
        value = round(net_qty * price, 2)
        invested = sum(t['amount'] for t in txns if t['txn_type'] == 'BUY')

        stock = Stock(
            user_id=user_id, isin=isin or '', name=symbol, quantity=net_qty,
            buy_price=last_price, live_price=price, value=value,
            ticker=ticker, source='tradebook', invested=invested,
            price_updated_at=dt.utcnow() if live_price else None,
        )
        db.session.add(stock)
        db.session.flush()  # need stock.id for the transaction rows below

        stock_txns = []
        for t in txns:
            row = StockTransaction(
                user_id=user_id, stock_id=stock.id, isin=isin, symbol=symbol,
                date=t['date'], txn_type=t['txn_type'], quantity=t['quantity'],
                price=t['price'], amount=t['amount'],
            )
            db.session.add(row)
            stock_txns.append(row)
            transactions_count += 1

        stock.xirr = _stock_xirr(stock_txns, value)
        all_transactions_for_portfolio.extend(stock_txns)
        total_current_value += value
        holdings_count += 1

    portfolio_rate = _portfolio_stock_xirr(all_transactions_for_portfolio, total_current_value)
    db.session.commit()
    return holdings_count, transactions_count, portfolio_rate, affected_goal_names


@app.route('/upload/tradebook', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def upload_tradebook():
    csv_file = request.files.get('csv_file')
    if not csv_file or csv_file.filename == '':
        flash('Please select a CSV file.', 'error')
        return redirect(url_for('upload'))
    try:
        file_text = csv_file.read().decode('utf-8-sig', errors='replace')
    except Exception:
        flash('Could not read the uploaded file.', 'error')
        return redirect(url_for('upload'))

    try:
        rows, skipped = _parse_tradebook_csv(file_text)
    except ValueError as e:
        flash(f'Could not read this as a tradebook CSV: {e}', 'error')
        return redirect(url_for('upload'))

    if not rows:
        flash('No valid BUY/SELL rows found in this file.', 'error')
        return redirect(url_for('upload'))

    holdings_count, transactions_count, portfolio_rate, affected_goal_names = import_tradebook(rows, current_user.id)

    if holdings_count == 0:
        flash('No open positions found (all holdings in this tradebook may be fully sold).', 'error')
        return redirect(url_for('upload'))

    xirr_msg = f' Portfolio XIRR: {portfolio_rate}%.' if portfolio_rate is not None else ''
    skip_msg = f' ({skipped} row(s) skipped — unrecognised format.)' if skipped else ''
    flash(f'Imported {holdings_count} stock holdings ({transactions_count} transactions).'
          f'{xirr_msg}{skip_msg}', 'success')
    if affected_goal_names:
        goal_list = ', '.join(affected_goal_names)
        flash(f'This re-upload replaced your tradebook stock holdings with new entries, so '
              f'{len(affected_goal_names)} goal(s) lost their old holding link(s): {goal_list}. '
              f'Their shortfall now reflects that — re-link the right holding(s) on the Goals page.', 'warning')
    return redirect(url_for('upload'))

def _stepup_sip_future_value(monthly_sip, step_up_pct, annual_return, years):
    """
    FV of a monthly SIP that increases by step_up_pct once a year.
    Year-by-year: each year's 12 payments are grown to THAT year's
    end (annuity-due — payment at the start of each month, matching
    the flat-SIP formula's own (1+r) tail factor below), then that
    year's total is compounded forward to the goal date. The SIP
    amount for next year is only stepped up after a full year.

    Only called when step_up_pct > 0 — see calculate_goal(). At
    step_up_pct == 0 this would be mathematically equivalent to the
    flat formula, but the flat formula's own code path is kept as
    the one actually used for that case (years of production use,
    zero reason to risk a rounding-level behaviour change for
    everyone's existing goals).
    """
    r = (annual_return / 100) / 12
    whole_years = int(years)
    frac_months = round((years - whole_years) * 12)
    fv = 0.0
    sip = monthly_sip
    for y in range(whole_years):
        months_remaining = (whole_years - y - 1) * 12 + frac_months
        if r > 0:
            fv_this_year = sip * (((1 + r) ** 12 - 1) / r) * (1 + r)
        else:
            fv_this_year = sip * 12
        fv += fv_this_year * ((1 + r) ** months_remaining)
        sip *= (1 + step_up_pct / 100)
    if frac_months > 0:
        if r > 0:
            fv += sip * (((1 + r) ** frac_months - 1) / r) * (1 + r)
        else:
            fv += sip * frac_months
    return fv


def calculate_goal(target_amt, target_year, current_savings, monthly_sip, annual_return,
                    inflation_rate=0, step_up_pct=0):
    from datetime import datetime as _dt
    current_year = _dt.now().year
    years  = max(target_year - current_year, 0)
    months = max(years * 12, 1)
    r = (annual_return / 100) / 12

    # Inflation-adjusted target
    if inflation_rate and inflation_rate > 0:
        inflation_adjusted_target = round(target_amt * ((1 + inflation_rate / 100) ** years), 2)
    else:
        inflation_adjusted_target = target_amt

    if step_up_pct and step_up_pct > 0:
        fv_sip = _stepup_sip_future_value(monthly_sip, step_up_pct, annual_return, years)
    elif r > 0:
        fv_sip = monthly_sip * (((1 + r) ** months - 1) / r) * (1 + r)
    else:
        fv_sip = monthly_sip * months
    fv_savings = current_savings * ((1 + r) ** months)
    projected  = round(fv_sip + fv_savings, 2)

    shortfall = round(inflation_adjusted_target - projected, 2)
    if r > 0 and shortfall > 0:
        required_sip = round((shortfall * r) / (((1 + r) ** months - 1) * (1 + r)), 2)
    else:
        required_sip = 0
    progress   = min(round((projected / inflation_adjusted_target) * 100, 1), 100) if inflation_adjusted_target > 0 else 0
    years_left = target_year - current_year
    return {
        'projected':                  projected,
        'shortfall':                  shortfall,
        'surplus':                    max(-shortfall, 0),
        'on_track':                   shortfall <= 0,
        'required_sip':               required_sip,
        'progress':                   progress,
        'years_left':                 years_left,
        'months':                     months,
        'inflation_adjusted_target':  inflation_adjusted_target,
        'inflation_applied':          inflation_rate > 0,
    }

def _holding_lookup(holding_type, holding_id):
    """Returns (holding, raw_value, display_name) for one of the four
    linkable holding types, or (None, 0, None) if it can't be found
    (deleted, or a re-upload wiped it — see import_detailed_cas() /
    import_tradebook(), both of which wipe-and-rebuild on re-import)."""
    if holding_type == 'mutual_fund':
        h = MutualFund.query.get(holding_id)
        return (h, (h.value or 0), h.scheme) if h else (None, 0, None)
    if holding_type == 'stock':
        h = Stock.query.get(holding_id)
        return (h, (h.value or 0), h.name) if h else (None, 0, None)
    if holding_type == 'wealth_asset':
        h = WealthAsset.query.get(holding_id)
        return (h, (h.current_value or 0), h.name) if h else (None, 0, None)
    if holding_type == 'retirement_scheme':
        h = RetirementScheme.query.get(holding_id)
        name = h.custom_type or h.scheme_type if h else None
        return (h, (h.current_balance or 0), name) if h else (None, 0, None)
    return (None, 0, None)


def _goal_linked_value(goal):
    """Sum of (holding_value * allocation_pct/100) across a goal's
    linked holdings, across all four linkable holding types — see
    GoalHoldingLink's docstring in models.py and _holding_lookup()
    above. Returns (linked_value, [ {link, holding, name,
    allocated_value} ... ]) — the list is for the template to render
    names/values and for goal_glide.rebalance_suggestion(), since
    GoalHoldingLink can't hold a real SQLAlchemy relationship across
    holding types."""
    linked_value = 0.0
    rows = []
    for link in goal.links:
        holding, raw_value, name = _holding_lookup(link.holding_type, link.holding_id)
        if holding is None:
            continue  # holding was deleted / re-upload wiped it — link is now stale, skip silently
        allocated_value = raw_value * (link.allocation_pct / 100)
        linked_value += allocated_value
        rows.append({'link': link, 'holding': holding, 'name': name, 'allocated_value': allocated_value})
    return linked_value, rows


def _holding_allocated_pct(holding_type, holding_id, user_id):
    """Total allocation_pct already committed to this ONE holding
    across ALL of the user's goals. A holding is a single pool of
    real money, so this can never legitimately exceed 100 — enforced
    in link_goal_holding(), which is the only place new links are
    created. (Goals-page audit, Sep 2026: previously unenforced, so
    the same fund could be linked at 100% to two different goals and
    silently double-counted.)"""
    rows = (GoalHoldingLink.query
        .join(Goal, GoalHoldingLink.goal_id == Goal.id)
        .filter(Goal.user_id == user_id,
                GoalHoldingLink.holding_type == holding_type,
                GoalHoldingLink.holding_id == holding_id)
        .all())
    return sum(l.allocation_pct for l in rows)


def _cleanup_goal_links_for_deleted_holdings(user_id, holding_type, deleted_holding_ids):
    """Call this right after a holding (or a batch of holdings) of
    the given type is gone for good — a manual delete, or a bulk
    wipe-and-reimport (CAS/CDSL/tradebook). Deletes any GoalHoldingLink
    rows that pointed at those now-gone ids, so a goal's linked value
    drops (and its shortfall reappears) immediately instead of
    silently keeping a dead link forever with no way to see or remove
    it (Goals-page audit, Sep 2026). Returns the sorted list of
    distinct goal names that were affected, for a flash message —
    does NOT commit; the caller is expected to already be inside a
    commit for the holding change itself."""
    if not deleted_holding_ids:
        return []
    links = (GoalHoldingLink.query
        .join(Goal, GoalHoldingLink.goal_id == Goal.id)
        .filter(Goal.user_id == user_id,
                GoalHoldingLink.holding_type == holding_type,
                GoalHoldingLink.holding_id.in_(deleted_holding_ids))
        .all())
    affected_goal_names = sorted({link.goal.name for link in links})
    for link in links:
        db.session.delete(link)
    return affected_goal_names


GOAL_REVIEW_PERIOD_DAYS = 90


@app.route('/goals')
@login_required
def goals():
    current_year = dt.utcnow().year
    user_goals = (Goal.query
        .filter_by(user_id=current_user.id, is_archived=False)
        .order_by(Goal.target_year).all())
    goals_data = []
    review_cutoff = dt.utcnow() - timedelta(days=GOAL_REVIEW_PERIOD_DAYS)

    near_term_count = 0
    long_term_count = 0
    achieved_suggested_count = 0

    for g in user_goals:
        linked_value, linked_rows = _goal_linked_value(g)
        effective_current = (g.current_savings or 0) + linked_value
        calc = calculate_goal(g.target_amt, g.target_year, effective_current, g.monthly_sip,
                               g.annual_return, getattr(g, 'inflation_rate', 0) or 0,
                               getattr(g, 'step_up_pct', 0) or 0)

        glide_curve = goal_glide.glide_path_curve(g, linked_value=linked_value)
        rebalance = goal_glide.rebalance_suggestion(g, linked_rows)
        drawdown = goal_glide.drawdown_curve(g, glide_curve[-1]['projected_corpus'] if glide_curve else 0)
        needs_review = (g.last_reviewed_at is None) or (g.last_reviewed_at < review_cutoff)

        # Already funded today, regardless of years left — different
        # from calc['on_track'] (which is about projected future SIP
        # growth reaching the target by target_year). A goal can be
        # on_track without being achieved yet, and vice versa (e.g.
        # a lump sum landed early). Suggested, never auto-applied —
        # the user confirms via the "Mark as Achieved" action, same
        # spirit as the review nudge.
        compare_target = (calc['inflation_adjusted_target']
                           if calc.get('inflation_applied') else g.target_amt)
        is_achieved_suggested = effective_current >= compare_target and compare_target > 0
        if is_achieved_suggested:
            achieved_suggested_count += 1

        years_left = g.target_year - current_year
        if years_left <= 1:
            near_term_count += 1
        else:
            long_term_count += 1

        # "type:id" keys already linked to THIS goal, so its own
        # "link a holding" dropdown doesn't re-offer them — picking
        # one again would always be rejected by link_goal_holding()'s
        # duplicate check, so there's no reason to show it as an
        # option (it's already visible above, in Linked Holdings).
        linked_holding_keys = {f'{l.holding_type}:{l.holding_id}' for l in g.links}

        goals_data.append({
            'goal': g, 'calc': calc, 'linked_value': linked_value, 'linked_rows': linked_rows,
            'glide_curve': glide_curve, 'rebalance': rebalance, 'drawdown': drawdown,
            'needs_review': needs_review, 'years_left': years_left,
            'is_achieved_suggested': is_achieved_suggested,
            'linked_holding_keys': linked_holding_keys,
        })

    archived_total_count = Goal.query.filter_by(user_id=current_user.id, is_archived=True).count()
    summary_stats = {
        'active_count': len(user_goals),
        'achieved_count': Goal.query.filter_by(
            user_id=current_user.id, is_archived=True, archive_reason='achieved').count(),
        'near_term_count': near_term_count,
        'long_term_count': long_term_count,
        'achieved_suggested_count': achieved_suggested_count,
        'archived_total_count': archived_total_count,
    }

    # For the "link a holding" picker — every holding is listed, but
    # each is annotated with how much of it is STILL unallocated
    # across the user's goals (100% minus whatever's already linked
    # elsewhere), so the dropdown can show e.g. "only ₹8,00,000 (80%)
    # left" and the form can cap/default to that amount. See
    # link_goal_holding(), which enforces this same 100% ceiling
    # server-side (Goals-page audit, Sep 2026 — previously the
    # comment here claimed this filtering existed and it didn't).
    def _annotate_availability(items, holding_type, value_attr):
        for h in items:
            raw_value = getattr(h, value_attr) or 0
            already_pct = _holding_allocated_pct(holding_type, h.id, current_user.id)
            remaining_pct = max(round(100 - already_pct, 2), 0)
            h.remaining_pct = remaining_pct
            h.remaining_value = raw_value * (remaining_pct / 100)
        return items

    available_funds = _annotate_availability(
        MutualFund.query.filter_by(user_id=current_user.id).order_by(MutualFund.scheme).all(),
        'mutual_fund', 'value')
    available_stocks = _annotate_availability(
        Stock.query.filter_by(user_id=current_user.id).order_by(Stock.name).all(),
        'stock', 'value')
    available_wealth_assets = _annotate_availability(
        (WealthAsset.query.filter_by(user_id=current_user.id, is_archived=False)
            .order_by(WealthAsset.name).all()),
        'wealth_asset', 'current_value')
    available_retirement_schemes = _annotate_availability(
        (RetirementScheme.query.filter_by(user_id=current_user.id, is_archived=False)
            .order_by(RetirementScheme.scheme_type).all()),
        'retirement_scheme', 'current_balance')

    return render_template('goals.html', goals_data=goals_data,
        summary_stats=summary_stats,
        available_funds=available_funds, available_stocks=available_stocks,
        available_wealth_assets=available_wealth_assets,
        available_retirement_schemes=available_retirement_schemes)

@app.route('/goals/add', methods=['POST'])
@login_required
def add_goal():
    name        = request.form.get('name', '').strip()
    emoji       = request.form.get('emoji', '').strip()
    target_amt  = safe_float(request.form.get('target_amt'))
    target_year = int(request.form.get('target_year', 2030))
    current_savings = safe_float(request.form.get('current_savings'))
    monthly_sip     = safe_float(request.form.get('monthly_sip'))
    annual_return   = safe_float(request.form.get('annual_return', '12'))
    if not name or target_amt <= 0:
        flash('Please enter a goal name and target amount.', 'error')
        return redirect(url_for('goals'))
    if target_year <= 2024:
        flash('Target year must be in the future.', 'error')
        return redirect(url_for('goals'))
    inflation_rate = safe_float(request.form.get('inflation_rate', '0'))
    step_up_pct    = safe_float(request.form.get('step_up_pct', '0'))
    glide_start    = safe_float(request.form.get('glide_start_equity_pct', '75'))
    glide_end      = safe_float(request.form.get('glide_end_equity_pct', '30'))
    is_retirement_goal = request.form.get('is_retirement_goal') == 'on'
    retirement_age        = request.form.get('retirement_age', type=int)
    life_expectancy        = request.form.get('life_expectancy', type=int) or 85
    monthly_expense_today  = request.form.get('monthly_expense_today', type=float)
    expense_inflation_pct  = safe_float(request.form.get('expense_inflation_pct', '6'))
    post_retirement_return_pct = safe_float(request.form.get('post_retirement_return_pct', '7'))
    goal = Goal(
        user_id=current_user.id, name=name, emoji=emoji,
        target_amt=target_amt, target_year=target_year,
        current_savings=current_savings, monthly_sip=monthly_sip,
        annual_return=annual_return if annual_return > 0 else 12.0,
        inflation_rate=inflation_rate if inflation_rate >= 0 else 0,
        step_up_pct=step_up_pct if step_up_pct >= 0 else 0,
        glide_start_equity_pct=max(min(glide_start, 100), 0),
        glide_end_equity_pct=max(min(glide_end, 100), 0),
        is_retirement_goal=is_retirement_goal,
        retirement_age=retirement_age if is_retirement_goal else None,
        life_expectancy=life_expectancy,
        monthly_expense_today=monthly_expense_today if is_retirement_goal else None,
        expense_inflation_pct=expense_inflation_pct,
        post_retirement_return_pct=post_retirement_return_pct,
    )
    db.session.add(goal)
    db.session.commit()
    flash('Goal added successfully!', 'success')
    return redirect(url_for('goals'))

@app.route('/goals/edit/<int:goal_id>', methods=['POST'])
@login_required
def edit_goal(goal_id):
    """Same fields/validation as add_goal(), but updates an existing
    goal in place — needed because glide-path and retirement-drawdown
    parameters are usually decided after seeing the goal's first
    projection, not at creation time. Deliberately doesn't touch
    last_reviewed_at (editing isn't reviewing — see review_goal())."""
    goal = Goal.query.get_or_404(goal_id)
    if goal.user_id != current_user.id:
        flash('Permission denied.', 'error')
        return redirect(url_for('goals'))

    name        = request.form.get('name', '').strip()
    emoji       = request.form.get('emoji', '').strip()
    target_amt  = safe_float(request.form.get('target_amt'))
    target_year = int(request.form.get('target_year', 2030))
    if not name or target_amt <= 0:
        flash('Please enter a goal name and target amount.', 'error')
        return redirect(url_for('goals'))
    if target_year <= 2024:
        flash('Target year must be in the future.', 'error')
        return redirect(url_for('goals'))

    is_retirement_goal = request.form.get('is_retirement_goal') == 'on'
    glide_start = safe_float(request.form.get('glide_start_equity_pct', '75'))
    glide_end   = safe_float(request.form.get('glide_end_equity_pct', '30'))

    goal.name = name
    goal.emoji = emoji
    goal.target_amt = target_amt
    goal.target_year = target_year
    goal.current_savings = safe_float(request.form.get('current_savings'))
    goal.monthly_sip = safe_float(request.form.get('monthly_sip'))
    annual_return = safe_float(request.form.get('annual_return', '12'))
    goal.annual_return = annual_return if annual_return > 0 else 12.0
    inflation_rate = safe_float(request.form.get('inflation_rate', '0'))
    goal.inflation_rate = inflation_rate if inflation_rate >= 0 else 0
    step_up_pct = safe_float(request.form.get('step_up_pct', '0'))
    goal.step_up_pct = step_up_pct if step_up_pct >= 0 else 0
    goal.glide_start_equity_pct = max(min(glide_start, 100), 0)
    goal.glide_end_equity_pct = max(min(glide_end, 100), 0)
    goal.is_retirement_goal = is_retirement_goal
    goal.retirement_age = request.form.get('retirement_age', type=int) if is_retirement_goal else None
    goal.life_expectancy = request.form.get('life_expectancy', type=int) or 85
    goal.monthly_expense_today = request.form.get('monthly_expense_today', type=float) if is_retirement_goal else None
    goal.expense_inflation_pct = safe_float(request.form.get('expense_inflation_pct', '6'))
    goal.post_retirement_return_pct = safe_float(request.form.get('post_retirement_return_pct', '7'))

    db.session.commit()
    flash(f'{goal.name} updated.', 'success')
    return redirect(url_for('goals'))


@app.route('/goals/delete/<int:goal_id>', methods=['POST'])
@login_required
def delete_goal(goal_id):
    goal = Goal.query.get_or_404(goal_id)
    if goal.user_id != current_user.id:
        flash('Permission denied.', 'error')
        return redirect(url_for('goals'))
    db.session.delete(goal)
    db.session.commit()
    flash('Goal deleted.', 'success')
    return redirect(url_for('goals'))

_HOLDING_OWNER_CHECK = {
    'mutual_fund':       lambda h, uid: h.user_id == uid,
    'stock':             lambda h, uid: h.user_id == uid,
    'wealth_asset':      lambda h, uid: h.user_id == uid,
    'retirement_scheme': lambda h, uid: h.user_id == uid,
}

# Sensible default asset_class per holding type, used when the link
# form doesn't override it (see goals.html's link-form asset_class
# select, which defaults to this same mapping client-side too).
_DEFAULT_ASSET_CLASS = {
    'mutual_fund': 'equity', 'stock': 'equity',
    'wealth_asset': 'debt', 'retirement_scheme': 'debt',
}


@app.route('/goals/<int:goal_id>/link', methods=['POST'])
@login_required
def link_goal_holding(goal_id):
    goal = Goal.query.get_or_404(goal_id)
    if goal.user_id != current_user.id:
        flash('Permission denied.', 'error')
        return redirect(url_for('goals'))

    # The link form's <select> submits a single "type:id" value (see
    # goals.html) so one dropdown can list all four holding types
    # together instead of needing four separate pickers.
    raw = request.form.get('holding', '')
    holding_type, _, holding_id_str = raw.partition(':')
    holding_id = int(holding_id_str) if holding_id_str.isdigit() else None
    allocation_pct = safe_float(request.form.get('allocation_pct', '100'))
    asset_class = request.form.get('asset_class') or _DEFAULT_ASSET_CLASS.get(holding_type, 'other')
    if asset_class not in ('equity', 'debt', 'other'):
        asset_class = 'other'

    if holding_type not in _HOLDING_OWNER_CHECK or holding_id is None:
        flash('Please select a valid holding.', 'error')
        return redirect(url_for('goals'))

    holding, raw_value, name = _holding_lookup(holding_type, holding_id)
    if not holding or not _HOLDING_OWNER_CHECK[holding_type](holding, current_user.id):
        flash('Please select a valid holding.', 'error')
        return redirect(url_for('goals'))
    if allocation_pct <= 0 or allocation_pct > 100:
        flash('Allocation must be between 1% and 100%.', 'error')
        return redirect(url_for('goals'))

    # A holding can't be linked to the same goal twice — there's no
    # "edit allocation" flow, so a repeat pick would just add a
    # second, separate link stacking more value on top of the first
    # rather than replacing it. Unlink and re-link instead.
    dup = GoalHoldingLink.query.filter_by(
        goal_id=goal.id, holding_type=holding_type, holding_id=holding_id).first()
    if dup:
        flash(f'{name} is already linked to {goal.name} — unlink it first if you want '
              f'to change the allocation.', 'error')
        return redirect(url_for('goals'))

    # A holding is one pool of real money: its allocation across ALL
    # of the user's goals can never add up to more than 100%, or the
    # same rupee gets counted toward two goals at once (Goals-page
    # audit, Sep 2026).
    already_pct = _holding_allocated_pct(holding_type, holding_id, current_user.id)
    remaining_pct = round(100 - already_pct, 2)
    if allocation_pct > remaining_pct + 1e-9:
        remaining_value = raw_value * (remaining_pct / 100)
        if remaining_pct <= 0:
            flash(f'{name} is already fully allocated to other goals — nothing left to link here.', 'error')
        else:
            flash(f'Only {remaining_pct:g}% of {name} (₹{remaining_value:,.0f}) is still '
                  f'unallocated — the rest is already linked to other goals.', 'error')
        return redirect(url_for('goals'))

    link = GoalHoldingLink(goal_id=goal.id, holding_type=holding_type,
                            holding_id=holding.id, allocation_pct=allocation_pct,
                            asset_class=asset_class)
    db.session.add(link)
    db.session.commit()
    flash(f'Linked {name} to {goal.name}.', 'success')
    return redirect(url_for('goals'))

@app.route('/goals/<int:goal_id>/unlink/<int:link_id>', methods=['POST'])
@login_required
def unlink_goal_holding(goal_id, link_id):
    goal = Goal.query.get_or_404(goal_id)
    if goal.user_id != current_user.id:
        flash('Permission denied.', 'error')
        return redirect(url_for('goals'))
    link = GoalHoldingLink.query.get_or_404(link_id)
    if link.goal_id != goal.id:
        flash('Permission denied.', 'error')
        return redirect(url_for('goals'))
    db.session.delete(link)
    db.session.commit()
    flash('Holding unlinked from goal.', 'success')
    return redirect(url_for('goals'))


@app.route('/goals/<int:goal_id>/review', methods=['POST'])
@login_required
def review_goal(goal_id):
    """Goal review nudge (see GOAL_REVIEW_PERIOD_DAYS in goals()) —
    just stamps last_reviewed_at. Deliberately doesn't change any
    other field: 'reviewing' a goal means the user looked at its
    current numbers and confirmed they're still fine, not that
    anything about the goal itself changed."""
    goal = Goal.query.get_or_404(goal_id)
    if goal.user_id != current_user.id:
        flash('Permission denied.', 'error')
        return redirect(url_for('goals'))
    goal.last_reviewed_at = dt.utcnow()
    db.session.commit()
    flash(f'{goal.name} marked as reviewed.', 'success')
    return redirect(url_for('goals'))


@app.route('/goals/<int:goal_id>/archive', methods=['POST'])
@login_required
def archive_goal(goal_id):
    """Archive a goal as either 'achieved' or 'dropped' — same
    Archive -> Restore lifecycle used across Insurance/Retirement
    Centre/Wealth (never a straight delete). reason comes from the
    form so one route covers both the "Mark as Achieved" and "Drop
    this goal" actions in goals.html."""
    goal = Goal.query.get_or_404(goal_id)
    if goal.user_id != current_user.id:
        flash('Permission denied.', 'error')
        return redirect(url_for('goals'))
    reason = request.form.get('reason', '').strip().lower()
    if reason not in ('achieved', 'dropped'):
        flash('Invalid archive reason.', 'error')
        return redirect(url_for('goals'))
    if goal.is_archived:
        flash(f'{goal.name} is already archived.', 'error')
        return redirect(url_for('goals'))

    goal.is_archived = True
    goal.archived_at = dt.utcnow()
    goal.archive_reason = reason
    db.session.commit()
    if reason == 'achieved':
        flash(f'🎉 {goal.name} marked as achieved and archived!', 'success')
    else:
        flash(f'{goal.name} archived as dropped.', 'success')
    return redirect(url_for('goals'))


@app.route('/goals/<int:goal_id>/restore', methods=['POST'])
@login_required
def restore_goal(goal_id):
    goal = Goal.query.get_or_404(goal_id)
    if goal.user_id != current_user.id:
        flash('Permission denied.', 'error')
        return redirect(url_for('goals'))
    if not goal.is_archived:
        flash(f'{goal.name} is not archived.', 'error')
        return redirect(url_for('goals'))

    goal.is_archived = False
    goal.archived_at = None
    goal.archive_reason = None
    db.session.commit()
    flash(f'{goal.name} restored to active goals.', 'success')
    return redirect(request.form.get('next') or url_for('goals'))


@app.route('/goals/archived')
@login_required
def archived_goals_view():
    """Dedicated Archived Goals page — its own page rather than a
    collapsible section at the bottom of /goals, matching the
    Insurance/Retirement Centre pattern (a separate Archive listing
    with square info cards, not an inline accordion)."""
    archived_goals = (Goal.query
        .filter_by(user_id=current_user.id, is_archived=True)
        .order_by(Goal.archived_at.desc()).all())
    return render_template('goals_archive.html', archived_goals=archived_goals)


# ── Step 7: Export routes (PDF + Excel) ──────────────────────────────────────
from flask import send_file
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, HRFlowable, KeepTogether)
import openpyxl
from openpyxl.styles import (Font, PatternFill, Alignment, Border, Side)
from openpyxl.utils import get_column_letter
import io as _io
from datetime import datetime as _dt

# ── shared colours ────────────────────────────────────────────────────────────
_TEAL   = colors.HexColor('#00d4aa')
_DARK   = colors.HexColor('#0d0f14')
_CARD   = colors.HexColor('#13161f')
_BORDER = colors.HexColor('#1e2130')
_MUTED  = colors.HexColor('#64748b')
_WHITE  = colors.white
_RED    = colors.HexColor('#ef4444')
_GREEN  = colors.HexColor('#22c55e')
_AMBER  = colors.HexColor('#f59e0b')

# ── PDF helpers ───────────────────────────────────────────────────────────────
def _fmt(n):
    return f"₹{n:,.0f}"

def _pct(n):
    return f"{n:.1f}%"

def _pdf_styles():
    base = getSampleStyleSheet()
    def S(name, **kw):
        return ParagraphStyle(name, **kw)
    return {
        'title':     S('title',   fontSize=22, textColor=_TEAL,  leading=28, fontName='Helvetica-Bold'),
        'subtitle':  S('sub',     fontSize=9,  textColor=_MUTED, leading=14, fontName='Helvetica'),
        'h2':        S('h2',      fontSize=13, textColor=_WHITE, leading=18, fontName='Helvetica-Bold', spaceAfter=4),
        'h3':        S('h3',      fontSize=10, textColor=_TEAL,  leading=14, fontName='Helvetica-Bold', spaceAfter=2),
        'body':      S('body',    fontSize=9,  textColor=_WHITE, leading=13, fontName='Helvetica'),
        'muted':     S('muted',   fontSize=8,  textColor=_MUTED, leading=12, fontName='Helvetica'),
        'right':     S('right',   fontSize=9,  textColor=_WHITE, leading=13, fontName='Helvetica', alignment=TA_RIGHT),
        'teal_right':S('tr',      fontSize=9,  textColor=_TEAL,  leading=13, fontName='Helvetica-Bold', alignment=TA_RIGHT),
        'green':     S('green',   fontSize=9,  textColor=_GREEN, leading=13, fontName='Helvetica-Bold', alignment=TA_RIGHT),
        'red':       S('red',     fontSize=9,  textColor=_RED,   leading=13, fontName='Helvetica-Bold', alignment=TA_RIGHT),
        'amber':     S('amber',   fontSize=9,  textColor=_AMBER, leading=13, fontName='Helvetica-Bold', alignment=TA_RIGHT),
    }

def _tbl_style(header_bg=None, row_colors=True):
    hbg = header_bg or _CARD
    cmds = [
        ('BACKGROUND',   (0, 0), (-1, 0),  hbg),
        ('TEXTCOLOR',    (0, 0), (-1, 0),  _TEAL),
        ('FONTNAME',     (0, 0), (-1, 0),  'Helvetica-Bold'),
        ('FONTSIZE',     (0, 0), (-1, 0),  8),
        ('FONTNAME',     (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE',     (0, 1), (-1, -1), 8),
        ('TEXTCOLOR',    (0, 1), (-1, -1), _WHITE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1),
            [colors.HexColor('#13161f'), colors.HexColor('#1a1e2d')] if row_colors else [_CARD]),
        ('GRID',         (0, 0), (-1, -1), 0.3, _BORDER),
        ('LEFTPADDING',  (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING',   (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING',(0, 0), (-1, -1), 5),
        ('VALIGN',       (0, 0), (-1, -1), 'MIDDLE'),
    ]
    return TableStyle(cmds)

def _section_header(text, styles):
    return [
        Spacer(1, 6*mm),
        HRFlowable(width='100%', thickness=0.5, color=_TEAL, spaceAfter=3),
        Paragraph(text, styles['h2']),
        Spacer(1, 2*mm),
    ]

def _sip_projection(target_amt, target_year, current_savings, monthly_sip, annual_return, step_up_pct=0):
    """Return list of (month_label, balance, kind) for 12 months + yearly to
    target. step_up_pct steps the SIP up once every 12 ELAPSED months
    (not calendar-year aligned), matching _stepup_sip_future_value() /
    calculate_goal() so this table's numbers agree with the goal's own
    headline Projected Corpus figure instead of quietly assuming a flat
    SIP. Each row is simulated fresh from month 0 (cheap at these
    horizons) rather than compounding incrementally, so a mid-run
    change in the step count can never drift from calculate_goal()'s math."""
    def _simulate(months):
        r = annual_return / 100 / 12
        step_up = (step_up_pct or 0) / 100
        balance = float(current_savings or 0)
        sip = float(monthly_sip or 0)
        for m in range(months):
            if m > 0 and m % 12 == 0:
                sip *= (1 + step_up)
            # Annuity-DUE, matching _stepup_sip_future_value()'s own
            # "* (1 + r)" tail factor: this month's SIP is added BEFORE
            # that month's growth is applied, not after — get this
            # backwards and the table silently drifts ~1% off the
            # goal's own headline Projected Corpus over a few years.
            if r > 0:
                balance = (balance + sip) * (1 + r)
            else:
                balance += sip
        return balance

    rows = []
    now = _dt.now()
    cur_year = now.year
    cur_month = now.month

    # Monthly for first 12 months
    for i in range(1, 13):
        m = (cur_month + i - 1) % 12 + 1
        y = cur_year + (cur_month + i - 1) // 12
        rows.append((f"{_dt(y, m, 1).strftime('%b %Y')}", _simulate(i), "monthly"))

    # Yearly milestones after month 12. Deliberately (yr - cur_year) * 12,
    # NOT calendar-aligned to each December's real month count — this must
    # match calculate_goal()'s own `months = (target_year - current_year) * 12`
    # exactly, or this table's final row silently disagrees with the goal's
    # headline Projected Corpus figure (the bug this docstring is fixing).
    for yr in range(cur_year + 1, target_year + 1):
        months_to_yr = (yr - cur_year) * 12
        if months_to_yr <= 12:
            continue
        rows.append((f"Dec {yr}", _simulate(months_to_yr), "yearly"))

    return rows

# ── PDF EXPORT ────────────────────────────────────────────────────────────────
@app.route('/export/pdf')
@login_required
def export_pdf():
    # ── gather data ──
    # Phase H: asset figures now come from WealthAsset (via
    # WealthStatisticsService), the authoritative Wealth source,
    # instead of the retired legacy Asset model.
    wstats  = WealthStatisticsService(current_user.id)
    assets_by_cat = wstats.assets_by_category()
    mfs     = MutualFund.query.filter_by(user_id=current_user.id).all()
    stocks  = Stock.query.filter_by(user_id=current_user.id).all()
    goals   = Goal.query.filter_by(user_id=current_user.id).order_by(Goal.target_year).all()

    equity_val     = sum(m.value for m in mfs) + sum(s.value for s in stocks)
    debt_val       = (sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.BANK_DEPOSITS, []))
                       + sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.INVESTMENTS, [])))
    gold_val       = sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.PRECIOUS_METALS, []))
    realestate_val = sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.REAL_ESTATE, []))
    other_val      = (sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.VEHICLES, []))
                       + sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.BUSINESS, []))
                       + sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.OTHER, [])))
    cash_val       = 0
    total          = equity_val + debt_val + gold_val + realestate_val + cash_val + other_val

    # ── build PDF ──
    buf = _io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
        title="MyWealthLens — Wealth Report",
    )
    W = doc.width
    S = _pdf_styles()
    story = []

    # ── COVER HEADER ──
    now_str = _dt.now().strftime("%d %B %Y, %I:%M %p")
    story += [
        Paragraph("MyWealthLens", S['title']),
        Paragraph("Personal Wealth Report", S['h2']),
        Paragraph(f"Generated on {now_str}", S['muted']),
        Spacer(1, 4*mm),
    ]

    # ── USER PROFILE CARD ──
    story += _section_header("👤  User Profile", S)
    profile_data = [
        ['Name', current_user.name, 'Email', current_user.email],
    ]

    pt = Table(profile_data, colWidths=[W*0.18, W*0.32, W*0.18, W*0.32])
    pt.setStyle(TableStyle([
        ('BACKGROUND',  (0,0), (-1,-1), _CARD),
        ('TEXTCOLOR',   (0,0), (-1,-1), _WHITE),
        ('TEXTCOLOR',   (0,0), (0,-1),  _MUTED),
        ('TEXTCOLOR',   (2,0), (2,-1),  _MUTED),
        ('FONTNAME',    (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE',    (0,0), (-1,-1), 9),
        ('FONTNAME',    (1,0), (1,-1),  'Helvetica-Bold'),
        ('FONTNAME',    (3,0), (3,-1),  'Helvetica-Bold'),
        ('GRID',        (0,0), (-1,-1), 0.3, _BORDER),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('TOPPADDING',  (0,0), (-1,-1), 6),
        ('BOTTOMPADDING',(0,0),(-1,-1), 6),
    ]))
    story += [pt, Spacer(1, 4*mm)]

    # ── NET WORTH SUMMARY ──
    story += _section_header("💰  Net Worth Summary", S)
    story.append(Paragraph(f"Total Net Worth: {_fmt(total)}", ParagraphStyle(
        'big', fontSize=16, textColor=_TEAL, fontName='Helvetica-Bold', spaceAfter=6)))

    def pct(v): return round(v/total*100, 1) if total else 0

    summary_data = [
        ['Asset Class', 'Value (₹)', 'Allocation %'],
        ['📊 Equity (MF + Stocks)', _fmt(equity_val), _pct(pct(equity_val))],
        ['🏛️ Debt (PPF/VPF/SSY/FD)', _fmt(debt_val),  _pct(pct(debt_val))],
        ['🥇 Gold / Silver',          _fmt(gold_val),  _pct(pct(gold_val))],
        ['🏠 Real Estate',             _fmt(realestate_val), _pct(pct(realestate_val))],
        ['💵 Cash & Others',           _fmt(cash_val + other_val), _pct(pct(cash_val + other_val))],
        ['TOTAL', _fmt(total), '100%'],
    ]
    st = Table(summary_data, colWidths=[W*0.52, W*0.26, W*0.22])
    st.setStyle(_tbl_style())
    st.setStyle(TableStyle([
        ('BACKGROUND', (0,-1), (-1,-1), _TEAL),
        ('TEXTCOLOR',  (0,-1), (-1,-1), _DARK),
        ('FONTNAME',   (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('ALIGN',      (1,0),  (-1,-1), 'RIGHT'),
    ]))
    story += [st, Spacer(1, 2*mm)]

    # ── ASSET DETAIL TABLES ──
    story += _section_header("📋  Asset Details", S)

    # Mutual Funds
    if mfs:
        story.append(Paragraph("Mutual Funds", S['h3']))
        mf_data = [['Scheme Name', 'Folio', 'Units', 'NAV (₹)', 'Value (₹)']]
        for m in mfs:
            mf_data.append([
                m.scheme or '—',
                getattr(m, 'folio', '—') or '—',
                f"{getattr(m, 'units', 0) or 0:,.3f}",
                f"{getattr(m, 'nav', 0) or 0:,.2f}",
                _fmt(m.value),
            ])
        mt = Table(mf_data, colWidths=[W*0.42, W*0.16, W*0.12, W*0.14, W*0.16])
        mt.setStyle(_tbl_style())
        story += [mt, Spacer(1, 3*mm)]

    # Stocks
    if stocks:
        story.append(Paragraph("Stocks / Demat Holdings", S['h3']))
        sk_data = [['Company / ISIN', 'Quantity', 'Price (₹)', 'Value (₹)']]
        for s in stocks:
            sk_data.append([
                s.name or getattr(s, 'isin', '—') or '—',
                f"{getattr(s, 'quantity', 0) or 0:,.0f}",
                f"{(s.live_price or s.buy_price or 0):,.2f}",
                _fmt(s.value),
            ])
        skt = Table(sk_data, colWidths=[W*0.46, W*0.16, W*0.18, W*0.20])
        skt.setStyle(_tbl_style())
        story += [skt, Spacer(1, 3*mm)]

    # Physical / other assets — itemized by Wealth Asset category
    cat_icons = {
        WealthAssetCategory.REAL_ESTATE:     '🏠',
        WealthAssetCategory.PRECIOUS_METALS: '🥇',
        WealthAssetCategory.VEHICLES:        '🚗',
        WealthAssetCategory.BANK_DEPOSITS:   '🏦',
        WealthAssetCategory.INVESTMENTS:     '📈',
        WealthAssetCategory.BUSINESS:        '🏢',
        WealthAssetCategory.OTHER:           '📦',
    }

    for cat in WealthAssetCategory.ALL:
        items = assets_by_cat.get(cat, [])
        if not items:
            continue
        story.append(Paragraph(f"{cat_icons.get(cat, '📦')} {cat}", S['h3']))
        ph_data = [['Name', 'Value (₹)']]
        for a in items:
            ph_data.append([a.name or cat, _fmt(a.current_value)])
        ph_data.append(['Subtotal', _fmt(sum(a.current_value for a in items))])
        pht = Table(ph_data, colWidths=[W*0.70, W*0.30])
        pht.setStyle(_tbl_style())
        pht.setStyle(TableStyle([
            ('BACKGROUND', (0,-1), (-1,-1), colors.HexColor('#1a1e2d')),
            ('FONTNAME',   (0,-1), (-1,-1), 'Helvetica-Bold'),
            ('ALIGN',      (1,0),  (1,-1),  'RIGHT'),
        ]))
        story += [pht, Spacer(1, 3*mm)]

    # ── GOALS + SIP PROJECTIONS ──
    if goals:
        story += _section_header("🎯  Financial Goals & SIP Projections", S)
        for g in goals:
            linked_value, _ = _goal_linked_value(g)
            effective_current = (g.current_savings or 0) + linked_value
            calc = calculate_goal(g.target_amt, g.target_year, effective_current,
                                  g.monthly_sip, g.annual_return,
                                  getattr(g, 'inflation_rate', 0) or 0,
                                  getattr(g, 'step_up_pct', 0) or 0)
            status_color = _GREEN if calc['on_track'] else _RED
            story.append(KeepTogether([
                Paragraph(f"{g.emoji or '⭐'} {g.name}", S['h3']),
                Spacer(1, 1*mm),
            ]))
            g_meta = [
                ['Target Amount', _fmt(g.target_amt), 'Target Year', str(g.target_year)],
                ['Current Savings', _fmt(g.current_savings or 0),
                 'Monthly SIP', _fmt(g.monthly_sip or 0)],
                ['Annual Return', f"{g.annual_return}% p.a.",
                 'Projected Corpus', _fmt(calc['projected'])],
                ['Years Left', str(calc['years_left']),
                 'Status',
                 'On Track ✓' if calc['on_track'] else f"Shortfall ₹{calc.get('shortfall',0):,.0f}"],
            ]
            if linked_value > 0 or calc.get('inflation_applied'):
                extra_row = [
                    'From Linked Holdings', _fmt(linked_value) if linked_value > 0 else '—',
                    'Target (Inflation-Adj.)',
                    _fmt(calc['inflation_adjusted_target']) if calc.get('inflation_applied') else '—',
                ]
                g_meta.append(extra_row)
            gmt = Table(g_meta, colWidths=[W*0.22, W*0.28, W*0.22, W*0.28])
            gmt.setStyle(TableStyle([
                ('BACKGROUND',   (0,0), (-1,-1), _CARD),
                ('TEXTCOLOR',    (0,0), (-1,-1), _WHITE),
                ('TEXTCOLOR',    (0,0), (0,-1),  _MUTED),
                ('TEXTCOLOR',    (2,0), (2,-1),  _MUTED),
                ('FONTNAME',     (1,0), (1,-1),  'Helvetica-Bold'),
                ('FONTNAME',     (3,0), (3,-1),  'Helvetica-Bold'),
                ('TEXTCOLOR',    (3,3), (3,3),   status_color),
                ('FONTSIZE',     (0,0), (-1,-1),  8),
                ('GRID',         (0,0), (-1,-1),  0.3, _BORDER),
                ('LEFTPADDING',  (0,0), (-1,-1),  6),
                ('TOPPADDING',   (0,0), (-1,-1),  5),
                ('BOTTOMPADDING',(0,0), (-1,-1),  5),
            ]))
            story += [gmt, Spacer(1, 2*mm)]

            # SIP projection table — same step-up SIP and inflation-adjusted
            # target as the headline stat box above, so the two never disagree
            compare_target = calc['inflation_adjusted_target'] if calc.get('inflation_applied') else g.target_amt
            proj = _sip_projection(g.target_amt, g.target_year, effective_current,
                                   g.monthly_sip, g.annual_return, getattr(g, 'step_up_pct', 0) or 0)
            if proj:
                story.append(Paragraph("SIP Growth Projection", S['h3']))
                sip_hdr = [['Period', 'Projected Balance (₹)', 'vs Target (₹)', 'Progress %']]
                sip_rows = []
                for (label, bal, kind) in proj:
                    delta = bal - compare_target
                    progress = min(round(bal / compare_target * 100, 1), 100) if compare_target else 0
                    sip_rows.append([
                        label,
                        f"{bal:,.0f}",
                        f"{'+' if delta >= 0 else ''}{delta:,.0f}",
                        f"{progress}%",
                    ])
                sip_data = sip_hdr + sip_rows
                sipt = Table(sip_data, colWidths=[W*0.22, W*0.28, W*0.28, W*0.22])
                sipt.setStyle(_tbl_style())
                sipt.setStyle(TableStyle([('ALIGN',(1,0),(-1,-1),'RIGHT')]))
                story += [sipt, Spacer(1, 5*mm)]

    # ── FOOTER ──
    story += [
        HRFlowable(width='100%', thickness=0.5, color=_BORDER),
        Spacer(1, 2*mm),
        Paragraph("Generated by MyWealthLens · For personal use only · Not investment advice",
                  ParagraphStyle('footer', fontSize=7, textColor=_MUTED,
                                 fontName='Helvetica', alignment=TA_CENTER)),
    ]

    # ── page background ──
    def _dark_bg(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(_DARK)
        canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
        canvas.restoreState()

    doc.build(story, onFirstPage=_dark_bg, onLaterPages=_dark_bg)
    buf.seek(0)
    fname = f"MyWealthLens_{current_user.name.replace(' ','_')}_{_dt.now().strftime('%Y%m%d')}.pdf"
    return send_file(buf, mimetype='application/pdf',
                     as_attachment=True, download_name=fname)


# ── EXCEL EXPORT ──────────────────────────────────────────────────────────────
@app.route('/export/excel')
@login_required
def export_excel():
    # Phase H: sourced from WealthAsset (authoritative), not the
    # retired legacy Asset model.
    wstats  = WealthStatisticsService(current_user.id)
    assets_by_cat = wstats.assets_by_category()
    all_assets_flat = [a for items in assets_by_cat.values() for a in items]
    mfs     = MutualFund.query.filter_by(user_id=current_user.id).all()
    stocks  = Stock.query.filter_by(user_id=current_user.id).all()
    goals   = Goal.query.filter_by(user_id=current_user.id).order_by(Goal.target_year).all()

    equity_val     = sum(m.value for m in mfs) + sum(s.value for s in stocks)
    debt_val       = (sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.BANK_DEPOSITS, []))
                       + sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.INVESTMENTS, [])))
    gold_val       = sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.PRECIOUS_METALS, []))
    realestate_val = sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.REAL_ESTATE, []))
    other_val      = (sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.VEHICLES, []))
                       + sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.BUSINESS, []))
                       + sum(a.current_value for a in assets_by_cat.get(WealthAssetCategory.OTHER, [])))
    cash_val       = 0
    total          = equity_val + debt_val + gold_val + realestate_val + cash_val + other_val

    wb = openpyxl.Workbook()

    # ── colour helpers ──
    TEAL_HEX  = '00D4AA'
    DARK_HEX  = '0D0F14'
    CARD_HEX  = '13161F'
    CARD2_HEX = '1A1E2D'
    BORD_HEX  = '1E2130'
    WHITE_HEX = 'E2E8F0'
    MUTED_HEX = '64748B'
    GREEN_HEX = '22C55E'
    RED_HEX   = 'EF4444'

    def _fill(hex_): return PatternFill('solid', fgColor=hex_)
    def _font(hex_=WHITE_HEX, bold=False, sz=10):
        return Font(color=hex_, bold=bold, size=sz, name='Calibri')
    def _border():
        s = Side(style='thin', color=BORD_HEX)
        return Border(left=s, right=s, top=s, bottom=s)
    def _align(h='left', v='center', wrap=False):
        return Alignment(horizontal=h, vertical=v, wrap_text=wrap)

    def _hdr_row(ws, row_num, values, col_start=1):
        for i, v in enumerate(values):
            c = ws.cell(row=row_num, column=col_start+i, value=v)
            c.fill = _fill(CARD_HEX)
            c.font = _font(TEAL_HEX, bold=True, sz=9)
            c.border = _border()
            c.alignment = _align('center')

    def _data_row(ws, row_num, values, col_start=1, alt=False):
        bg = CARD2_HEX if alt else CARD_HEX
        for i, v in enumerate(values):
            c = ws.cell(row=row_num, column=col_start+i, value=v)
            c.fill = _fill(bg)
            c.font = _font(sz=9)
            c.border = _border()
            c.alignment = _align()

    def _title_cell(ws, row_num, col, text, sz=14):
        c = ws.cell(row=row_num, column=col, value=text)
        c.font = Font(color=TEAL_HEX, bold=True, size=sz, name='Calibri')
        c.fill = _fill(DARK_HEX)
        c.alignment = _align()

    def _set_col_widths(ws, widths):
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    def _freeze(ws, cell='A2'):
        ws.freeze_panes = cell

    def _tab_color(ws, hex_):
        ws.sheet_properties.tabColor = hex_

    # ════════════════════════════════════════════════
    # SHEET 1 — Summary
    # ════════════════════════════════════════════════
    ws1 = wb.active
    ws1.title = "Summary"
    _tab_color(ws1, TEAL_HEX)
    ws1.sheet_view.showGridLines = False
    for row in ws1.iter_rows(min_row=1, max_row=60, min_col=1, max_col=8):
        for cell in row:
            cell.fill = _fill(DARK_HEX)

    _title_cell(ws1, 1, 1, "MyWealthLens — Wealth Report", sz=16)
    ws1.cell(row=2, column=1, value=f"Generated: {_dt.now().strftime('%d %B %Y, %I:%M %p')}").font = _font(MUTED_HEX, sz=9)

    r = 4
    _title_cell(ws1, r, 1, "USER PROFILE", sz=11)
    r += 1
    _hdr_row(ws1, r, ['Field', 'Value'])
    r += 1
    profile_rows = [
        ('Name', current_user.name),
        ('Email', current_user.email),
    ]
    for i, (k, v) in enumerate(profile_rows):
        _data_row(ws1, r, [k, v], alt=i%2==1)
        r += 1

    r += 1
    _title_cell(ws1, r, 1, "NET WORTH SUMMARY", sz=11)
    r += 1
    _hdr_row(ws1, r, ['Asset Class', 'Value (₹)', 'Allocation %'])
    r += 1
    summary_rows = [
        ('Equity (MF + Stocks)', equity_val),
        ('Debt (PPF/VPF/SSY/FD)', debt_val),
        ('Gold / Silver', gold_val),
        ('Real Estate', realestate_val),
        ('Cash & Others', cash_val + other_val),
    ]
    for i, (lbl, val) in enumerate(summary_rows):
        pct_val = round(val/total*100, 1) if total else 0
        _data_row(ws1, r, [lbl, val, f"{pct_val}%"], alt=i%2==1)
        ws1.cell(row=r, column=2).number_format = '₹#,##0'
        r += 1
    # Total row
    for col, val in enumerate([('TOTAL', total, '100%')], 1):
        pass
    _hdr_row(ws1, r, ['TOTAL', total, '100%'])
    ws1.cell(row=r, column=2).number_format = '₹#,##0'

    _set_col_widths(ws1, [28, 20, 15])
    _freeze(ws1, 'A5')

    # ════════════════════════════════════════════════
    # SHEET 2 — All Assets
    # ════════════════════════════════════════════════
    ws2 = wb.create_sheet("All Assets")
    _tab_color(ws2, '6366F1')
    ws2.sheet_view.showGridLines = False
    for row in ws2.iter_rows(min_row=1, max_row=500, min_col=1, max_col=8):
        for cell in row:
            cell.fill = _fill(DARK_HEX)

    _title_cell(ws2, 1, 1, "All Assets", sz=14)
    r2 = 3
    _hdr_row(ws2, r2, ['Category', 'Name / Scheme', 'Sub-type', 'Units / Qty', 'Price / NAV (₹)', 'Value (₹)'])
    r2 += 1

    all_rows = []
    for m in mfs:
        all_rows.append(['Mutual Fund', m.scheme or '—',
                         getattr(m, 'amc', '') or '',
                         getattr(m, 'units', '') or '', getattr(m, 'nav', '') or '', m.value])
    for s in stocks:
        all_rows.append(['Stock', s.name or '—', getattr(s, 'isin', '') or '',
                         getattr(s, 'quantity', '') or '', (s.live_price or s.buy_price or ''), s.value])
    for a in all_assets_flat:
        all_rows.append([a.category, a.name or '—', a.asset_type or '', '', '', a.current_value])

    for i, row_data in enumerate(all_rows):
        _data_row(ws2, r2, row_data, alt=i%2==1)
        ws2.cell(row=r2, column=6).number_format = '₹#,##0'
        r2 += 1

    _set_col_widths(ws2, [16, 40, 20, 12, 15, 16])
    _freeze(ws2, 'A4')

    # ════════════════════════════════════════════════
    # SHEET 3 — Goals + SIP Projections
    # ════════════════════════════════════════════════
    ws4 = wb.create_sheet("Goals & Projections")
    _tab_color(ws4, 'F59E0B')
    ws4.sheet_view.showGridLines = False
    for row in ws4.iter_rows(min_row=1, max_row=1000, min_col=1, max_col=8):
        for cell in row:
            cell.fill = _fill(DARK_HEX)

    _title_cell(ws4, 1, 1, "Goals & SIP Projections", sz=14)
    r4 = 3

    if goals:
        for g in goals:
            linked_value, _ = _goal_linked_value(g)
            effective_current = (g.current_savings or 0) + linked_value
            calc = calculate_goal(g.target_amt, g.target_year, effective_current,
                                  g.monthly_sip, g.annual_return,
                                  getattr(g, 'inflation_rate', 0) or 0,
                                  getattr(g, 'step_up_pct', 0) or 0)
            _title_cell(ws4, r4, 1, f"{g.emoji or '⭐'} {g.name}", sz=12)
            r4 += 1
            _hdr_row(ws4, r4, ['Target (₹)', 'Target Year', 'Savings (₹)',
                                'SIP/mo (₹)', 'Return %', 'Projected (₹)', 'Status'])
            r4 += 1
            status = 'On Track ✓' if calc['on_track'] else f"Shortfall ₹{calc.get('shortfall',0):,.0f}"
            _data_row(ws4, r4, [g.target_amt, g.target_year, g.current_savings or 0,
                                 g.monthly_sip or 0, f"{g.annual_return}%",
                                 round(calc['projected']), status])
            for col in [1, 3, 4, 6]:
                ws4.cell(row=r4, column=col).number_format = '₹#,##0'
            ws4.cell(row=r4, column=7).font = \
                _font(GREEN_HEX if calc['on_track'] else RED_HEX, bold=True, sz=9)
            r4 += 2

            # SIP projection
            ws4.cell(row=r4, column=1, value="SIP Growth Projection").font = _font(TEAL_HEX, bold=True, sz=10)
            r4 += 1
            _hdr_row(ws4, r4, ['Period', 'Projected Balance (₹)', 'vs Target (₹)', 'Progress %'])
            r4 += 1

            compare_target = calc['inflation_adjusted_target'] if calc.get('inflation_applied') else g.target_amt
            proj = _sip_projection(g.target_amt, g.target_year, effective_current,
                                   g.monthly_sip, g.annual_return, getattr(g, 'step_up_pct', 0) or 0)
            for i, (label, bal, _) in enumerate(proj):
                delta = bal - compare_target
                progress = min(round(bal / compare_target * 100, 1), 100) if compare_target else 0
                _data_row(ws4, r4, [label, round(bal), round(delta), f"{progress}%"], alt=i%2==1)
                ws4.cell(row=r4, column=2).number_format = '₹#,##0'
                ws4.cell(row=r4, column=3).number_format = '₹#,##0'
                delta_color = GREEN_HEX if delta >= 0 else RED_HEX
                ws4.cell(row=r4, column=3).font = _font(delta_color, sz=9)
                r4 += 1
            r4 += 2
    else:
        ws4.cell(row=r4, column=1, value="No goals configured.").font = _font(MUTED_HEX)

    _set_col_widths(ws4, [16, 20, 18, 16])
    _freeze(ws4, 'A3')

    # ── save + send ──
    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"MyWealthLens_{current_user.name.replace(' ','_')}_{_dt.now().strftime('%Y%m%d')}.xlsx"
    return send_file(buf,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=fname)



@app.errorhandler(429)
def rate_limit_exceeded(e):
    if request.method == 'GET':
        return render_template('login.html',
            error='Too many login attempts. Please wait 15 minutes.'), 429
    flash('Too many attempts. Please wait 15 minutes before trying again.', 'error')
    return redirect(url_for('login')), 429

@app.route('/account/change-password', methods=['POST'])
@login_required
def change_password():
    current_pw = request.form.get('current_password', '')
    new_pw     = request.form.get('new_password', '')
    confirm_pw = request.form.get('confirm_password', '')
    if not bcrypt.checkpw(current_pw.encode('utf-8'), current_user.password.encode('utf-8')):
        flash('Current password is incorrect.', 'error')
        return redirect(url_for('preferences'))
    if len(new_pw) < 8:
        flash('New password must be at least 8 characters.', 'error')
        return redirect(url_for('preferences'))
    if new_pw != confirm_pw:
        flash('New passwords do not match.', 'error')
        return redirect(url_for('preferences'))
    hashed = bcrypt.hashpw(new_pw.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    current_user.password = hashed
    db.session.commit()
    flash('Password changed successfully!', 'success')
    return redirect(url_for('preferences'))


@app.route('/account/encryption/setup', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def encryption_setup():
    """
    Records a user's Document Vault encryption setup. This endpoint
    NEVER receives the passphrase itself — only the salt (random,
    not secret) and a "verifier" (a known constant encrypted with the
    key derived from the passphrase, also not secret on its own) that
    were both computed entirely client-side in static/js/mwl-crypto.js.
    That's what makes "the server never sees the key" a checkable
    fact rather than a promise: this function has no way to derive
    the key even if it wanted to, since it never receives the
    passphrase.

    Deliberately one-shot: if a user already has encryption set up,
    this refuses rather than silently overwriting the salt — doing so
    would orphan every already-encrypted document (they were encrypted
    under the OLD salt's key; a new salt derives a different key that
    can't decrypt them). Changing/resetting the passphrase safely
    (re-encrypting existing documents under a new key) isn't built
    yet — that's tracked as a known follow-up, not silently ignored.
    """
    if current_user.encryption_salt:
        return jsonify(error="Encryption is already set up for your account. "
                              "Changing your passphrase isn't supported yet — "
                              "contact support before doing anything that assumes it is."), 409

    data = request.get_json(silent=True) or {}
    salt         = (data.get('salt') or '').strip()
    verifier     = (data.get('verifier') or '').strip()
    verifier_iv  = (data.get('verifier_iv') or '').strip()

    if not salt or not verifier or not verifier_iv:
        return jsonify(error="Missing encryption setup data."), 400
    if len(salt) > 64 or len(verifier_iv) > 64:
        return jsonify(error="Invalid encryption setup data."), 400

    current_user.encryption_salt        = salt
    current_user.encryption_verifier    = verifier
    current_user.encryption_verifier_iv = verifier_iv
    current_user.encryption_enabled_at  = dt.utcnow()
    db.session.commit()
    return jsonify(ok=True)


@app.route('/account/encryption/status')
@login_required
def encryption_status():
    """
    Non-secret setup data a page needs to unlock the current session
    (derive the key client-side and confirm it's correct via the
    verifier) — salt/verifier/verifier_iv are all safe to expose to
    their own owner exactly as safe as they are to store server-side.
    """
    return jsonify(
        enabled=bool(current_user.encryption_salt),
        salt=current_user.encryption_salt,
        verifier=current_user.encryption_verifier,
        verifier_iv=current_user.encryption_verifier_iv,
    )

app.register_blueprint(insurance_bp)
app.register_blueprint(retirement_bp)
app.register_blueprint(wealth_bp)
app.register_blueprint(family_bp)
app.register_blueprint(backup_bp)
app.register_blueprint(cashflow_bp)

# Phase I — Automatic Wealth Snapshots. Registers `flask wealth
# snapshot`, invoked by Windows Task Scheduler (see the Phase I
# final report for setup). Kept as a separate module rather than
# defined inline here, matching this project's existing pattern of
# routes.py/services.py living inside each module's own folder.
from wealth.cli import register_cli
register_cli(app)

# Backup — `flask backup run`, invoked by Windows Task Scheduler
# every few minutes. Same pattern as Wealth's CLI registration above.
from backup.cli import register_cli as register_backup_cli
register_backup_cli(app)

if __name__ == '__main__':
    import os
    # Render requires 0.0.0.0. Local development still works fine with this.
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)