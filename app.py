from flask import Flask, render_template, redirect, url_for, request, flash, session, jsonify
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
import bcrypt, pdfplumber, io, re, os, secrets, csv, hashlib, yfinance as yf
import casparser
from datetime import datetime as dt, timedelta, date as _date_cls
from models import (db, User, MutualFund, MutualFundTransaction, Stock, StockTransaction,
                     Goal, GoalHoldingLink, NetWorthHistory, BackupCode, UserSession)
from cas_xirr import scheme_xirr as _scheme_xirr, portfolio_xirr as _portfolio_xirr
from mail import send_password_reset_email
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
import session_manager
import twofa


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


def _resolve_database_uri():
    """
    Postgres/Alembic migration (Sep 2026): DATABASE_URL, when set,
    points this app at Postgres instead of the local SQLite file —
    the first step of the agreed production-readiness plan (Postgres
    + Alembic first, since encryption-aware schema changes are much
    safer to design and roll out with real migrations than with the
    plain "check state, create_all()" scripts SQLite got by with).

    Unset (the default on Mohan's own machine today), this resolves
    to the exact same sqlite:///mywealthlens.db as before — nothing
    changes for him until he deliberately sets DATABASE_URL himself.

    Some hosts (Heroku and a few others) still hand out a
    "postgres://" URL; SQLAlchemy 1.4+ requires "postgresql://" for
    the same thing, so that one substitution is normalized here.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///mywealthlens.db"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


app.config["SQLALCHEMY_DATABASE_URI"] = _resolve_database_uri()
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
# Flask-Migrate/Alembic — the actual schema-change tool going forward on
# Postgres. `migrations/` (created by `flask db init`, run once) holds the
# version history; `flask db migrate` autogenerates a new revision from
# model changes, `flask db upgrade` applies pending revisions. This does
# NOT replace db.create_all() below for SQLite users who never set
# DATABASE_URL — that path is untouched and keeps working exactly as before.
migrate = Migrate(app, db)

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


@app.before_request
def enforce_session_revocation():
    """
    Account security (Sep 2026, Batch 3) — makes "log out other
    devices" actually work. Flask-Login's own cookie has no server-side
    revocation of its own, so this confirms the browser's session
    token still has a live UserSession row (see session_manager.py)
    before letting an otherwise-authenticated request proceed. If the
    row is gone — someone revoked it from Preferences > Security, on
    this device or another — the request is logged out right here,
    rather than on whatever page it happened to land on next.
    """
    if current_user.is_authenticated:
        if not session_manager.validate_and_touch(current_user.id):
            logout_user()
            session.pop('sid', None)


# Global display currency (Sep 2026) — registered once here so every
# template can call format_inr(...)/format_money_precise(...)/
# display_symbol()/display_currency_code() without each route having
# to pass them in explicitly. Routes/modules that already pass their
# own format_inr into render_template() still take precedence for
# that call (an explicit kwarg always overrides a Jinja global of the
# same name) — those module-level format_inr functions were rewired
# to delegate to currency_display.format_money() too, so both paths
# end up currency-aware; this registration is the safety net for
# templates/routes that never passed one explicitly.
import currency_display
app.jinja_env.globals["format_inr"] = currency_display.format_money
app.jinja_env.globals["format_money"] = currency_display.format_money
app.jinja_env.globals["format_money_precise"] = currency_display.format_money_precise
app.jinja_env.globals["display_symbol"] = currency_display.display_symbol
app.jinja_env.globals["display_currency_code"] = currency_display.display_currency_code
app.jinja_env.globals["to_display"] = currency_display.to_display


def _bootstrap_schema():
    """
    Production-readiness audit (Sep 2026): schema is now managed
    entirely through Alembic (`flask db upgrade`), on SQLite as well
    as Postgres — replacing db.create_all() and the old per-module
    hand-written "migrate_xxx.py" script pattern for good. Every
    future schema change (new table, new column, anything) should now
    be captured with `flask db migrate` and applied with
    `flask db upgrade`, on any database.

    This still runs automatically at every startup, exactly like
    create_all() always did — nothing changes about the "pull, restart,
    it's up to date" workflow. It's just Alembic doing the work now,
    so every change is properly version-tracked instead of inferred
    fresh at each restart.

    One-time transition handling: a database that already has tables
    from create_all()'s years of running, but no alembic_version table
    (since create_all() never tracked versions), gets STAMPED at the
    current baseline rather than having Alembic try to CREATE TABLE
    on tables that already exist, which would fail outright. A
    genuinely fresh, empty database just gets the full migration
    history applied normally.
    """
    from sqlalchemy import inspect
    from flask_migrate import upgrade as _alembic_upgrade, stamp as _alembic_stamp

    existing_tables = set(inspect(db.engine).get_table_names())
    if "alembic_version" not in existing_tables and "user" in existing_tables:
        print("Existing pre-Alembic database found — stamping at the current "
              "schema version (no tables touched, no data changed)...")
        _alembic_stamp()

    _alembic_upgrade()


with app.app_context():
    _bootstrap_schema()

def safe_float(val, default=0.0):
    try:
        return float(str(val).strip())
    except (TypeError, ValueError):
        return default

def safe_int(val, default=0):
    """Same purpose as safe_float() above, for form fields parsed with
    int(). Goals-module audit (Sep 2026, Batch 6): add_goal()/edit_goal()
    used a bare int(request.form.get('target_year', 2030)) -- fine for
    the normal <select>, which can only submit a real year, but a
    hostile or scripted POST with a non-numeric value (curl, devtools,
    a buggy client) raised an uncaught ValueError -> unhandled 500,
    instead of the friendly "Target year must be..." flash the rest of
    that validation logic clearly intends."""
    try:
        return int(str(val).strip())
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
        session_manager.create_session(user)
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
            if user.totp_enabled:
                # Two-factor account (Sep 2026, Batch 3): password alone
                # isn't enough to log in — stash the user id as a
                # PENDING second factor rather than calling login_user()
                # yet, and send them to the code-entry step. Nothing
                # about this user is treated as authenticated until
                # /login/2fa verifies the code.
                session['pending_2fa_user_id'] = user.id
                return redirect(url_for('login_2fa'))
            login_user(user)
            session_manager.create_session(user)
            flash(f'Welcome back, {user.name}!', 'success')
            return redirect(url_for('dashboard'))
        else:
            flash('Invalid email or password.', 'error')
    return render_template('login.html')


@app.route('/login/2fa', methods=['GET', 'POST'])
@limiter.limit('5 per 15 minutes', methods=['POST'])
def login_2fa():
    """
    Second step of login for accounts with 2FA enabled — reached only
    after /login already verified the password and parked the user id
    in session['pending_2fa_user_id']. Accepts either a 6-digit
    authenticator code or one of the account's backup codes; either
    way, THIS is the point login_user()/create_session() actually run.
    """
    pending_id = session.get('pending_2fa_user_id')
    if not pending_id:
        return redirect(url_for('login'))
    user = db.session.get(User, pending_id)
    if not user or not user.totp_enabled:
        session.pop('pending_2fa_user_id', None)
        return redirect(url_for('login'))

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        ok = twofa.verify_totp_code(user.totp_secret, code)
        used_backup_code = None
        if not ok:
            for bc in BackupCode.query.filter_by(user_id=user.id, used=False).all():
                if twofa.check_backup_code(code, bc.code_hash):
                    ok = True
                    used_backup_code = bc
                    break
        if ok:
            if used_backup_code:
                used_backup_code.used = True
                db.session.commit()
            session.pop('pending_2fa_user_id', None)
            login_user(user)
            session_manager.create_session(user)
            if used_backup_code:
                remaining = BackupCode.query.filter_by(user_id=user.id, used=False).count()
                flash(f'Welcome back, {user.name}! You used a backup code — '
                      f'{remaining} unused backup code(s) remain.', 'success')
            else:
                flash(f'Welcome back, {user.name}!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid authentication code.', 'error')
    return render_template('login_2fa.html')


@app.route('/logout')
@login_required
def logout():
    session_manager.end_current_session(current_user.id)
    logout_user()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

PASSWORD_RESET_TOKEN_TTL_MINUTES = 60


@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit('5 per 15 minutes', methods=['POST'])
def forgot_password():
    """
    Production-readiness (Sep 2026): this used to look up the user and
    then do nothing — the generic "if an account exists..." message
    was correct security practice, but no reset link was ever actually
    generated or sent. Now it is: a random token is generated, only
    its SHA-256 hash is stored (see User.reset_token_hash), and the
    raw token goes out in the emailed link only. The same generic
    message is shown whether or not the email exists, and whether or
    not sending actually succeeded — this route must never reveal
    which accounts exist, or whether SMTP happens to be configured.
    """
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            raw_token = secrets.token_urlsafe(32)
            user.reset_token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
            user.reset_token_expires = dt.utcnow() + timedelta(minutes=PASSWORD_RESET_TOKEN_TTL_MINUTES)
            db.session.commit()

            reset_url = url_for('reset_password', token=raw_token, _external=True)
            sent, reason = send_password_reset_email(
                app.instance_path, user.email, reset_url, PASSWORD_RESET_TOKEN_TTL_MINUTES
            )
            if not sent:
                app.logger.warning(f"Password reset email not sent for user {user.id}: {reason}")

        flash('If an account exists for that email, a reset link has been sent.', 'success')
        return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit('10 per hour', methods=['POST'])
def reset_password(token):
    """
    The link from the reset email lands here. Looks the user up by the
    SHA-256 hash of the token in the URL (never by the raw token
    directly — same reasoning as never storing the raw token: a DB
    leak alone still can't be used to find or forge a valid reset
    link). Expired or already-used (hash cleared) tokens are rejected
    with the same "invalid or expired" message either way, so a
    guesser can't distinguish the two cases.
    """
    token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
    user = User.query.filter_by(reset_token_hash=token_hash).first()
    valid = user is not None and user.reset_token_expires is not None \
        and user.reset_token_expires > dt.utcnow()

    if not valid:
        flash('That reset link is invalid or has expired — please request a new one.', 'error')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')
        if password != confirm:
            flash('Passwords do not match.', 'error')
            return render_template('reset_password.html', token=token)
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'error')
            return render_template('reset_password.html', token=token)

        user.password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        # Single-use: clear the token immediately on success so the same
        # emailed link can't be replayed to set the password again.
        user.reset_token_hash = None
        user.reset_token_expires = None
        db.session.commit()
        flash('Your password has been reset — please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)

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

    # History for stacked area chart. Values are converted to the
    # user's global display currency (Sep 2026) here, in Python,
    # before being serialized to the template's `| tojson` — the
    # chart's JS then just displays whatever numbers it's given,
    # already in the right currency, and only needs to know which
    # symbol/notation to label them with (see dashboard.html).
    import currency_display
    history = NetWorthHistory.query.filter_by(user_id=current_user.id)\
        .order_by(NetWorthHistory.snapshot_date).limit(365).all()
    history_data = [{
        'date':        h.snapshot_date.strftime('%d %b %Y'),
        'total':       currency_display.to_display(h.total),
        'equity':      currency_display.to_display(h.equity),
        'debt':        currency_display.to_display(h.debt),
        'gold':        currency_display.to_display(h.gold),
        'realestate':  currency_display.to_display(h.realestate),
        'cash':        currency_display.to_display(h.cash),
        'other':       currency_display.to_display(h.other),
    } for h in history]

    # ── Unified cross-module Recent Activity (Wealth, Insurance,
    # Retirement, Family Centre merged into one feed) — replaces the
    # four separate per-module "Recent Activity" widgets, their
    # per-item equivalents (Insurance's Policy Timeline, Retirement's
    # per-scheme Activity section), and Family Centre's old standalone
    # Audit Trail page. See activity.py.
    import activity as activity_module
    recent_activity = activity_module.get_unified_activity(current_user.id, days=10)[:5]

    # Freshest refresh timestamp per section, for the "as of" label next
    # to the Refresh Prices button — None if nothing's ever been
    # refreshed (import time doesn't set these, only an actual refresh).
    stock_times = [s.price_updated_at for s in stocks if s.price_updated_at]
    mf_times = [m.nav_updated_at for m in mfs if m.nav_updated_at]
    stocks_last_refreshed = max(stock_times) if stock_times else None
    mfs_last_refreshed = max(mf_times) if mf_times else None

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
        stocks_last_refreshed=stocks_last_refreshed,
        mfs_last_refreshed=mfs_last_refreshed,
        format_date=format_date)


@app.route('/refresh-prices', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def refresh_prices():
    """
    Manual "Refresh Prices" button (production-readiness, Sep 2026) —
    refreshes every one of the current user's Stock/MutualFund
    holdings via price_refresh.refresh_all_prices(), scoped to just
    this user (the scheduled `flask prices refresh` CLI command is the
    all-users equivalent, for automatic refresh). Rate-limited since
    each click makes real outbound calls (yfinance per stock, one AMFI
    file download).

    Also re-derives the INR-equivalent of every non-INR Wealth Centre
    asset/liability from its stored foreign amount, via one Frankfurter
    lookup per distinct currency in use (fx_rates.refresh_fx_for_wealth_rows)
    — same button, same click, same "keep everything current" intent,
    since most holdings won't have any foreign currency at all and this
    is a no-op for them.
    """
    from price_refresh import refresh_all_prices
    import fx_rates

    summary = refresh_all_prices(db, user_id=current_user.id)
    fx_summary = fx_rates.refresh_fx_for_wealth_rows(db, user_id=current_user.id)

    parts = []
    if summary["stocks_updated"] or summary["stocks_failed"]:
        parts.append(f"{summary['stocks_updated']} stock price(s) updated"
                      + (f", {summary['stocks_failed']} couldn't be fetched" if summary["stocks_failed"] else ""))
    if summary["mfs_updated"] or summary["mfs_failed"] or summary["mfs_no_code"]:
        mf_bit = f"{summary['mfs_updated']} mutual fund NAV(s) updated"
        extras = []
        if summary["mfs_no_code"]:
            extras.append(f"{summary['mfs_no_code']} missing an AMFI code (re-upload the CAS to fix)")
        if summary["mfs_failed"]:
            extras.append(f"{summary['mfs_failed']} not found in today's AMFI file")
        if extras:
            mf_bit += " (" + "; ".join(extras) + ")"
        parts.append(mf_bit)

    if fx_summary["assets_updated"] or fx_summary["liabilities_updated"]:
        parts.append(f"{fx_summary['assets_updated']} foreign-currency asset(s) and "
                     f"{fx_summary['liabilities_updated']} liability(ies) re-converted to INR")
    if fx_summary["currencies_failed"]:
        failed_ccy = ", ".join(f["currency"] for f in fx_summary["currencies_failed"])
        flash(f"Couldn't refresh exchange rates for: {failed_ccy}. Those holdings' INR values are unchanged.", "warning")

    if summary["amfi_error"] and summary["nav_source"] == "mfapi_fallback":
        flash("AMFI's NAV file was unreachable, so mutual fund NAVs were refreshed from a backup source instead. "
              + "Refreshed: " + ". ".join(parts) + ".", "warning")
    elif summary["amfi_error"]:
        flash(f"Stock prices were refreshed, but mutual fund NAVs couldn't be — {summary['amfi_error']}", "error")
    elif not parts:
        flash("No stock or mutual fund holdings to refresh yet — upload a CAS or tradebook first.", "warning")
    else:
        flash("Refreshed: " + ". ".join(parts) + ".", "success")

    return redirect(url_for('dashboard'))


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

    import fx_rates

    # Security (Sep 2026, Batch 3) — active sessions list, with each
    # row's device label pre-computed here since Jinja can't call
    # session_manager's regex helper directly.
    current_token = session_manager.current_session_token()
    active_sessions = []
    for s in session_manager.list_sessions(current_user.id):
        active_sessions.append({
            'id': s.id,
            'device': session_manager.describe_device(s.user_agent),
            'ip_address': s.ip_address or 'Unknown',
            'last_seen_at': s.last_seen_at,
            'created_at': s.created_at,
            'is_current': (s.session_token == current_token),
        })
    backup_codes_remaining = None
    if current_user.totp_enabled:
        backup_codes_remaining = BackupCode.query.filter_by(
            user_id=current_user.id, used=False).count()

    return render_template(
        'preferences.html', user=current_user, system_health=system_health,
        backup_settings=backup_settings, backup_has_passphrase=backup_services.has_passphrase(),
        backup_last_display=backup_last_display,
        supported_currencies=fx_rates.SUPPORTED_CURRENCIES,
        active_sessions=active_sessions,
        backup_codes_remaining=backup_codes_remaining,
    )


@app.route('/account/currency', methods=['POST'])
@login_required
@limiter.limit('20 per hour')
def update_display_currency():
    """
    Saves the user's chosen global display currency (Sep 2026 — see
    currency_display.py). Pure display-layer setting: nothing in the
    database is converted or rewritten by this route, only the one
    column that says which currency to convert TO at render time.
    """
    import fx_rates
    currency = (request.form.get('currency') or 'INR').strip().upper()
    if currency not in fx_rates.SUPPORTED_CURRENCIES:
        flash(f"'{currency}' isn't a supported currency.", "error")
        return redirect(url_for('preferences') + '#appearance')

    current_user.display_currency = currency
    db.session.commit()

    if currency == 'INR':
        flash("Display currency set to Indian Rupee (₹).", "success")
    else:
        # Warm the rate cache right away rather than waiting for the
        # next page render to discover a fetch problem — the user
        # should find out now, not see silently-wrong-looking numbers
        # later. get_display_context() is memoized per-request (flask.g),
        # so this doesn't cost anything extra on the redirect that follows.
        import currency_display
        _, _, _, ok = currency_display.get_display_context()
        if ok:
            flash(f"Display currency set to {fx_rates.SUPPORTED_CURRENCIES[currency]}. "
                  f"Every value across the app is now shown converted to {currency} — "
                  f"nothing in your data was changed.", "success")
        else:
            flash(f"Display currency set to {currency}, but today's exchange rate couldn't "
                  f"be fetched — values will show in INR until a rate becomes available "
                  f"(this refreshes automatically).", "warning")
    return redirect(url_for('preferences') + '#appearance')


@app.route('/account/notifications', methods=['POST'])
@login_required
@limiter.limit('20 per hour')
def update_notification_preferences():
    """
    Saves the two Notifications toggles (My Account > Notifications).
    Both are plain checkboxes on one form, submitted together — an
    unchecked checkbox simply isn't present in request.form, so
    absence means False, matching a normal HTML form's behavior.
    Actual sending happens later, via the `flask notifications ...`
    CLI jobs (notifications_cli.py) run on a schedule — this route
    only flips the opt-in flags.
    """
    current_user.notify_monthly_summary = 'notify_monthly_summary' in request.form
    current_user.notify_renewal_sip_reminders = 'notify_renewal_sip_reminders' in request.form
    db.session.commit()
    flash("Notification preferences saved.", "success")
    return redirect(url_for('preferences') + '#notifications')


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
# Batch 5 (Sep 2026): broadened past Zerodha's own column names with a
# few more variants seen across other Indian brokers' exports (Groww's
# "Company", Upstox's "Scrip", etc.) — hints are still tried in order
# per field, so a specific match (e.g. "trade_date") always wins over
# a generic one (e.g. plain "date", which could otherwise match an
# unrelated "Settlement Date" column on a file that has both).
_TRADEBOOK_COLUMN_HINTS = {
    'symbol':   ['tradingsymbol', 'trading symbol', 'symbol', 'scrip', 'instrument', 'stock name', 'company'],
    'isin':     ['isin'],
    'date':     ['trade_date', 'trade date', 'order execution time', 'execution date', 'date'],
    'type':     ['trade_type', 'transaction_type', 'txn_type', 'buy/sell', 'buy_sell', 'type', 'side'],
    'quantity': ['quantity', 'qty'],
    'price':    ['trade_price', 'trade price', 'price', 'rate'],
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
            # Batch 5 (Sep 2026): some brokers export quantity/price with
            # thousand-separator commas (e.g. "1,234.50") or a stray "Rs."/"₹"
            # symbol -- strip those before the float() conversion instead of
            # letting the row silently get skipped by the except below.
            qty_raw = (raw.get(cols['quantity']) or '0').strip().replace(',', '').replace('₹', '').replace('Rs.', '').replace('Rs', '')
            price_raw = (raw.get(cols['price']) or '0').strip().replace(',', '').replace('₹', '').replace('Rs.', '').replace('Rs', '')
            qty = float(qty_raw or 0)
            price = float(price_raw or 0)

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


def _tradebook_txn_key(symbol, isin, date_val, txn_type, quantity, price):
    """Natural key used to detect a tradebook row that's already been
    imported, when merging a new upload with existing history (Batch 5,
    Sep 2026). Quantity/price are rounded so float-representation noise
    doesn't produce a false "this is new" negative."""
    return (symbol, isin or '', date_val, txn_type, round(quantity or 0, 4), round(price or 0, 4))


def import_tradebook(rows, user_id, wipe=False):
    """
    Rebuilds this user's TRADEBOOK-sourced stock holdings from a list
    of parsed BUY/SELL rows (see _parse_tradebook_csv()). Only rows with
    source='tradebook' are ever touched, so stocks imported via the
    separate CDSL/NSDL CAS upload (source='cdsl') are left untouched —
    the two import paths are independent, matching how a real investor
    might use CDSL for a snapshot balance and a broker tradebook for
    the transaction history behind it.

    Batch 5 (Sep 2026): by default this MERGES the new upload with the
    existing tradebook transaction history instead of replacing it
    outright (the original behavior, kept as an explicit opt-in via
    wipe=True). This matters because most Indian brokers only let you
    export one financial year's tradebook at a time — always wiping
    would silently destroy prior years' transactions (and with them any
    real long-term XIRR) the moment someone uploads a second year's
    file. A row is matched against existing history on a natural key
    (symbol, isin, date, txn_type, quantity, price); anything not
    already present is treated as new and added. wipe=True discards all
    existing tradebook history first and rebuilds from just this file —
    for deliberately starting over, e.g. correcting a bad import.

    If merging and every row in this upload already exists (a repeat
    upload of the same file), nothing is touched at all — existing
    holdings, their ids, and any Goal links to them are left exactly as
    they were, and (0 new transactions) is reported back.

    Grouped by (symbol, isin): closing quantity = sum(BUY qty) -
    sum(SELL qty). Only positive-quantity positions become a holding
    (a fully-sold position is skipped, same convention as mutual
    funds). Live price is fetched the same way the CDSL importer does;
    falls back to the last transaction's price if that fails.

    Returns (holdings_count, transactions_count, portfolio_rate,
    affected_goal_names, new_transactions_count). affected_goal_names —
    see import_detailed_cas()'s docstring for why it exists (Goals-page
    audit, Sep 2026): whenever holdings actually get rebuilt, the new
    Stock rows get new ids, so any GoalHoldingLink to the old
    tradebook-sourced ids is explicitly cleaned up here rather than
    left to go stale.
    """
    existing_stocks = Stock.query.filter_by(user_id=user_id, source='tradebook').all()

    if wipe:
        combined_rows = list(rows)
        new_count = len(rows)
    else:
        existing_rows = [
            {'symbol': t.symbol, 'isin': t.isin, 'date': t.date,
             'txn_type': t.txn_type, 'quantity': t.quantity,
             'price': t.price, 'amount': t.amount}
            for s in existing_stocks for t in s.transactions
        ]
        existing_keys = {
            _tradebook_txn_key(r['symbol'], r['isin'], r['date'], r['txn_type'], r['quantity'], r['price'])
            for r in existing_rows
        }
        new_rows = [
            r for r in rows
            if _tradebook_txn_key(r['symbol'], r['isin'], r['date'], r['txn_type'], r['quantity'], r['price'])
            not in existing_keys
        ]
        combined_rows = existing_rows + new_rows
        new_count = len(new_rows)

        if new_count == 0:
            # Nothing new to add -- leave existing holdings/ids/goal links
            # completely untouched rather than doing a pointless rebuild.
            all_existing_txns = [t for s in existing_stocks for t in s.transactions]
            total_value = sum(s.value for s in existing_stocks)
            portfolio_rate = _portfolio_stock_xirr(all_existing_txns, total_value)
            return len(existing_stocks), len(all_existing_txns), portfolio_rate, [], 0

    stale_stock_ids = [s.id for s in existing_stocks]
    # Batch 5 (Sep 2026) bug fix: Stock.query...delete() is a bulk DELETE
    # that bypasses the ORM-level cascade="all, delete-orphan" on
    # Stock.transactions (that cascade only fires for session.delete(obj),
    # never for Query.delete()), and there's no ON DELETE CASCADE at the DB
    # level either -- so the StockTransaction rows under a wiped Stock were
    # silently left behind, orphaned. That was mostly harmless before (the
    # old rows just sat there unreachable) EXCEPT that SQLite reuses a
    # deleted integer rowid when there's no AUTOINCREMENT column, so a
    # freshly-created Stock could silently inherit a stale, already-deleted
    # Stock's id -- and its relationship would then "reattach" to that old
    # stock's orphaned transactions, corrupting the rebuilt holding with
    # transactions from a completely different import. Deleting the child
    # rows explicitly, first, closes that gap for good.
    if stale_stock_ids:
        # synchronize_session='fetch' (not the default 'evaluate'/False) so
        # SQLAlchemy also evicts these rows from its in-session identity
        # map -- the merge path above may have already loaded some of them
        # via s.transactions, and leaving stale entries behind is exactly
        # what produces the "identity map already had an identity, replacing
        # it" warning once new rows with the same (reused) ids get flushed.
        StockTransaction.query.filter(StockTransaction.stock_id.in_(stale_stock_ids)).delete(synchronize_session='fetch')
    Stock.query.filter_by(user_id=user_id, source='tradebook').delete()
    affected_goal_names = _cleanup_goal_links_for_deleted_holdings(
        user_id, 'stock', stale_stock_ids)
    db.session.flush()
    # SQLite reuses a deleted integer rowid (no AUTOINCREMENT column here),
    # so the StockTransaction rows created just below can legitimately get
    # the same ids the just-deleted rows had. expire_all() clears those
    # stale identity-map entries so SQLAlchemy doesn't warn about (or risk
    # confusing) the replacement -- the deletes above are already flushed,
    # so there's nothing pending to lose.
    db.session.expire_all()

    groups = {}
    for r in combined_rows:
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
    return holdings_count, transactions_count, portfolio_rate, affected_goal_names, new_count


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

    # Batch 5 (Sep 2026): merge-by-default so a second year's tradebook
    # export doesn't wipe out an earlier year's transaction history (see
    # import_tradebook()'s docstring). The "start over" checkbox on the
    # upload form opts back into the old wipe-and-rebuild behavior, for
    # deliberately discarding everything and re-importing from scratch.
    wipe = request.form.get('wipe_existing') == 'on'
    holdings_count, transactions_count, portfolio_rate, affected_goal_names, new_count = \
        import_tradebook(rows, current_user.id, wipe=wipe)

    if holdings_count == 0:
        flash('No open positions found (all holdings in this tradebook may be fully sold).', 'error')
        return redirect(url_for('upload'))

    skip_msg = f' ({skipped} row(s) skipped — unrecognised format.)' if skipped else ''

    if not wipe and new_count == 0:
        flash(f'Nothing new in this file — every transaction was already in your tradebook history.'
              f'{skip_msg}', 'success')
        return redirect(url_for('upload'))

    xirr_msg = f' Portfolio XIRR: {portfolio_rate}%.' if portfolio_rate is not None else ''
    if wipe:
        summary_msg = f'Imported {holdings_count} stock holdings ({transactions_count} transactions).'
    else:
        summary_msg = (f'Added {new_count} new transaction(s). Holdings now: {holdings_count} '
                        f'({transactions_count} transactions total).')
    flash(f'{summary_msg}{xirr_msg}{skip_msg}', 'success')
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
        if rebalance:
            # goal_glide.py is a pure function with no Flask/request context
            # (see its module docstring), so it always messages in INR —
            # re-render the amount here in the user's display currency
            # (Sep 2026 global display currency feature) without touching
            # its own INR-only "amount" field.
            import currency_display
            direction_word = "debt → equity" if rebalance["direction"] == "debt_to_equity" else "equity → debt"
            target_pct = glide_curve[0]["equity_pct"] if glide_curve else 100.0
            rebalance["message"] = (
                f"Move {currency_display.format_money(rebalance['amount'])} {direction_word} "
                f"to reach the {target_pct:.0f}% equity target for this year."
            )
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
@limiter.limit('30 per hour')
def add_goal():
    name        = request.form.get('name', '').strip()
    emoji       = request.form.get('emoji', '').strip()
    target_amt  = safe_float(request.form.get('target_amt'))
    target_year = safe_int(request.form.get('target_year'), None)
    current_savings = safe_float(request.form.get('current_savings'))
    monthly_sip     = safe_float(request.form.get('monthly_sip'))
    annual_return   = safe_float(request.form.get('annual_return', '12'))
    if not name or target_amt <= 0:
        flash('Please enter a goal name and target amount.', 'error')
        return redirect(url_for('goals'))
    if target_year is None:
        flash('Please enter a valid target year.', 'error')
        return redirect(url_for('goals'))
    # Goals-module audit (Sep 2026, Batch 6): this used to check against
    # a hardcoded "<= 2024", which only worked by coincidence when 2024
    # was still in the past for everyone using the app -- as the
    # calendar moves on it would silently accept genuinely past target
    # years (e.g. "2025" typed in 2027) instead of catching them here.
    # Checked against the real current year instead; this year itself
    # is allowed (an "achieve it now" goal is a legitimate, tested shape).
    if target_year < dt.utcnow().year:
        flash('Target year must be this year or later.', 'error')
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
@limiter.limit('30 per hour')
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
    target_year = safe_int(request.form.get('target_year'), None)
    if not name or target_amt <= 0:
        flash('Please enter a goal name and target amount.', 'error')
        return redirect(url_for('goals'))
    if target_year is None:
        flash('Please enter a valid target year.', 'error')
        return redirect(url_for('goals'))
    if target_year < dt.utcnow().year:
        flash('Target year must be this year or later.', 'error')
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
@limiter.limit('30 per hour')
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
@limiter.limit('30 per hour')
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
            import currency_display
            flash(f'Only {remaining_pct:g}% of {name} ({currency_display.format_money(remaining_value)}) is still '
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
@limiter.limit('30 per hour')
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
@limiter.limit('30 per hour')
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
@limiter.limit('30 per hour')
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
@limiter.limit('30 per hour')
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
    # Global display currency (Sep 2026): converts the stored INR value
    # and formats it in the user's chosen display currency — see
    # currency_display.py. Every _fmt() call site in this PDF export
    # gets currency-awareness for free via this one function.
    # PDF-safe (Sep 2026 audit fix): this export uses ReportLab's base
    # Helvetica font, which can't render the ₹ glyph (not in
    # WinAnsiEncoding) — format_money_pdf_safe() spells INR as "Rs."
    # instead, same convention insurance_centre's PDF export already
    # used this for; format_money() (the ₹-glyph version) is correct
    # for HTML templates, just not for this ReportLab document.
    import currency_display
    return currency_display.format_money_pdf_safe(n)

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

    import currency_display
    _sym = currency_display.display_symbol()
    summary_data = [
        ['Asset Class', f'Value ({_sym})', 'Allocation %'],
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
        mf_data = [['Scheme Name', 'Folio', 'Units', f'NAV ({_sym})', f'Value ({_sym})']]
        for m in mfs:
            nav_disp = currency_display.to_display(getattr(m, 'nav', 0) or 0)
            mf_data.append([
                m.scheme or '—',
                getattr(m, 'folio', '—') or '—',
                f"{getattr(m, 'units', 0) or 0:,.3f}",
                f"{nav_disp:,.2f}",
                _fmt(m.value),
            ])
        mt = Table(mf_data, colWidths=[W*0.42, W*0.16, W*0.12, W*0.14, W*0.16])
        mt.setStyle(_tbl_style())
        story += [mt, Spacer(1, 3*mm)]

    # Stocks
    if stocks:
        story.append(Paragraph("Stocks / Demat Holdings", S['h3']))
        sk_data = [['Company / ISIN', 'Quantity', f'Price ({_sym})', f'Value ({_sym})']]
        for s in stocks:
            price_disp = currency_display.to_display(s.live_price or s.buy_price or 0)
            sk_data.append([
                s.name or getattr(s, 'isin', '—') or '—',
                f"{getattr(s, 'quantity', 0) or 0:,.0f}",
                f"{price_disp:,.2f}",
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
        ph_data = [['Name', f'Value ({_sym})']]
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
                 'On Track ✓' if calc['on_track'] else f"Shortfall {_fmt(calc.get('shortfall',0))}"],
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
                sip_hdr = [['Period', f'Projected Balance ({_sym})', f'vs Target ({_sym})', 'Progress %']]
                sip_rows = []
                for (label, bal, kind) in proj:
                    delta = bal - compare_target
                    progress = min(round(bal / compare_target * 100, 1), 100) if compare_target else 0
                    bal_disp = currency_display.to_display(bal)
                    delta_disp = currency_display.to_display(delta)
                    sip_rows.append([
                        label,
                        f"{bal_disp:,.0f}",
                        f"{'+' if delta_disp >= 0 else ''}{delta_disp:,.0f}",
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

    # Global display currency (Sep 2026): every stored figure below is
    # still INR; convert to the user's chosen display currency before
    # writing it into a cell, and label/format cells with that currency's
    # symbol instead of a hardcoded ₹. See currency_display.py.
    import currency_display
    _sym = currency_display.display_symbol()
    _cd  = currency_display.to_display
    _num_fmt = f'"{_sym}"#,##0'

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
    _hdr_row(ws1, r, ['Asset Class', f'Value ({_sym})', 'Allocation %'])
    r += 1
    summary_rows = [
        ('Equity (MF + Stocks)', equity_val),
        ('Debt (PPF/VPF/SSY/FD)', debt_val),
        ('Gold / Silver', gold_val),
        ('Real Estate', realestate_val),
        ('Cash & Others', cash_val + other_val),
    ]
    for i, (lbl, val) in enumerate(summary_rows):
        # Percentage stays a ratio of the raw INR totals (unaffected by
        # currency conversion); only the displayed value itself converts.
        pct_val = round(val/total*100, 1) if total else 0
        _data_row(ws1, r, [lbl, _cd(val), f"{pct_val}%"], alt=i%2==1)
        ws1.cell(row=r, column=2).number_format = _num_fmt
        r += 1
    # Total row
    _hdr_row(ws1, r, ['TOTAL', _cd(total), '100%'])
    ws1.cell(row=r, column=2).number_format = _num_fmt

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
    _hdr_row(ws2, r2, ['Category', 'Name / Scheme', 'Sub-type', 'Units / Qty', f'Price / NAV ({_sym})', f'Value ({_sym})'])
    r2 += 1

    all_rows = []
    for m in mfs:
        nav = getattr(m, 'nav', '') or ''
        all_rows.append(['Mutual Fund', m.scheme or '—',
                         getattr(m, 'amc', '') or '',
                         getattr(m, 'units', '') or '', _cd(nav) if nav != '' else '', _cd(m.value)])
    for s in stocks:
        price = s.live_price or s.buy_price or ''
        all_rows.append(['Stock', s.name or '—', getattr(s, 'isin', '') or '',
                         getattr(s, 'quantity', '') or '', _cd(price) if price != '' else '', _cd(s.value)])
    for a in all_assets_flat:
        all_rows.append([a.category, a.name or '—', a.asset_type or '', '', '', _cd(a.current_value)])

    for i, row_data in enumerate(all_rows):
        _data_row(ws2, r2, row_data, alt=i%2==1)
        ws2.cell(row=r2, column=5).number_format = _num_fmt
        ws2.cell(row=r2, column=6).number_format = _num_fmt
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
            _hdr_row(ws4, r4, [f'Target ({_sym})', 'Target Year', f'Savings ({_sym})',
                                f'SIP/mo ({_sym})', 'Return %', f'Projected ({_sym})', 'Status'])
            r4 += 1
            status = 'On Track ✓' if calc['on_track'] else f"Shortfall {currency_display.format_money(calc.get('shortfall',0))}"
            _data_row(ws4, r4, [_cd(g.target_amt), g.target_year, _cd(g.current_savings or 0),
                                 _cd(g.monthly_sip or 0), f"{g.annual_return}%",
                                 round(_cd(calc['projected'])), status])
            for col in [1, 3, 4, 6]:
                ws4.cell(row=r4, column=col).number_format = _num_fmt
            ws4.cell(row=r4, column=7).font = \
                _font(GREEN_HEX if calc['on_track'] else RED_HEX, bold=True, sz=9)
            r4 += 2

            # SIP projection
            ws4.cell(row=r4, column=1, value="SIP Growth Projection").font = _font(TEAL_HEX, bold=True, sz=10)
            r4 += 1
            _hdr_row(ws4, r4, ['Period', f'Projected Balance ({_sym})', f'vs Target ({_sym})', 'Progress %'])
            r4 += 1

            compare_target = calc['inflation_adjusted_target'] if calc.get('inflation_applied') else g.target_amt
            proj = _sip_projection(g.target_amt, g.target_year, effective_current,
                                   g.monthly_sip, g.annual_return, getattr(g, 'step_up_pct', 0) or 0)
            for i, (label, bal, _) in enumerate(proj):
                delta = bal - compare_target
                progress = min(round(bal / compare_target * 100, 1), 100) if compare_target else 0
                _data_row(ws4, r4, [label, round(_cd(bal)), round(_cd(delta)), f"{progress}%"], alt=i%2==1)
                ws4.cell(row=r4, column=2).number_format = _num_fmt
                ws4.cell(row=r4, column=3).number_format = _num_fmt
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
@limiter.limit('10 per hour')
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


@app.route('/account/delete', methods=['POST'])
@login_required
@limiter.limit('5 per hour')
def delete_account():
    """
    Self-service "Delete my account" (Batch 3, Sep 2026). Permanently
    deletes the signed-in user's account and everything tied to it,
    across every module — the same cascade-delete logic the admin CLI
    tool (delete_user.py) uses, both sourced from account_deletion.py
    so the two can never drift the way this project's table lists once
    did (see that module's docstring).

    Two-factor confirmation, since this is irreversible from the UI:
      1. Current password (bcrypt-verified, same pattern as
         change_password() above).
      2. Typing the literal word DELETE into a confirmation field —
         stronger than the backup-restore box's checkbox, because a
         restore can be undone and this cannot.

    A full DB backup is still taken first (account_deletion.backup_db)
    as a last-resort safety net for Mohan himself, even though the
    user-facing flow treats this as final.
    """
    current_pw = request.form.get('current_password', '')
    typed_confirm = (request.form.get('confirm_delete', '') or '').strip()

    if not bcrypt.checkpw(current_pw.encode('utf-8'), current_user.password.encode('utf-8')):
        flash('Current password is incorrect. Account was not deleted.', 'error')
        return redirect(url_for('preferences') + '#privacy')

    if typed_confirm != 'DELETE':
        flash('You must type DELETE exactly to confirm. Account was not deleted.', 'error')
        return redirect(url_for('preferences') + '#privacy')

    import account_deletion
    user_id = current_user.id
    user_name = current_user.name

    # Backup first — best-effort; deletion still proceeds even if this
    # fails, since the user explicitly asked for their data gone.
    account_deletion.backup_db(app)

    account_deletion.wipe_document_files(app, db, user_id)
    account_deletion.cascade_delete_user(app, db, user_id)

    logout_user()
    session.pop('sid', None)
    flash(f"Your MyWealthLens account and all associated data have been "
          f"permanently deleted. Goodbye, {user_name}.", 'success')
    return redirect(url_for('login'))


# ── Two-Factor Authentication (Sep 2026, Batch 3) ──

@app.route('/account/2fa/setup', methods=['GET', 'POST'])
@login_required
@limiter.limit('10 per hour')
def account_2fa_setup():
    """
    GET: starts (or resumes) a setup attempt — generates a fresh TOTP
    secret if one isn't already pending, and shows the QR code to
    scan. Note the secret is written to the DB here, before it's
    confirmed — see the User.totp_secret docstring in models.py for
    why that's fine (totp_enabled is what actually turns 2FA on).

    POST: verifies the 6-digit code the user typed back in from their
    authenticator app. On success, flips totp_enabled on, issues a
    fresh set of backup codes, and shows them exactly once.
    """
    if current_user.totp_enabled:
        flash('Two-factor authentication is already enabled on your account.', 'error')
        return redirect(url_for('preferences') + '#security')

    if request.method == 'POST':
        code = request.form.get('code', '').strip()
        if not twofa.verify_totp_code(current_user.totp_secret, code):
            flash('That code didn’t match. Double-check your authenticator app and try again.', 'error')
            return redirect(url_for('account_2fa_setup'))

        current_user.totp_enabled = True
        # Clear out any stale unused codes from an earlier abandoned
        # attempt before issuing a fresh set.
        BackupCode.query.filter_by(user_id=current_user.id).delete()
        plain_codes = twofa.generate_backup_codes()
        for code_str in plain_codes:
            db.session.add(BackupCode(user_id=current_user.id,
                                       code_hash=twofa.hash_backup_code(code_str)))
        db.session.commit()
        return render_template('2fa_backup_codes.html', codes=plain_codes, regenerated=False)

    if not current_user.totp_secret:
        current_user.totp_secret = twofa.generate_secret()
        db.session.commit()

    uri = twofa.provisioning_uri(current_user.totp_secret, current_user.email)
    qr = twofa.qr_data_uri(uri)
    return render_template('2fa_setup.html', qr_data_uri=qr, secret=current_user.totp_secret)


@app.route('/account/2fa/cancel-setup', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def account_2fa_cancel_setup():
    """Abandons an in-progress (not yet confirmed) setup attempt —
    clears the pending secret so /account/2fa/setup starts clean next
    time. Refuses to touch an already-ENABLED account; that's what
    /account/2fa/disable is for, and it requires re-verifying the
    account first."""
    if not current_user.totp_enabled:
        current_user.totp_secret = None
        db.session.commit()
    return redirect(url_for('preferences') + '#security')


@app.route('/account/2fa/disable', methods=['POST'])
@login_required
@limiter.limit('5 per hour')
def account_2fa_disable():
    """Turning 2FA OFF requires proving you still control both factors
    — current password AND a valid code — so a stolen/forgotten
    session alone can't downgrade an account's security."""
    current_pw = request.form.get('current_password', '')
    code = request.form.get('code', '').strip()

    if not bcrypt.checkpw(current_pw.encode('utf-8'), current_user.password.encode('utf-8')):
        flash('Current password is incorrect. Two-factor authentication was not disabled.', 'error')
        return redirect(url_for('preferences') + '#security')

    ok = twofa.verify_totp_code(current_user.totp_secret, code)
    if not ok:
        for bc in BackupCode.query.filter_by(user_id=current_user.id, used=False).all():
            if twofa.check_backup_code(code, bc.code_hash):
                ok = True
                break
    if not ok:
        flash('That code didn’t match. Two-factor authentication was not disabled.', 'error')
        return redirect(url_for('preferences') + '#security')

    current_user.totp_enabled = False
    current_user.totp_secret = None
    BackupCode.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    flash('Two-factor authentication has been turned off.', 'success')
    return redirect(url_for('preferences') + '#security')


@app.route('/account/2fa/regenerate-backup-codes', methods=['POST'])
@login_required
@limiter.limit('5 per hour')
def account_2fa_regenerate_backup_codes():
    """Invalidates every existing backup code and issues 10 fresh ones
    — for when a user has used most of them up, or worries an old set
    may have leaked. Requires the current password, same bar as
    disabling 2FA outright, since this is also a meaningful account-
    recovery capability."""
    if not current_user.totp_enabled:
        flash('Two-factor authentication isn’t enabled on your account.', 'error')
        return redirect(url_for('preferences') + '#security')

    current_pw = request.form.get('current_password', '')
    if not bcrypt.checkpw(current_pw.encode('utf-8'), current_user.password.encode('utf-8')):
        flash('Current password is incorrect. Backup codes were not regenerated.', 'error')
        return redirect(url_for('preferences') + '#security')

    BackupCode.query.filter_by(user_id=current_user.id).delete()
    plain_codes = twofa.generate_backup_codes()
    for code_str in plain_codes:
        db.session.add(BackupCode(user_id=current_user.id,
                                   code_hash=twofa.hash_backup_code(code_str)))
    db.session.commit()
    return render_template('2fa_backup_codes.html', codes=plain_codes, regenerated=True)


# ── Session Management ("log out other devices", Sep 2026, Batch 3) ──

@app.route('/account/sessions/<int:session_id>/revoke', methods=['POST'])
@login_required
@limiter.limit('20 per hour')
def account_revoke_session(session_id):
    deleted, was_current = session_manager.revoke_session(current_user.id, session_id)
    if not deleted:
        flash('That session was already gone.', 'error')
        return redirect(url_for('preferences') + '#security')

    if was_current:
        logout_user()
        session.pop('sid', None)
        flash('You have been logged out of this device.', 'success')
        return redirect(url_for('login'))

    flash('That device has been logged out.', 'success')
    return redirect(url_for('preferences') + '#security')


@app.route('/account/sessions/revoke-others', methods=['POST'])
@login_required
@limiter.limit('10 per hour')
def account_revoke_other_sessions():
    count = session_manager.revoke_other_sessions(current_user.id)
    if count:
        flash(f'Logged out {count} other device(s). This device stays signed in.', 'success')
    else:
        flash('No other active sessions found.', 'success')
    return redirect(url_for('preferences') + '#security')


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

# Prices — `flask prices refresh`, for scheduled automatic stock
# price / mutual fund NAV refresh (production-readiness, Sep 2026).
# Same Task Scheduler pattern as Wealth's snapshot CLI above.
from price_refresh_cli import register_price_refresh_cli
register_price_refresh_cli(app)

# Notifications — `flask notifications monthly-summary` / `flask
# notifications reminders` (Sep 2026). Same Task Scheduler pattern as
# the CLI commands above; see notifications_cli.py / notifications_service.py.
from notifications_cli import register_notifications_cli
register_notifications_cli(app)

if __name__ == '__main__':
    import os
    # Production-readiness audit (Sep 2026): Flask's own app.run() is
    # Werkzeug's development server — single-threaded by default, not
    # hardened against slow/malicious clients, and explicitly documented
    # by Flask itself as unfit for anything but local development, even
    # with debug=False. Waitress is a real, battle-tested production WSGI
    # server with no C extensions to compile, which matters here since it
    # needs to "just work" from `py app.py` on Windows the same way the
    # dev server always did — no separate process manager, no reverse
    # proxy required to be safe to leave running.
    #
    # HTTPS is deliberately NOT handled here — deferred until a hosting
    # platform is chosen, since Render/Railway/etc. all terminate TLS
    # automatically at their edge the moment this is deployed there, and
    # building local HTTPS now would just be thrown away. MWL_HTTPS stays
    # the single flag to flip (see SESSION_COOKIE_SECURE above) once real
    # HTTPS is in front of this, whatever form that takes.
    from waitress import serve
    port = int(os.environ.get('PORT', 5000))
    print(f"Serving on http://127.0.0.1:{port} (Waitress — Ctrl+C to stop)")
    serve(app, host='0.0.0.0', port=port)