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
  5. VestingTranche             — RSU/ESPP vesting events, RSU_ESPP
                                  holdings only (Batch 9.7, Sep 2026) —
                                  FMV-at-vest sets both the taxable
                                  perquisite value and, later, the
                                  capital-gains cost basis; see the
                                  table's own docstring

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
The same disclaimer, for the same reason, applies to every other tax-
adjacent report this module produces (TCS estimate, DTAA/Form 67
summary, LTCG/STCG classification — Batch 9.4/9.5/9.6, Sep 2026).
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


class DocumentType:
    """Batch 9.3 (Sep 2026) — Document Vault for international holdings.
    Own list per this module's document-taxonomy convention (mirrors
    insurance_centre.DocumentType / retirement_centre's equivalent —
    each module keeps its own, deliberately not shared)."""
    PURCHASE_CONFIRMATION = "Purchase Confirmation / Statement"
    ACCOUNT_STATEMENT     = "Account Statement"
    TAX_DOCUMENT          = "Tax Document (1099 / W-8BEN / Foreign Tax)"
    OWNERSHIP_DOCUMENT    = "Property / Ownership Document"
    OTHER_DOCUMENTS       = "Other Documents"

    ALL = [PURCHASE_CONFIRMATION, ACCOUNT_STATEMENT, TAX_DOCUMENT,
           OWNERSHIP_DOCUMENT, OTHER_DOCUMENTS]


class TimelineEvent:
    """Batch 10.3 (Oct 2026) — event types for a holding's audit trail.
    Own list per this module's convention (mirrors insurance_centre's
    TimelineEvent, deliberately not shared)."""
    CREATED             = "Holding Created"
    UPDATED             = "Holding Updated"
    NOMINEE_UPDATED     = "Nominees Updated"
    VALUE_REFRESHED     = "Value Refreshed"
    TRANSACTION_ADDED   = "Transaction Added"
    TRANSACTION_EDITED  = "Transaction Edited"
    TRANSACTION_DELETED = "Transaction Deleted"
    VESTING_ADDED       = "Vesting Tranche Added"
    VESTING_EDITED      = "Vesting Tranche Edited"
    VESTING_DELETED     = "Vesting Tranche Deleted"
    DOCUMENT_UPLOADED   = "Document Uploaded"
    DOCUMENT_DELETED    = "Document Deleted"
    ARCHIVED            = "Archived"
    RESTORED            = "Restored"


class InternationalTxnType:
    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    ALL = [BUY, SELL, DIVIDEND]


class VestingPlanType:
    """Batch 9.7 (Sep 2026) — RSU/ESPP vesting tranches, RSU_ESPP
    holdings only. Both plan types are tracked the same way (a dated
    event that grants/purchases shares at a known fair market value),
    but the tax treatment of the price actually paid differs: RSU
    grants are free (no purchase_price_native), while ESPP shares are
    bought at a discount to FMV -- see VestingTranche's own docstring
    for how that discount becomes taxable perquisite income."""
    RSU = "RSU"
    ESPP = "ESPP"
    ALL = [RSU, ESPP]


class RateBasis:
    """Batch 11 (Oct 2026) — which date's SBI TT buying rate converts a
    foreign-currency amount to INR. Two conventions appear in practice and
    in the sources, so the module offers both instead of silently picking
    one: the rate ON the event's own date, or the rate on the last day of
    the MONTH BEFORE the event (the Rule 115 convention for income, gains
    and salary perquisites). Which one applies to which figure is spelled
    out in rates.py and in the CA brief."""
    SAME_DAY = "same_day"
    PREV_MONTH_END = "prev_month_end"
    ALL = [SAME_DAY, PREV_MONTH_END]


class CgMethod:
    """How a capital gain is converted to INR (a point practitioners
    disagree on — see rates.py): convert cost and proceeds SEPARATELY at
    their own dates' rates, or compute the gain in the foreign currency
    and convert it once at the sale date's rate."""
    SEPARATE = "separate"
    SINGLE = "single"
    ALL = [SEPARATE, SINGLE]


class RemittancePurpose:
    INVESTMENT_SECURITIES = "Investment in securities/shares"
    INVESTMENT_PROPERTY = "Investment in immovable property"
    MAINTENANCE_OF_RELATIVE = "Maintenance of close relatives abroad"
    EDUCATION = "Education abroad"
    MEDICAL = "Medical treatment abroad"   # Batch 10.1 (Oct 2026) — has its own TCS rate
    EMPLOYMENT = "Emigration / employment abroad"
    OTHER = "Other"
    ALL = [INVESTMENT_SECURITIES, INVESTMENT_PROPERTY, MAINTENANCE_OF_RELATIVE,
           EDUCATION, MEDICAL, EMPLOYMENT, OTHER]


# RBI's Liberalised Remittance Scheme annual cap, in USD, per resident
# individual per financial year — this is a POLICY LIMIT set by RBI,
# not something MyWealthLens calculates; it hasn't changed since 2015
# (raised from $125k to $250k) but IS the kind of figure that could
# change by government notification, so it's kept as a single named
# constant, easy to find and update if it ever does.
LRS_ANNUAL_LIMIT_USD = 250_000

# TCS (Tax Collected at Source) on LRS remittances: Batch 9.4 hardcoded
# one threshold + one rate here. Batch 10.1 (Oct 2026) moved the rules
# into international_centre/tcs_rules.py — a dated table, because both
# the threshold (₹7L -> ₹10L from 1 Apr 2025) and the rate (now depends
# on purpose; education/medical dropped to 2% from 1 Apr 2026) have
# changed by law and will change again.


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

    # Batch 11 (Oct 2026) — Schedule FA, Table A3 wants the foreign
    # entity's address, ZIP and nature (company / fund / bank ...), none
    # of which the app stored before. All optional; the Schedule FA
    # report lists what is still missing per holding.
    entity_address = db.Column(db.String(255), nullable=True)
    entity_zip     = db.Column(db.String(20), nullable=True)
    entity_nature  = db.Column(db.String(60), nullable=True)

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
    value_updated_at = db.Column(db.DateTime, nullable=True)
    # Batch 10.3 (Oct 2026) — when the holding's VALUE was last set by the
    # user (creation, or an edit that changed current_value_native). Drives
    # the "this manually-valued asset hasn't been updated in a while"
    # nudge. updated_at can't be used for that: it moves on every FX
    # refresh, even when the user has not touched the value.

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
    documents = db.relationship(
        "InternationalHoldingDocument", backref="holding", lazy=True,
        cascade="all, delete-orphan",
    )
    vesting_tranches = db.relationship(
        "VestingTranche", backref="holding", lazy=True,
        cascade="all, delete-orphan",
    )
    schedule_fa_inputs = db.relationship(
        "ScheduleFaYearInput", backref="holding", lazy=True,
        cascade="all, delete-orphan",
    )
    timeline = db.relationship(
        "InternationalTimeline", backref="holding", lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="InternationalTimeline.created_at.desc()",
    )

    def __repr__(self):
        return f"<InternationalHolding {self.name} {self.native_currency}{self.current_value_native}>"

    @property
    def is_ticker_based(self):
        return self.asset_type in InternationalAssetType.TICKER_BASED

    @property
    def is_rsu_espp(self):
        """Batch 9.7 (Sep 2026) — gates the Vesting Tranches section on
        holding_detail.html, same pattern as is_ticker_based above
        (avoids hardcoding the asset type's display string in a
        template)."""
        return self.asset_type == InternationalAssetType.RSU_ESPP

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
    amount_native = db.Column(db.Float, nullable=False)  # always populated — quantity*price for BUY/SELL. For a DIVIDEND, the NET amount actually received (after any withholding) — this is what XIRR uses as the real cash flow, unaffected by Batch 9.5 below.

    gross_amount_native  = db.Column(db.Float, nullable=True)
    tax_withheld_native  = db.Column(db.Float, nullable=True)
    # Batch 9.5 (Sep 2026) — DIVIDEND only. Optional: most foreign
    # brokers withhold tax at source (e.g. the US's 25% treaty rate
    # under the India-US DTAA) before crediting a dividend, so what
    # actually lands in the account is already net. When given,
    # gross_amount_native is what was DECLARED (before withholding) and
    # tax_withheld_native is what was withheld; amount_native (the real
    # cash flow) is derived as gross - withheld — see
    # services.add_transaction()/update_transaction(). Left null (as
    # every dividend before this batch already is) for a manually-
    # entered net-only dividend, or for BUY/SELL, where they're
    # meaningless. Powers get_dtaa_summary()'s Form 67 support figures.

    ttbr_override      = db.Column(db.Float, nullable=True)
    ttbr_override_date = db.Column(db.Date, nullable=True)
    # Batch 11 (Oct 2026) — an SBI TT buying rate (INR per 1 unit of the
    # holding's currency) typed in for THIS event, e.g. a figure the CA
    # gave. When set it beats the rate book and any estimate for every INR
    # conversion of this transaction. `ttbr_override_date` is only a note
    # of which date the rate belongs to.

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

    education_loan_funded = db.Column(db.Boolean, nullable=False, default=False)
    # Batch 10.1 (Oct 2026) — only meaningful when purpose is Education:
    # an education loan from a financial institution has its own (lower
    # or nil) TCS rate. Ignored for every other purpose.

    tcs_amount_inr = db.Column(db.Float, nullable=False, default=0.0)
    # Batch 9.4 (Sep 2026), reworked Batch 10.1 (Oct 2026) — an ESTIMATE
    # of the TCS on THIS remittance: the portion above the running,
    # date-ordered aggregate threshold for its financial year, at the
    # rate for its purpose (see tcs_rules.py). From 10.1 the whole
    # financial year is recomputed (services.recompute_fy_tcs) after
    # every add/edit/delete, so the figure is always consistent with
    # what's currently logged, regardless of entry order. An authorised
    # dealer (bank) collects the real TCS from the totals IT sees for
    # your PAN across ALL banks, which this module cannot — treat this
    # as your own estimate, not your bank's figure.

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


class VestingTranche(db.Model):
    """Batch 9.7 (Sep 2026) — one RSU/ESPP vesting event: a dated grant
    of shares (RSU) or a discounted purchase (ESPP), RSU_ESPP holdings
    only. Kept as its own table rather than folded into
    InternationalTransaction because a vesting event isn't really a
    BUY -- there's no price "paid" for an RSU grant, and an ESPP
    purchase's price paid is deliberately NOT its capital-gains cost
    basis (see below), which InternationalTransaction's BUY handling
    has no room to represent.

    Tax treatment (India), which is why this table exists:
      1. PERQUISITE (taxable as salary income, in the FY of vesting):
         (fmv_native - purchase_price_native) * quantity -- the full
         value of an RSU grant (purchase_price_native is None/0), or
         just the discount on an ESPP purchase. See
         perquisite_value_native below and services.
         get_vesting_perquisite_summary().
      2. CAPITAL GAINS (later, when the shares are eventually SOLD):
         cost basis is FMV at vest, NOT what was actually paid --
         perquisite tax was already charged on the FMV-vs-paid
         difference, so taxing it again via a lower capital-gains cost
         basis would be double taxation. See cost_basis_native below,
         and services.classify_capital_gains(), which folds these
         tranches into the same FIFO lot-matching BUY transactions use
         (Batch 9.6), keyed on vest_date/fmv_native exactly as a BUY is
         keyed on its own date/price.

    grant_date is optional and purely informational (RSU grant date /
    ESPP offering-period start) -- every actual tax/FIFO calculation
    here uses vest_date, the date shares were actually received."""
    __tablename__ = "international_vesting_tranche"
    __table_args__ = (
        db.Index("ix_intl_vesting_holding", "holding_id"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    holding_id = db.Column(db.Integer, db.ForeignKey("international_holding.id"), nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    plan_type  = db.Column(db.String(10), nullable=False, default=VestingPlanType.RSU)  # VestingPlanType.ALL
    grant_date = db.Column(db.Date, nullable=True)   # informational only — see docstring
    vest_date  = db.Column(db.Date, nullable=False)  # the date shares actually vested / the ESPP purchase settled

    quantity   = db.Column(db.Float, nullable=False)
    fmv_native = db.Column(db.Float, nullable=False)  # fair market value PER SHARE at vest, native currency

    purchase_price_native = db.Column(db.Float, nullable=True)
    # ^ ESPP only: the discounted price actually paid per share. Null
    #   (treated as 0) for RSU, which is a free grant.

    notes = db.Column(db.String(500), nullable=True)

    ttbr_override      = db.Column(db.Float, nullable=True)
    ttbr_override_date = db.Column(db.Date, nullable=True)
    # Batch 11 — same meaning as on InternationalTransaction: an explicit
    # SBI TT buying rate for this vesting event (perquisite and capital-
    # gains cost basis both use it).

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def perquisite_value_native(self):
        """Taxable as salary income in the FY of vesting — see class
        docstring point 1."""
        paid = self.purchase_price_native or 0.0
        return round((self.fmv_native - paid) * self.quantity, 2)

    @property
    def cost_basis_native(self):
        """The cost basis a later SALE of these shares uses for capital
        gains — FMV at vest, not what was actually paid. See class
        docstring point 2."""
        return round(self.fmv_native * self.quantity, 2)

    def __repr__(self):
        return f"<VestingTranche {self.plan_type} {self.vest_date} qty={self.quantity}>"


class InternationalHoldingDocument(db.Model):
    """Batch 9.3 (Sep 2026) — Document Vault for international holdings,
    closing the last parity gap flagged when this module first shipped
    (Insurance, Retirement and Wealth all had one; this module had
    none). Local document metadata only — file bytes never touch the
    database, same as every other module's Document Vault. Files live
    at instance/documents/international/<holding_id>/<stored_name>.

    Unlike InsuranceDocument's ondelete="SET NULL" (which keeps a
    document row around for audit after its policy is hard-deleted),
    this table CASCADEs on holding delete via the ORM relationship
    above (cascade="all, delete-orphan") — the same choice
    RetirementDocument made, for the same reason: like Retirement
    Centre, this module's own delete_holding_permanently() already
    requires the holding to be archived first (Archive -> Delete
    Permanently lifecycle), so there is no risk of silently losing
    live audit trail data, and simplicity wins.

    iv / is_encrypted: schema-level parity with RetirementDocument's
    Document Vault client-side encryption columns (Sep 2026 production-
    readiness work — see static/js/mwl-crypto.js), added here so this
    module doesn't need a follow-up migration once that feature is
    switched on for it too. IMPORTANT — confirmed during this batch's
    audit: the actual browser-side code that would populate these
    fields on upload/download (referenced in Wealth/Retirement's own
    route comments as static/js/mwl-doc-encrypt-upload.js) does not
    exist anywhere in the repo. mwl-crypto.js only exposes the
    encrypt/decrypt primitives; nothing calls them yet. So today, here
    exactly as in Wealth/Retirement, is_encrypted is always False and
    documents are stored as plain bytes — this is a real, pre-existing
    gap across the whole app, not something introduced or fixed by
    this batch. Flagged to Mohan; out of scope for Batch 9.3 to fix."""
    __tablename__ = "international_holding_document"
    __table_args__ = (
        db.Index("ix_intl_doc_holding", "holding_id"),
        db.Index("ix_intl_doc_user",    "user_id"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    holding_id = db.Column(db.Integer,
                            db.ForeignKey("international_holding.id"),
                            nullable=False)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    doc_type      = db.Column(db.String(50), nullable=False)  # DocumentType.ALL
    title         = db.Column(db.String(255), nullable=True)
    # ^ user-facing display name, distinct from the uploaded file's own
    #   filename — falls back to original_name when not provided
    original_name = db.Column(db.String(255), nullable=False)
    stored_name   = db.Column(db.String(255), nullable=False)
    file_path     = db.Column(db.String(500), nullable=False)
    file_size     = db.Column(db.Integer, nullable=True)
    notes         = db.Column(db.String(500), nullable=True)

    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    iv           = db.Column(db.String(64), nullable=True)
    is_encrypted = db.Column(db.Boolean, default=False, nullable=False)

    @property
    def display_name(self):
        return self.title or self.original_name

    @property
    def file_size_display(self):
        if not self.file_size:
            return "Unknown"
        if self.file_size < 1024:
            return f"{self.file_size} B"
        if self.file_size < 1024 * 1024:
            return f"{self.file_size / 1024:.1f} KB"
        return f"{self.file_size / (1024*1024):.1f} MB"

    def __repr__(self):
        return f"<InternationalHoldingDocument {self.original_name}>"


class InternationalTimeline(db.Model):
    """Batch 10.3 (Oct 2026) — audit history for every international
    holding, shown on the holding detail page. Append-only: services
    only ever add rows. Mirrors insurance_centre's InsuranceTimeline.
    Deleted together with its holding (ORM cascade) — an audit trail for
    something permanently deleted has nothing left to describe."""
    __tablename__ = "international_timeline"
    __table_args__ = (
        db.Index("ix_intl_timeline_holding", "holding_id"),
    )

    id          = db.Column(db.Integer, primary_key=True)
    holding_id  = db.Column(db.Integer, db.ForeignKey("international_holding.id"), nullable=False)
    user_id     = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    event_type  = db.Column(db.String(50), nullable=False)    # TimelineEvent constants
    description = db.Column(db.String(500), nullable=False)   # human-readable
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<InternationalTimeline {self.event_type} holding={self.holding_id}>"


class InternationalReminderAck(db.Model):
    """Batch 10.6 (Oct 2026) — remembers which reminders the user has
    marked done (or dismissed). Reminders themselves are NOT stored: they
    are computed fresh from holdings, remittances and today's date every
    time (see services.get_reminders), so they can never go stale. This
    table only records "the user has dealt with reminder X", keyed by a
    short period-specific string such as "schedule_fa:2025",
    "form67:2025" or "estate:over"."""
    __tablename__ = "international_reminder_ack"
    __table_args__ = (
        db.UniqueConstraint("user_id", "reminder_key", name="uq_intl_reminder_ack_user_key"),
    )

    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    reminder_key    = db.Column(db.String(60), nullable=False)
    acknowledged_at = db.Column(db.DateTime, default=datetime.utcnow)


class SbiTtbrRate(db.Model):
    """Batch 11 (Oct 2026) — the user's own "rate book": SBI TT buying
    rates (INR per 1 unit of `currency`) for specific dates, typed in or
    pasted from SBI's published card rates. Enter the month-end rates once
    and every transaction picks its rate up automatically (see rates.py).
    Per user because it is their reference data, never shared."""
    __tablename__ = "international_sbi_rate"
    __table_args__ = (
        db.UniqueConstraint("user_id", "currency", "rate_date", name="uq_intl_sbi_rate"),
        db.Index("ix_intl_sbi_rate_lookup", "user_id", "currency", "rate_date"),
    )

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    currency   = db.Column(db.String(3), nullable=False)
    rate_date  = db.Column(db.Date, nullable=False)
    rate       = db.Column(db.Float, nullable=False)
    note       = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class InternationalRateSettings(db.Model):
    """Batch 11 — the two conversion conventions practitioners disagree
    on, as per-user settings with documented defaults (rates.py)."""
    __tablename__ = "international_rate_settings"

    id       = db.Column(db.Integer, primary_key=True)
    user_id  = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True)
    fa_basis  = db.Column(db.String(20), nullable=False, default=RateBasis.SAME_DAY)
    cg_method = db.Column(db.String(20), nullable=False, default=CgMethod.SEPARATE)


class ScheduleFaYearInput(db.Model):
    """Batch 11 — figures for one holding and one calendar year that the
    app cannot work out by itself (it only keeps daily USD snapshots from
    the day tracking started): the broker's year-end value, the peak value
    and its date. All in the holding's own currency. Optional; when set
    they replace the snapshot-derived Schedule FA figures."""
    __tablename__ = "international_schedule_fa_input"
    __table_args__ = (
        db.UniqueConstraint("holding_id", "calendar_year", name="uq_intl_fa_input"),
    )

    id            = db.Column(db.Integer, primary_key=True)
    user_id       = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    holding_id    = db.Column(db.Integer, db.ForeignKey("international_holding.id"), nullable=False)
    calendar_year = db.Column(db.Integer, nullable=False)
    peak_date            = db.Column(db.Date, nullable=True)
    peak_value_native    = db.Column(db.Float, nullable=True)
    closing_value_native = db.Column(db.Float, nullable=True)
    notes         = db.Column(db.String(200), nullable=True)
