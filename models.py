"""
models.py — Database Models
============================
Tables:
  1. User                    — registered users
  2. MutualFund              — parsed from CAMS/KFintech CAS PDF (current
                                 holding snapshot, reconciled from #3)
  3. MutualFundTransaction   — full transaction history per scheme, from
                                 a DETAILED CAS PDF (casparser). Powers
                                 real per-scheme/portfolio XIRR — see
                                 upload_cams() and services in app.py
  4. Stock                   — current holding snapshot from a CDSL/NSDL CAS
                                 PDF, or reconciled from #9 (broker tradebook)
  5. Goal                    — financial goals, incl. glide-path and
                                 post-retirement drawdown parameters
  6. GoalHoldingLink         — links a Goal to a real holding (MutualFund,
                                 Stock, wealth.WealthAsset or
                                 retirement_centre.RetirementScheme) with an
                                 allocation % and an equity/debt/other
                                 classification — see goal_glide.py
  7. UserProfile             — life stage profile
  8. Insurance               — insurance policies
  9. StockTransaction        — full buy/sell history per stock, from a broker
                                 tradebook CSV. Powers real per-stock/portfolio
                                 XIRR for equities — see stock_xirr.py and
                                 import_tradebook() in app.py
  10. NetWorthHistory        — daily net worth snapshots (see its own class
                                 docstring for why this coexists with
                                 wealth.models.WealthSnapshot — Phase N)

Note (Phase H): the legacy Asset model/table has been retired. The
authoritative Assets system is wealth.models.WealthAsset. See the
Phase H final report for the full audit and migration decision.

Note (Phase N): the legacy Loan model/table has also been retired —
confirmed unused (no imports, no FKs, no live queries anywhere in
the codebase) and confirmed empty (0 rows on both real user accounts
at time of removal). The authoritative Liabilities system is
wealth.models.WealthLiability. See wealth/migrate_phase_n.py for the
table-drop migration and the Phase N final report for the full audit.
"""

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "user"
    id       = db.Column(db.Integer, primary_key=True)
    name     = db.Column(db.String(150), nullable=False)
    email    = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)

    # Document Vault client-side (zero-knowledge) encryption — Sep 2026
    # production-readiness work. None of these three values are secret
    # on their own (that's the point): encryption_salt is combined with
    # the user's separate encryption passphrase — which is NEVER sent to
    # or stored on this server, in this column or any other — entirely
    # in the browser to derive the real key via PBKDF2. The verifier
    # pair lets the app confirm a re-entered passphrase is correct
    # without the server ever learning the key itself: it's a known
    # constant encrypted client-side at setup time, and checking it
    # again just means decrypting it client-side and comparing the
    # result. See static/js/mwl-crypto.js for the actual cryptography.
    encryption_salt           = db.Column(db.String(64),  nullable=True)
    encryption_verifier       = db.Column(db.Text,        nullable=True)
    encryption_verifier_iv    = db.Column(db.String(64),  nullable=True)
    encryption_enabled_at     = db.Column(db.DateTime,    nullable=True)

    # Forgot Password (Sep 2026): only the SHA-256 hash of the reset
    # token is ever stored — the raw token exists only in the emailed
    # link and briefly in memory while handling that request, never
    # written to the database. A DB leak alone can't be used to reset
    # anyone's password. Single-use (cleared on successful reset) and
    # time-limited (see PASSWORD_RESET_TOKEN_TTL_MINUTES in app.py).
    reset_token_hash    = db.Column(db.String(64), nullable=True)
    reset_token_expires = db.Column(db.DateTime,   nullable=True)

    # Global display currency (Sep 2026) — a pure DISPLAY-layer setting.
    # Every value is still stored in INR everywhere in the database,
    # exactly as before (CAS holdings are India-only by nature; Wealth
    # Centre's own per-asset currency support converts foreign holdings
    # TO INR at save time — see wealth/models.py). This column only
    # controls what currency amounts are CONVERTED TO and shown as on
    # screen/in exports, via currency_display.py. Changing it never
    # rewrites a single stored value.
    display_currency = db.Column(db.String(3), nullable=False, default="INR")

    # Notifications (Sep 2026) — both start OFF; the user opts in under
    # My Account > Notifications. Matches the reset-password email's
    # existing pattern of only ever sending mail the user asked for.
    # Actual sending is driven by the `flask notifications ...` CLI
    # commands (notifications_cli.py), invoked on a schedule via Windows
    # Task Scheduler — same pattern as `flask wealth snapshot` / `flask
    # backup run` / `flask prices refresh`. See notifications_service.py.
    notify_monthly_summary       = db.Column(db.Boolean, nullable=False, default=False)
    notify_renewal_sip_reminders = db.Column(db.Boolean, nullable=False, default=False)

    # Two-factor authentication (Sep 2026, Batch 3). totp_secret is the
    # base32 TOTP seed (pyotp) -- set as soon as setup starts, but
    # totp_enabled only flips to True once the user proves they can
    # generate a valid code from it (see app.py's /account/2fa/setup).
    # A secret sitting here with totp_enabled=False is just an
    # in-progress/abandoned setup attempt, not a live second factor.
    totp_secret  = db.Column(db.String(64), nullable=True)
    totp_enabled = db.Column(db.Boolean, nullable=False, default=False)

    mutual_funds = db.relationship("MutualFund", backref="owner", lazy=True, cascade="all, delete-orphan")
    mf_transactions = db.relationship("MutualFundTransaction", backref="owner", lazy=True, cascade="all, delete-orphan")
    stocks       = db.relationship("Stock",      backref="owner", lazy=True, cascade="all, delete-orphan")
    stock_transactions = db.relationship("StockTransaction", backref="owner", lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User {self.email}>"


class MutualFund(db.Model):
    __tablename__ = "mutual_fund"
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    folio       = db.Column(db.String(50),  nullable=True)
    amc         = db.Column(db.String(200), nullable=True)
    scheme      = db.Column(db.String(300), nullable=False)
    isin        = db.Column(db.String(20),  nullable=True)
    amfi_code   = db.Column(db.String(20),  nullable=True)
    units       = db.Column(db.Float, nullable=False)
    nav         = db.Column(db.Float, nullable=True)
    value       = db.Column(db.Float, nullable=False)
    invested    = db.Column(db.Float, nullable=True)   # net cost basis, from CAS 'cost' when available
    xirr        = db.Column(db.Float, nullable=True)   # cached per-scheme XIRR, refreshed on each CAS import
    source      = db.Column(db.String(20), default="cams")
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Production-readiness (Sep 2026): set whenever `nav` is refreshed via
    # the manual "Refresh Prices" button or the scheduled `flask prices
    # refresh` CLI command (see price_refresh.py) — same purpose as
    # Stock.price_updated_at below, added now for parity since MFs didn't
    # have any way to refresh nav after import until this.
    nav_updated_at = db.Column(db.DateTime, nullable=True)
    transactions = db.relationship(
        "MutualFundTransaction", backref="holding", lazy=True,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<MutualFund {self.scheme} units={self.units}>"


class MutualFundTransaction(db.Model):
    """
    Phase: Upload CAS rebuild. One row per transaction line parsed from a
    CAMS/KFintech DETAILED CAS PDF (via the `casparser` library) — NOT the
    Summary statement, which only has closing balances and can't produce
    these. This table is what makes real per-scheme and portfolio XIRR
    possible; MutualFund (above) remains the current-holding snapshot,
    now reconciled FROM these transactions + casparser's own valuation
    rather than re-derived by hand.

    mutual_fund_id links to the MutualFund holding row for the same
    folio+scheme. A holding can be re-imported (CAS re-upload) — see
    upload_cams() in app.py for the folio+scheme reconciliation rule
    (replace transactions for that scheme, keep other schemes untouched).
    """
    __tablename__ = "mutual_fund_transaction"
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    mutual_fund_id = db.Column(db.Integer, db.ForeignKey("mutual_fund.id"), nullable=False)
    folio          = db.Column(db.String(50),  nullable=False)
    scheme         = db.Column(db.String(300), nullable=False)
    date           = db.Column(db.Date, nullable=False)
    # Raw casparser TransactionType value (e.g. 'PURCHASE', 'REDEMPTION',
    # 'DIVIDEND_REINVEST', 'SWITCH_IN', 'STT_TAX', ...). Kept as the exact
    # enum string rather than our own vocabulary so the XIRR cash-flow
    # classification (wealth-adjacent logic, see services.classify_cashflow)
    # can evolve without a migration.
    txn_type       = db.Column(db.String(30), nullable=False)
    description    = db.Column(db.String(300), nullable=True)
    amount         = db.Column(db.Float, nullable=True)
    units          = db.Column(db.Float, nullable=True)
    nav            = db.Column(db.Float, nullable=True)
    balance_units  = db.Column(db.Float, nullable=True)  # running unit balance printed on the statement
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<MFTxn {self.scheme} {self.txn_type} {self.date}>"


class Stock(db.Model):
    __tablename__ = "stock"
    id               = db.Column(db.Integer, primary_key=True)
    user_id          = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    isin             = db.Column(db.String(20),  nullable=False)
    name             = db.Column(db.String(300), nullable=False)
    quantity         = db.Column(db.Float, nullable=False)
    buy_price        = db.Column(db.Float, nullable=True)
    live_price       = db.Column(db.Float, nullable=True)
    value            = db.Column(db.Float, nullable=False)
    ticker           = db.Column(db.String(20), nullable=True)
    source           = db.Column(db.String(20), default="cdsl")
    uploaded_at      = db.Column(db.DateTime, default=datetime.utcnow)
    price_updated_at = db.Column(db.DateTime, nullable=True)
    # Tradebook rebuild: net cost basis + cached XIRR, same pattern as
    # MutualFund.invested/xirr — refreshed on each tradebook import.
    invested         = db.Column(db.Float, nullable=True)
    xirr             = db.Column(db.Float, nullable=True)
    transactions = db.relationship(
        "StockTransaction", backref="holding", lazy=True,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Stock {self.name} qty={self.quantity}>"


class StockTransaction(db.Model):
    """
    Tradebook rebuild. One row per BUY/SELL line parsed from a broker
    tradebook CSV (Zerodha console export format, with a generic
    header-matching fallback for other brokers — see
    import_tradebook() in app.py). Mirrors MutualFundTransaction's
    role for mutual funds: this is what makes real per-stock and
    portfolio XIRR for equities possible; Stock (above) remains the
    current-holding snapshot, reconciled FROM these transactions.
    """
    __tablename__ = "stock_transaction"
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    stock_id    = db.Column(db.Integer, db.ForeignKey("stock.id"), nullable=False)
    isin        = db.Column(db.String(20),  nullable=True)
    symbol      = db.Column(db.String(50),  nullable=False)
    date        = db.Column(db.Date, nullable=False)
    txn_type    = db.Column(db.String(10), nullable=False)  # 'BUY' or 'SELL'
    quantity    = db.Column(db.Float, nullable=False)
    price       = db.Column(db.Float, nullable=True)
    amount      = db.Column(db.Float, nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<StockTxn {self.symbol} {self.txn_type} {self.date}>"


class Goal(db.Model):
    __tablename__ = "goal"
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name            = db.Column(db.String(200), nullable=False)
    emoji           = db.Column(db.String(10),  nullable=True)
    target_amt      = db.Column(db.Float, nullable=False)
    target_year     = db.Column(db.Integer, nullable=False)
    current_savings = db.Column(db.Float, default=0)
    monthly_sip     = db.Column(db.Float, default=0)
    annual_return   = db.Column(db.Float, default=12.0)
    inflation_rate  = db.Column(db.Float, default=0)
    # Goals rebuild: annual SIP step-up (%). 0 = flat SIP, unchanged
    # behaviour from before this column existed. See
    # calculate_goal()'s step-up branch in app.py.
    step_up_pct     = db.Column(db.Float, default=0)

    # Glide path (goal_glide.py): target equity allocation % now vs.
    # at the target year, interpolated linearly year by year. Debt %
    # is always 100 - equity % (no separate column — see
    # goal_glide.glide_path_curve()). Defaults (75 -> 30) are a
    # generic "aggressive now, conservative near the goal" curve,
    # editable per goal — a short-dated goal should set these much
    # closer together (or equal, to disable glide-based rebalancing
    # advice entirely).
    glide_start_equity_pct = db.Column(db.Float, default=75.0)
    glide_end_equity_pct   = db.Column(db.Float, default=30.0)

    # Post-retirement drawdown (goal_glide.py's drawdown_curve()) —
    # only used/shown when is_retirement_goal is set. Kept on Goal
    # rather than a separate table: one goal is either a retirement
    # goal or it isn't, and these fields are meaningless otherwise.
    is_retirement_goal         = db.Column(db.Boolean, default=False)
    retirement_age             = db.Column(db.Integer, nullable=True)
    life_expectancy            = db.Column(db.Integer, default=85)
    monthly_expense_today      = db.Column(db.Float, nullable=True)
    expense_inflation_pct      = db.Column(db.Float, default=6.0)
    post_retirement_return_pct = db.Column(db.Float, default=7.0)

    # Goal review nudge: last time the user explicitly confirmed this
    # goal's numbers/allocations are still right (POST /goals/<id>/review).
    # None means "never reviewed" — always nudge until the first review.
    last_reviewed_at = db.Column(db.DateTime, nullable=True)

    # Goal archiving: same is_archived/archived_at convention used by
    # WealthAsset/RetirementScheme/InsurancePolicy (Archive -> Restore,
    # never a straight delete). archive_reason distinguishes a goal the
    # user actually hit ('achieved') from one they gave up on
    # ('dropped') — the plain boolean can't tell those apart, and the
    # UI needs to show them differently (see goals() in app.py).
    is_archived     = db.Column(db.Boolean, default=False, nullable=False)
    archived_at     = db.Column(db.DateTime, nullable=True)
    archive_reason  = db.Column(db.String(20), nullable=True)  # 'achieved' | 'dropped'

    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    links = db.relationship("GoalHoldingLink", backref="goal", lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Goal {self.name}>"


class GoalHoldingLink(db.Model):
    """
    Goals rebuild: links a Goal to a real holding so 'current savings'
    can be the ACTUAL current value of specific investments instead of
    (or alongside) a manually-typed number. Mirrors the nominee/heir
    linking pattern already used elsewhere in the app (percentage
    allocation rather than all-or-nothing ownership — a holding CAN
    back more than one goal, e.g. 60% of an FD toward one goal and the
    remaining 40% toward another).

    A holding is still a single pool of real money, though, so as of
    Sep 2026 the total allocation_pct across ALL of a user's goals for
    one holding_type+holding_id can never exceed 100 — enforced in
    app.py's link_goal_holding() via _holding_allocated_pct(), and
    surfaced on the Goals page as "X% / ₹Y still available" so a
    second link request can only ever claim what's actually left
    unallocated (previously unenforced: the same fund could be linked
    at 100% to two different goals and silently double-counted).
    Likewise, if a holding_id disappears for good — the user deletes
    it, or a CAS/CDSL/tradebook re-upload wipes and reinserts with new
    ids — any GoalHoldingLink still pointing at it is explicitly
    deleted by _cleanup_goal_links_for_deleted_holdings() rather than
    left to silently rot; see the callers of that helper in app.py,
    wealth/services.py and retirement_centre/routes.py.

    holding_type is a plain string rather than a second FK column per
    type, so a new linkable asset type is a new holding_type value,
    not a schema change. Wired up in this phase: 'mutual_fund'
    (MutualFund), 'stock' (Stock), 'wealth_asset' (WealthAsset — this
    is how a Fixed Deposit or Gold holding becomes goal-linkable,
    since both already live in that table under the Bank & Deposits /
    Precious Metals categories — see wealth/models.py), and
    'retirement_scheme' (RetirementScheme — EPF/PPF/NPS/etc, keyed off
    current_balance). The lookup is done in app.py's
    _goal_linked_value() rather than a DB-level relationship, since
    SQLAlchemy can't polymorphically FK across tables here.

    asset_class classifies THIS link (not the underlying holding) as
    'equity', 'debt' or 'other' for glide-path rebalancing (see
    goal_glide.py). It's on the link rather than the holding because
    the same holding can reasonably be viewed differently by different
    goals, and because none of the four holding tables above have a
    reliable equity/debt classification of their own to read instead
    (a scheme name alone doesn't say "large-cap equity fund" vs "debt
    fund" reliably enough to automate this) — the user sets it once
    when linking, defaulted sensibly per holding_type in app.py.
    """
    __tablename__ = "goal_holding_link"
    id             = db.Column(db.Integer, primary_key=True)
    goal_id        = db.Column(db.Integer, db.ForeignKey("goal.id"), nullable=False)
    holding_type   = db.Column(db.String(30), nullable=False)
    holding_id     = db.Column(db.Integer, nullable=False)
    allocation_pct = db.Column(db.Float, default=100.0)
    asset_class    = db.Column(db.String(10), default="equity")  # 'equity' | 'debt' | 'other'
    created_at     = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<GoalHoldingLink goal={self.goal_id} {self.holding_type}#{self.holding_id} {self.allocation_pct}%>"


class UserProfile(db.Model):
    __tablename__ = "user_profile"
    id             = db.Column(db.Integer, primary_key=True)
    user_id        = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    age            = db.Column(db.Integer, nullable=False)
    marital_status = db.Column(db.String(20), default="single")
    dependents     = db.Column(db.Integer, default=0)
    updated_at     = db.Column(db.DateTime, default=db.func.now(), onupdate=db.func.now())


class NetWorthHistory(db.Model):
    """
    Phase N architectural decision (Sections 7/8 of the Phase N spec):
    this table and wealth.models.WealthSnapshot both track "net worth
    over time" and have been repeatedly flagged as a possible
    accidental duplicate architecture across Phases H, I, and M.

    Investigated with evidence, not assumption. Conclusion: KEEP BOTH
    — they have materially distinct, non-overlapping responsibilities
    (spec's Option C), not a legacy-vs-current split:

      NetWorthHistory  — powers the MAIN APP dashboard's (/dashboard)
                          trend chart. Scope INCLUDES MutualFund/Stock
                          (CAS-imported broker holdings — a feature
                          entirely outside the Wealth Centre) alongside
                          WealthAsset/WealthLiability category totals.
                          Written on page-load (deduped per calendar
                          day) by _save_snapshot() in app.py.

      WealthSnapshot   — powers the WEALTH CENTRE's own History page
                          (/wealth/history). Scope is STRICTLY
                          WealthAsset + WealthLiability — deliberately
                          excludes MutualFund/Stock (Phase D's
                          established Wealth-Centre boundary). Written
                          only by explicit user action or the Phase I
                          scheduled CLI, never by a page visit.

    Because NetWorthHistory's scope genuinely includes non-Wealth-
    Centre data (CAS investment holdings), merging it into
    WealthSnapshot would either silently change what WealthSnapshot
    has always meant (Phase F's explicit scope), or require dropping
    CAS holdings from the main dashboard's trend chart — a real,
    working, independent feature, not legacy debt. Neither is an
    acceptable side effect of an internal architecture cleanup.
    Kept as two deliberately separate systems, now documented as such
    on both classes.
    """
    __tablename__ = "net_worth_history"
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    snapshot_date = db.Column(db.Date, nullable=False)
    total         = db.Column(db.Float, nullable=False)
    equity        = db.Column(db.Float, default=0)
    debt          = db.Column(db.Float, default=0)
    gold          = db.Column(db.Float, default=0)
    realestate    = db.Column(db.Float, default=0)
    cash          = db.Column(db.Float, default=0)
    other         = db.Column(db.Float, default=0)
    liabilities   = db.Column(db.Float, default=0)
    # International Investing Centre's active-holdings total, converted
    # USD->INR via currency_display.usd_to_inr() (Sep 2026) — added so
    # the main dashboard's net worth figure actually includes foreign
    # holdings, which it didn't when that module first shipped. 0 for
    # every pre-existing row (nullable=False with a server default via
    # the migration), same "immutable history" treatment as every other
    # column here — old snapshots aren't backfilled, they just predate
    # this module.
    international = db.Column(db.Float, default=0)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint("user_id", "snapshot_date", name="uq_user_snapshot_date"),
    )

    def __repr__(self):
        return f"<NetWorthHistory {self.snapshot_date} total={self.total}>"


class FxRateCache(db.Model):
    """
    Global display currency (Sep 2026) — a small shared cache of
    INR-per-unit-foreign-currency rates, refreshed at most once a day
    per currency, regardless of how many users or page views ask for
    that currency. Without this, every page load for every user with
    a non-INR display currency would trigger a live Frankfurter call —
    slow, wasteful, and unnecessary since exchange rates don't move
    within a day for this app's purposes (same "fetch once, apply
    many" principle as price_refresh.py's AMFI handling).

    Not tied to any one user — currency, not user_id, is the key,
    since the rate for USD->INR is the same for every user who has
    USD selected as their display currency.
    """
    __tablename__ = "fx_rate_cache"
    currency   = db.Column(db.String(3), primary_key=True)
    rate       = db.Column(db.Float, nullable=False)
    # ^ INR per 1 unit of `currency` (e.g. currency="USD", rate=83.5
    #   means 1 USD = INR 83.5) — same convention as fx_rates.py's
    #   fetch_fx_rate(from_currency, "INR").
    rate_date  = db.Column(db.Date, nullable=True)
    # ^ the date Frankfurter says this rate is actually dated (may
    #   lag `fetched_at` over a weekend/holiday — normal, not stale).
    fetched_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<FxRateCache {self.currency}={self.rate} fetched={self.fetched_at}>"


class NotificationLog(db.Model):
    """
    Notifications (Sep 2026) — records one row per notification email
    actually sent, so the daily `flask notifications ...` CLI runs (see
    notifications_cli.py / notifications_service.py) never send the same
    reminder or summary twice. Each row's (user_id, notif_type, ref_key)
    is unique:
      - monthly summary:  notif_type="monthly_summary",
                           ref_key="YYYY-MM" (the month being summarized)
      - renewal/SIP due:  notif_type="renewal_sip_reminder",
                           ref_key="insurance:<policy_id>:<renewal_date>"
                           or "recurring:<recurring_payment_id>:<due_date>"
    A CLI run checks for an existing row before sending, so re-running
    the same day (or after a missed day) never re-sends a reminder for
    the same underlying due date — matching the idempotent-CLI pattern
    already used by the Wealth snapshot / backup / price-refresh jobs.
    """
    __tablename__ = "notification_log"
    __table_args__ = (
        db.UniqueConstraint("user_id", "notif_type", "ref_key",
                             name="uq_notification_log_user_type_ref"),
    )
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    notif_type = db.Column(db.String(30), nullable=False)
    ref_key    = db.Column(db.String(120), nullable=False)
    sent_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<NotificationLog user={self.user_id} {self.notif_type} {self.ref_key}>"


class UserSession(db.Model):
    """
    Account security (Sep 2026, Batch 3) — server-side record of one
    active login, so "log out other devices" is actually possible.

    Flask's session cookie is signed but client-side by default: the
    cookie alone can't be revoked from the server once issued. So each
    successful login (see app.py's session_manager.create_session())
    also writes one row here and puts its random `session_token` in
    the browser's session cookie. A before_request check then confirms
    that token still has a row before treating the request as
    authenticated -- deleting the row (revoking) is what actually logs
    that device out, on its very next request.

    user_agent/ip_address are stored only to show a human a recognizable
    "Chrome on Windows, last seen 2 hours ago" line in Preferences >
    Security -- never used for anything else, and deleted the moment
    the session itself is revoked or expires.
    """
    __tablename__ = "user_session"
    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    session_token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    user_agent    = db.Column(db.String(255), nullable=True)
    ip_address    = db.Column(db.String(64), nullable=True)
    created_at    = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<UserSession user={self.user_id} token={self.session_token[:8]}…>"


class BackupCode(db.Model):
    """
    One single-use 2FA recovery code (Sep 2026, Batch 3) -- issued 10
    at a time whenever 2FA is enabled or backup codes are regenerated
    (see app.py's /account/2fa/setup and /account/2fa/regenerate-backup-
    codes). Stored bcrypt-hashed, exactly like the account password
    (code_hash), never in plaintext -- the plain codes are shown to the
    user exactly once, right after generation, and cannot be retrieved
    again afterward. `used` flips to True the moment a code is
    consumed at login, so each code works exactly once.
    """
    __tablename__ = "backup_code"
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    code_hash  = db.Column(db.String(256), nullable=False)
    used       = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f"<BackupCode user={self.user_id} used={self.used}>"
