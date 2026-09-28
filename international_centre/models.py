"""
International Investing Centre — Models
=========================================
New standalone module (confirmed with Mohan, Sep 2026) for tracking
investments held OUTSIDE India — US/global stocks, ETFs, international
mutual funds, foreign real estate, foreign bank accounts/bonds — kept
fully separate from Wealth Centre's own multi-currency asset support
(wealth/models.py), which converts a foreign holding straight to INR.

Design decision: this module uses USD as a FIXED reporting anchor,
regardless of the holding's own native currency or the user's chosen
global display currency (My Account > Appearance). Every holding's
value is converted to USD and stored as usd_value; the dashboard/
reports total THAT, then hand the USD total to currency_display's
existing INR-based conversion pipeline (via usd_to_inr(), added to
currency_display.py alongside this module) to render in the user's
actual display currency. This matches how people actually think about
a foreign portfolio ("my US portfolio is worth $42,000") as a number
in its own right, not just "whatever the rupee value happens to be
today."

Statement import (a real broker/bank CSV or PDF) is explicitly OUT of
scope for this first version — Mohan doesn't have a sample file yet.
Manual entry only, matching how every other module here started
(Wealth Centre, Cashflow Centre). Import can be added once a real
sample file is available to build against — see international_centre/
routes.py's upload placeholder note.

Four tables:
  1. InternationalHolding      — one row per foreign position (stock,
                                  fund, property, bank account, bond...)
  2. InternationalTransaction  — BUY/SELL/DIVIDEND history per holding,
                                  powers real per-holding & portfolio
                                  XIRR (see international_xirr.py)
  3. RemittanceRecord          — money sent abroad under RBI's
                                  Liberalised Remittance Scheme (LRS),
                                  tracked against the $250,000-per-
                                  financial-year cap
  4. InternationalValueSnapshot — periodic (daily, via scheduled job)
                                  USD value snapshot per holding, used
                                  to derive the OPENING/PEAK/CLOSING
                                  values Schedule FA (Income Tax Act
                                  foreign-asset disclosure) requires for
                                  each CALENDAR year (Jan-Dec —
                                  deliberately NOT the Indian financial
                                  year; Schedule FA's own reporting
                                  period is the calendar year, a
                                  well-known quirk for Indian filers
                                  with foreign assets)

Archive→Restore lifecycle for holdings, matching the rest of the app
(never a direct hard delete of something with transaction/snapshot
history hanging off it).

IMPORTANT — not tax advice: Schedule FA figures this module produces
(services.py's get_schedule_fa_summary()) are a tracking aid built from
this module's own recorded data and Frankfurter's FX rates, NOT a
CBDT-compliant filing document. The Income Tax Department prescribes
its own conversion rate (SBI's TT buying rate as of the relevant date)
for actual filing purposes, which can differ from Frankfurter's. This
is stated plainly on the report itself — see templates/schedule_fa.html.
"""
from datetime import datetime
from models import db


class InternationalAssetType:
    US_STOCK = "US/International Stock"
    US_ETF = "US/International ETF"
    INTL_MUTUAL_FUND = "International Mutual Fund"
    FOREIGN_BANK_ACCOUNT = "Foreign Bank Account"
    FOREIGN_REAL_ESTATE = "Foreign Real Estate"
    FOREIGN_BOND = "Foreign Bond"
    RSU_ESPP = "RSU / ESPP (Employer Stock)"
    OTHER = "Other"
    ALL = [US_STOCK, US_ETF, INTL_MUTUAL_FUND, FOREIGN_BANK_ACCOUNT,
           FOREIGN_REAL_ESTATE, FOREIGN_BOND, RSU_ESPP, OTHER]

    # Which types support live price lookups by ticker (yfinance) vs.
    # are always manually valued (a bank balance, a property).
    TICKER_BASED = {US_STOCK, US_ETF, RSU_ESPP}


class InternationalTxnType:
    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    ALL = [BUY, SELL, DIVIDEND]


class RemittancePurpose:
    INVESTMENT_SECURITIES = "Investment in securities/shares"
    INVESTMENT_PROPERTY = "Investment in immovable property"
    MAINTENANCE_OF_RELATIVE = "Maintenance of close relatives abroad"
    EDUCATION = "Education abroad"
    EMPLOYMENT = "Emigration / employment abroad"
    OTHER = "Other"
    ALL = [INVESTMENT_SECURITIES, INVESTMENT_PROPERTY, MAINTENANCE_OF_RELATIVE,
           EDUCATION, EMPLOYMENT, OTHER]


# RBI's Liberalised Remittance Scheme annual cap, in USD, per resident
# individual per financial year — this is a POLICY LIMIT set by RBI,
# not something MyWealthLens calculates; it hasn't changed since 2015
# (raised from $125k to $250k) but IS the kind of figure that could
# change by government notification, so it's kept as a single named
# constant, easy to find and update if it ever does.
LRS_ANNUAL_LIMIT_USD = 250_000


class InternationalHolding(db.Model):
    """One foreign position — a stock/ETF/fund holding, a foreign bank
    account, a piece of overseas property, a foreign bond. Value is
    always expressed in the holding's own native_currency AND, derived
    from that, in USD (this module's fixed anchor — see module
    docstring). For ticker-based types, quantity/avg_cost/live price
    drive value; for everything else (bank account, real estate, bond)
    current_value_native is entered directly, same as Wealth Centre's
    own manual-asset model."""
    __tablename__ = "international_holding"
    __table_args__ = (
        db.Index("ix_intl_holding_user_archived", "user_id", "archived"),
    )

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    asset_type = db.Column(db.String(50), nullable=False)   # InternationalAssetType.ALL
    name       = db.Column(db.String(200), nullable=False)  # "Apple Inc.", "Chase Checking", "Condo, Austin TX"
    ticker     = db.Column(db.String(20), nullable=True)     # for TICKER_BASED types (live price lookups)
    country    = db.Column(db.String(100), nullable=True)    # "United States", "Singapore", ... (Schedule FA needs this)
    broker_or_institution = db.Column(db.String(200), nullable=True)  # "Interactive Brokers", "Chase Bank"
    account_number_masked = db.Column(db.String(50), nullable=True)   # last 4 digits only, by convention — never the full number

    native_currency = db.Column(db.String(3), nullable=False, default="USD")  # fx_rates.SUPPORTED_CURRENCIES

    # Ticker-based holdings: driven by quantity + price. Manually-valued
    # holdings (bank account, property, bond): current_value_native set
    # directly, quantity/prices left null.
    quantity              = db.Column(db.Float, nullable=True)
    avg_cost_native        = db.Column(db.Float, nullable=True)  # per-unit average cost, native currency
    live_price_native      = db.Column(db.Float, nullable=True)  # per-unit, native currency
    current_value_native   = db.Column(db.Float, nullable=False)  # always populated — computed for ticker-based, entered directly otherwise

    # USD anchor (this module's fixed reporting currency — see module
    # docstring). Re-derived every time current_value_native changes or
    # a price/FX refresh runs; not user-editable.
    usd_value    = db.Column(db.Float, nullable=False, default=0.0)
    fx_rate_used = db.Column(db.Float, nullable=True)   # native_currency -> USD rate used for usd_value
    fx_rate_date = db.Column(db.Date, nullable=True)
    price_updated_at = db.Column(db.DateTime, nullable=True)

    invested_native = db.Column(db.Float, nullable=True)  # net cost basis in native currency, from transactions
    xirr            = db.Column(db.Float, nullable=True)  # cached, refreshed alongside price/value

    notes    = db.Column(db.Text, nullable=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transactions = db.relationship(
        "InternationalTransaction", backref="holding", lazy=True,
        cascade="all, delete-orphan",
    )
    snapshots = db.relationship(
        "InternationalValueSnapshot", backref="holding", lazy=True,
        cascade="all, delete-orphan",
    )
    nominees = db.relationship(
        "InternationalHoldingNominee", backref="holding", lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<InternationalHolding {self.name} {self.native_currency}{self.current_value_native}>"

    @property
    def is_ticker_based(self):
        return self.asset_type in InternationalAssetType.TICKER_BASED

    @property
    def total_nominees_percentage(self):
        return sum(n.percentage or 0 for n in self.nominees)


class InternationalHoldingNominee(db.Model):
    """Who should get this foreign holding — mirrors WealthAssetHeir/
    InsuranceNominee/RetirementSchemeNominee exactly (Sep 2026, added
    after Mohan flagged that international holdings were invisible to
    Family Centre's "who's connected to my financial life" view and
    Coverage Gaps check). One holding can have several nominees, each
    with their own share; percentage is validated the same way (can't
    exceed 100% on its own, nor push the holding's running total over
    100% — see international_centre/services.py's create_holding/
    update_holding, which do the wipe-and-rebuild-on-save handling the
    same way wealth/services.py's create_asset/update_asset do for
    heirs). Relationship is free text, matching WealthAssetHeir's own
    convention, not a fixed list the way Insurance's nominee form
    uses."""
    __tablename__ = "international_holding_nominee"
    __table_args__ = (
        db.Index("ix_intl_holding_nominee_holding", "holding_id"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    holding_id = db.Column(db.Integer,
                            db.ForeignKey("international_holding.id", ondelete="CASCADE"),
                            nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    name         = db.Column(db.String(200), nullable=False)
    relationship = db.Column(db.String(100), nullable=True)
    percentage   = db.Column(db.Float, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<InternationalHoldingNominee {self.name} ({self.relationship})>"


class InternationalTransaction(db.Model):
    """BUY/SELL/DIVIDEND history for a holding, native currency. Powers
    real per-holding and portfolio XIRR (international_xirr.py),
    mirroring StockTransaction's role for domestic stocks. A dividend
    here is a real cash payout (there's no reinvestment transaction
    type in this module) so it's always a genuine positive cash flow —
    see international_xirr.py's module docstring."""
    __tablename__ = "international_transaction"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    holding_id = db.Column(db.Integer, db.ForeignKey("international_holding.id"), nullable=False)

    date     = db.Column(db.Date, nullable=False)
    txn_type = db.Column(db.String(10), nullable=False)  # InternationalTxnType.ALL
    quantity      = db.Column(db.Float, nullable=True)  # null for a DIVIDEND, or a lump-sum bank/property entry
    price_native  = db.Column(db.Float, nullable=True)
    amount_native = db.Column(db.Float, nullable=False)  # always populated — quantity*price for BUY/SELL, the payout for DIVIDEND

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<InternationalTxn {self.txn_type} {self.date} {self.amount_native}>"


class RemittanceRecord(db.Model):
    """One outward remittance under RBI's Liberalised Remittance Scheme
    (LRS). Tracked in both the amount actually sent (INR, since that's
    what leaves an Indian bank account) and its USD equivalent at the
    time (what counts against the $250k/FY LRS cap — RBI's limit is
    denominated in USD regardless of which currency the money is
    converted to on the way out)."""
    __tablename__ = "international_remittance"
    __table_args__ = (
        db.Index("ix_intl_remit_user_date", "user_id", "date"),
    )

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    holding_id = db.Column(db.Integer, db.ForeignKey("international_holding.id"), nullable=True)
    # ^ optional link to the holding this remittance funded — nullable
    # since a remittance can predate the holding being recorded, or
    # fund something not tracked as a holding at all (e.g. a relative's
    # maintenance abroad).

    date           = db.Column(db.Date, nullable=False)
    amount_inr     = db.Column(db.Float, nullable=False)
    amount_usd     = db.Column(db.Float, nullable=False)  # amount_inr converted to USD at the rate on `date` — this is what counts against LRS_ANNUAL_LIMIT_USD
    fx_rate_used   = db.Column(db.Float, nullable=True)
    purpose        = db.Column(db.String(50), nullable=False, default=RemittancePurpose.INVESTMENT_SECURITIES)
    remitting_bank = db.Column(db.String(100), nullable=True)
    notes          = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Remittance {self.date} ${self.amount_usd}>"


class InternationalValueSnapshot(db.Model):
    """One dated USD-value snapshot per holding, written by the daily
    scheduled job (`flask international snapshot` — mirrors `flask
    wealth snapshot`). Purpose: Schedule FA requires the PEAK value a
    foreign asset held during the CALENDAR year (Jan-Dec, not the
    Indian financial year), which can't be reconstructed after the
    fact without having actually recorded values along the way — same
    reasoning as NetWorthHistory/WealthSnapshot elsewhere in the app.
    One row per (holding, date); a day with no job run just has no
    snapshot, which Schedule FA reporting treats as a data gap rather
    than guessing a value for it (see services.py)."""
    __tablename__ = "international_value_snapshot"
    __table_args__ = (
        db.UniqueConstraint("holding_id", "date", name="uq_intl_snapshot_holding_date"),
        db.Index("ix_intl_snapshot_holding_date", "holding_id", "date"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    holding_id = db.Column(db.Integer, db.ForeignKey("international_holding.id"), nullable=False)
    date       = db.Column(db.Date, nullable=False)
    usd_value  = db.Column(db.Float, nullable=False)

    def __repr__(self):
        return f"<InternationalSnapshot holding={self.holding_id} {self.date} ${self.usd_value}>"
