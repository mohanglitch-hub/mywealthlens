"""
International Investing Centre — SBI TT buying-rate engine (Batch 11, Oct 2026)
==============================================================================
Indian tax figures for foreign assets must be in rupees, converted at the
State Bank of India's telegraphic-transfer BUYING rate (SBI TTBR), not at a
market or ECB reference rate. Until this batch the reports converted with a
single cached "today" rate (or Frankfurter's ECB rate), which is fine for
tracking but wrong for a tax working. This module is the one place that
decides, for any foreign-currency amount on any date, WHICH INR rate to use
and WHERE it came from.

Order of precedence for one conversion (first hit wins):

  1. override   - a rate typed against that very transaction / vesting event
                  (e.g. a figure the CA supplied). Beats everything.
  2. rate book  - the user's own table of SBI rates (SbiTtbrRate). Enter the
                  month-end rates once and every event picks them up.
                  A rate-book entry is accepted for a target date if it is
                  dated ON that date or up to MAX_GAP_DAYS earlier - the
                  usual "last published rate before the date" practice for
                  weekends and bank holidays - but never an older, stale one.
  3. estimate   - Frankfurter's ECB reference rate for the target date.
                  NOT an SBI rate. Always labelled as an estimate, and the
                  reports say so, so an estimate can't pass for a tax rate.
  4. missing    - nothing usable; the report shows a blank, never a guess.

Which DATE's rate applies is a convention, and the sources disagree on parts
of it, so it is explicit and configurable instead of buried:

  * SAME_DAY         - the rate on the event's own date (what the ITR form's
                       instructions say for Schedule FA values: the date of
                       investment, of the peak, of closing).
  * PREV_MONTH_END   - the rate on the last day of the month BEFORE the event
                       (Rule 115's "specified date" for salary - which is how
                       an RSU perquisite is taxed - and for capital gains and
                       other income).

Defaults used by the reports (documented in the CA brief, overridable):
  Schedule FA event-date figures ... per user setting `fa_basis` (SAME_DAY)
  Schedule FA closing value ........ always 31 December itself
  Dividends / Form 67 .............. PREV_MONTH_END
  RSU/ESPP perquisite .............. PREV_MONTH_END
  Capital gains .................... PREV_MONTH_END, converted per user
                                     setting `cg_method` (cost and proceeds
                                     separately, or one rate on the gain)

None of this is tax advice; the module is a calculator with its working
shown. Rule 115 is, per one source, carried forward as Rule 206 under the
Income-tax Act, 2025 from FY 2026-27 - the convention itself is unchanged.
"""
import re
import threading
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from fx_rates import fetch_fx_rate, FxRateError, SUPPORTED_CURRENCIES
from international_centre.models import (
    SbiTtbrRate, InternationalRateSettings, RateBasis, CgMethod,
)
from wealth.timezone_utils import today_ist

# ── Constants ────────────────────────────────────────────────────────

MAX_GAP_DAYS = 5            # how far back a rate-book entry may be and still count for a date
MIN_RATE, MAX_RATE = 0.001, 5000.0     # INR per 1 unit; sanity bounds (JPY ~0.6 ... KWD ~280)
MIN_RATE_DATE = date(2000, 1, 1)
MAX_IMPORT_LINES = 2000
ESTIMATE_FAILURES_BEFORE_GIVING_UP = 3  # stop hammering a dead network mid-report

SOURCE_OVERRIDE = "override"
SOURCE_RATE_BOOK = "rate_book"
SOURCE_ESTIMATE = "estimate"
SOURCE_MISSING = "missing"
SOURCE_INR = "inr"
OFFICIAL_SOURCES = {SOURCE_OVERRIDE, SOURCE_RATE_BOOK, SOURCE_INR}

Settings = namedtuple("Settings", "fa_basis cg_method")
DEFAULT_SETTINGS = Settings(RateBasis.SAME_DAY, CgMethod.SEPARATE)

BASIS_LABELS = {
    RateBasis.SAME_DAY: "Rate on the event's own date",
    RateBasis.PREV_MONTH_END: "Rate on the last day of the previous month",
}
METHOD_LABELS = {
    CgMethod.SEPARATE: "Convert cost and sale proceeds separately, each at its own date's rate",
    CgMethod.SINGLE: "Work out the gain in the foreign currency, then convert once at the sale rate",
}


# ── Pure helpers ─────────────────────────────────────────────────────

def last_day_of_previous_month(d):
    return d.replace(day=1) - timedelta(days=1)


def specified_date(event_date, basis):
    """The date whose SBI rate applies to an event on `event_date`."""
    if basis == RateBasis.PREV_MONTH_END:
        return last_day_of_previous_month(event_date)
    return event_date


@dataclass
class RateResult:
    rate: float            # INR per 1 unit of the currency; None when missing
    source: str            # SOURCE_*
    target_date: date      # the date the convention asked for
    date_used: date        # the date the rate is actually dated (rate book / estimate)

    @property
    def ok(self):
        return self.rate is not None

    @property
    def official(self):
        return self.source in OFFICIAL_SOURCES and self.rate is not None

    def label(self):
        if self.source == SOURCE_INR:
            return "INR (no conversion)"
        if self.source == SOURCE_OVERRIDE:
            return "SBI rate typed for this entry"
        if self.source == SOURCE_RATE_BOOK:
            when = f"{self.date_used:%d %b %Y}"
            return f"SBI rate book, {when}"
        if self.source == SOURCE_ESTIMATE:
            return f"ECB estimate, {self.date_used:%d %b %Y} (not SBI)"
        return f"No rate for {self.target_date:%d %b %Y}"


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d %b %Y", "%d-%b-%Y", "%d %B %Y", "%d-%b-%y", "%d/%m/%y")


def parse_any_date(text):
    text = (text or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_rate_value(text):
    """'84.50', '₹84.50', 'Rs. 84.50', '1,02.5' -> float, or None."""
    cleaned = re.sub(r"[₹\s]|rs\.?|inr", "", (text or "").strip(), flags=re.I).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def check_rate_bounds(currency, rate_date, rate):
    """Shared validation. Returns a list of error strings."""
    errors = []
    cur = (currency or "").upper().strip()
    if cur == "INR":
        errors.append("INR needs no conversion rate.")
    elif cur not in SUPPORTED_CURRENCIES:
        errors.append(f"{cur or 'That currency'} is not a supported currency.")
    if rate_date is None:
        errors.append("Please enter a valid date.")
    elif rate_date > today_ist():
        errors.append("A rate cannot be dated in the future.")
    elif rate_date < MIN_RATE_DATE:
        errors.append("That date is too far in the past.")
    if rate is None:
        errors.append("Please enter a valid rate.")
    elif not (MIN_RATE <= rate <= MAX_RATE):
        errors.append(f"A rate of {rate:g} looks wrong; expected rupees per 1 unit of the currency "
                      f"(between {MIN_RATE:g} and {MAX_RATE:g}).")
    return errors


def parse_rate_lines(text):
    """Pure parser for a pasted rate list, one 'date <sep> rate' per line.
    Separators: comma, tab, semicolon, or (failing those) the last run of
    spaces. Blank lines and '#' comments are skipped. Returns
    (rows, errors): rows = [(date, rate)], errors = [(line_number, message)]."""
    rows, errors = [], []
    lines = (text or "").splitlines()
    if len(lines) > MAX_IMPORT_LINES:
        return [], [(0, f"Too many lines ({len(lines)}); paste at most {MAX_IMPORT_LINES} at a time.")]
    for n, raw in enumerate(lines, start=1):
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        # drop currency decorations ("₹", "Rs.", "INR") so "31 Oct 2025 ₹ 84.10" still splits cleanly
        line = re.sub(r"(₹|\brs\.?|\binr\b)", " ", line, flags=re.I).strip()
        m = re.search(r"[,;\t]", line)
        if m:
            date_part, rate_part = line[:m.start()], line[m.end():]
        else:
            parts = line.rsplit(None, 1)
            if len(parts) != 2:
                errors.append((n, "Expected a date and a rate on the same line."))
                continue
            date_part, rate_part = parts
        d, r = parse_any_date(date_part), parse_rate_value(rate_part)
        if d is None:
            errors.append((n, f"Couldn't read the date '{date_part.strip()[:30]}'."))
        elif r is None:
            errors.append((n, f"Couldn't read the rate '{rate_part.strip()[:30]}'."))
        else:
            rows.append((d, r))
    return rows, errors


# ── Settings ─────────────────────────────────────────────────────────

def get_settings(user_id):
    row = InternationalRateSettings.query.filter_by(user_id=user_id).first()
    if not row:
        return DEFAULT_SETTINGS
    return Settings(
        row.fa_basis if row.fa_basis in RateBasis.ALL else DEFAULT_SETTINGS.fa_basis,
        row.cg_method if row.cg_method in CgMethod.ALL else DEFAULT_SETTINGS.cg_method,
    )


def save_settings(db, user_id, fa_basis, cg_method):
    """Returns an error string, or None on success."""
    if fa_basis not in RateBasis.ALL:
        return "Please choose a valid Schedule FA rate date."
    if cg_method not in CgMethod.ALL:
        return "Please choose a valid capital-gains conversion method."
    row = InternationalRateSettings.query.filter_by(user_id=user_id).first()
    if row:
        row.fa_basis, row.cg_method = fa_basis, cg_method
    else:
        db.session.add(InternationalRateSettings(user_id=user_id, fa_basis=fa_basis, cg_method=cg_method))
    db.session.commit()
    return None


# ── Rate book ────────────────────────────────────────────────────────

def save_rate(db, user_id, currency, rate_date, rate, note=None):
    """Insert or update the rate for (currency, date). Returns
    (errors, action) where action is 'added' / 'updated' / None."""
    cur = (currency or "").upper().strip()
    errors = check_rate_bounds(cur, rate_date, rate)
    if errors:
        return errors, None
    note = (note or "").strip()[:100] or None
    row = SbiTtbrRate.query.filter_by(user_id=user_id, currency=cur, rate_date=rate_date).first()
    if row:
        row.rate, row.note = rate, note
        action = "updated"
    else:
        db.session.add(SbiTtbrRate(user_id=user_id, currency=cur, rate_date=rate_date, rate=rate, note=note))
        action = "added"
    db.session.commit()
    return [], action


def import_rates(db, user_id, currency, text):
    """Bulk add from pasted text. Valid lines are saved even if others fail,
    and every failure is reported with its line number. Returns
    {'added', 'updated', 'errors': [(line, msg)]}."""
    cur = (currency or "").upper().strip()
    result = {"added": 0, "updated": 0, "errors": []}
    pre = check_rate_bounds(cur, today_ist(), 1.0)   # validates only the currency here
    pre = [e for e in pre if "currency" in e or "INR" in e]
    if pre:
        result["errors"].append((0, pre[0]))
        return result
    rows, parse_errors = parse_rate_lines(text)
    result["errors"].extend(parse_errors)
    # last value wins if a date is repeated within the paste
    deduped = {}
    for d, r in rows:
        deduped[d] = r
    existing = {x.rate_date: x for x in SbiTtbrRate.query.filter_by(user_id=user_id, currency=cur).all()}
    for d, r in sorted(deduped.items()):
        errs = check_rate_bounds(cur, d, r)
        if errs:
            result["errors"].append((0, f"{d:%d %b %Y}: {errs[0]}"))
            continue
        if d in existing:
            existing[d].rate = r
            result["updated"] += 1
        else:
            db.session.add(SbiTtbrRate(user_id=user_id, currency=cur, rate_date=d, rate=r, note="imported"))
            result["added"] += 1
    db.session.commit()
    return result


def delete_rate(db, user_id, rate_id):
    row = SbiTtbrRate.query.filter_by(id=rate_id, user_id=user_id).first()
    if not row:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def list_rates(user_id, currency=None):
    q = SbiTtbrRate.query.filter_by(user_id=user_id)
    if currency:
        q = q.filter_by(currency=currency.upper())
    return q.order_by(SbiTtbrRate.currency, SbiTtbrRate.rate_date.desc()).all()


# ── The resolver ─────────────────────────────────────────────────────

_ESTIMATE_CACHE = {}        # (currency, target_date) -> (rate, actual_date)  - past rates never change
_ESTIMATE_LOCK = threading.Lock()


def clear_estimate_cache():
    with _ESTIMATE_LOCK:
        _ESTIMATE_CACHE.clear()


class RateResolver:
    """Resolves INR rates for one user across a whole report. Create one per
    report run: it loads the rate book once, remembers which rates it could
    not find (`needed`), and counts where each rate came from (`summary`).

    allow_estimate=False is "collect mode" (used to build the list of rates
    the user still has to enter): nothing touches the network."""

    def __init__(self, user_id, allow_estimate=True):
        self.user_id = user_id
        self.allow_estimate = allow_estimate
        self.estimates_unavailable = False
        self._failures = 0
        self._failed_targets = set()
        self._book = {}
        for r in SbiTtbrRate.query.filter_by(user_id=user_id).order_by(SbiTtbrRate.rate_date).all():
            self._book.setdefault(r.currency, []).append((r.rate_date, r.rate))
        self.results = []
        self.needed = {}    # (currency, target_date) -> set(purpose)

    # -- lookups --
    def _book_lookup(self, currency, target):
        best = None
        for d, rate in self._book.get(currency, []):
            if d <= target and (target - d).days <= MAX_GAP_DAYS:
                best = (d, rate)       # list is date-ascending, so the last match is the latest
        return best

    def _estimate_lookup(self, currency, target):
        key = (currency, target)
        with _ESTIMATE_LOCK:
            if key in _ESTIMATE_CACHE:
                return _ESTIMATE_CACHE[key]
        if not self.allow_estimate or self.estimates_unavailable or key in self._failed_targets:
            return None
        try:
            rate, actual = fetch_fx_rate(currency, "INR", target)
        except FxRateError:
            self._failures += 1
            self._failed_targets.add(key)
            if self._failures >= ESTIMATE_FAILURES_BEFORE_GIVING_UP:
                self.estimates_unavailable = True
            return None
        with _ESTIMATE_LOCK:
            _ESTIMATE_CACHE[key] = (rate, actual)
        return rate, actual

    def prefetch(self, items):
        """items: iterable of (currency, event_date, basis, override). Fetches
        every needed estimate concurrently so a report doesn't pay for
        them one at a time."""
        if not self.allow_estimate:
            return
        todo = set()
        for cur, event_date, basis, override in items:
            cur = (cur or "").upper()
            if cur == "INR" or (override or 0) > 0:
                continue
            target = specified_date(event_date, basis)
            if self._book_lookup(cur, target):
                continue
            with _ESTIMATE_LOCK:
                if (cur, target) in _ESTIMATE_CACHE:
                    continue
            todo.add((cur, target))
        if not todo:
            return

        def one(pair):
            try:
                return pair, fetch_fx_rate(pair[0], "INR", pair[1])
            except FxRateError:
                return pair, None

        with ThreadPoolExecutor(max_workers=min(6, len(todo))) as pool:
            for pair, got in pool.map(one, sorted(todo)):
                if got is not None:
                    with _ESTIMATE_LOCK:
                        _ESTIMATE_CACHE[pair] = got
                else:
                    self._failed_targets.add(pair)
                    self._failures += 1
        if self._failures >= ESTIMATE_FAILURES_BEFORE_GIVING_UP:
            self.estimates_unavailable = True

    # -- the public call --
    def resolve(self, currency, event_date, basis, override=None, purpose=None, override_date=None):
        cur = (currency or "").upper().strip()
        target = specified_date(event_date, basis)

        if cur == "INR":
            result = RateResult(1.0, SOURCE_INR, target, target)
        elif override is not None and override > 0:
            result = RateResult(override, SOURCE_OVERRIDE, target, override_date or target)
        else:
            hit = self._book_lookup(cur, target)
            if hit:
                result = RateResult(hit[1], SOURCE_RATE_BOOK, target, hit[0])
            else:
                est = self._estimate_lookup(cur, target)
                if est:
                    result = RateResult(est[0], SOURCE_ESTIMATE, target, est[1])
                else:
                    result = RateResult(None, SOURCE_MISSING, target, None)

        if not result.official:
            self.needed.setdefault((cur, target), set())
            if purpose:
                self.needed[(cur, target)].add(purpose)
        self.results.append(result)
        return result

    def convert(self, amount_native, currency, event_date, basis, override=None, purpose=None, override_date=None):
        """(amount_in_inr or None, RateResult)."""
        res = self.resolve(currency, event_date, basis, override=override, purpose=purpose, override_date=override_date)
        if not res.ok or amount_native is None:
            return None, res
        return round(amount_native * res.rate, 2), res

    # -- reporting helpers --
    def summary(self):
        counts = {SOURCE_OVERRIDE: 0, SOURCE_RATE_BOOK: 0, SOURCE_ESTIMATE: 0, SOURCE_MISSING: 0}
        for r in self.results:
            if r.source in counts:
                counts[r.source] += 1
        counts["official"] = counts[SOURCE_OVERRIDE] + counts[SOURCE_RATE_BOOK]
        counts["total"] = counts["official"] + counts[SOURCE_ESTIMATE] + counts[SOURCE_MISSING]
        counts["all_official"] = counts["total"] > 0 and counts[SOURCE_ESTIMATE] == 0 and counts[SOURCE_MISSING] == 0
        return counts

    def needed_list(self):
        """[{'currency', 'target_date', 'purposes'}] oldest first."""
        return [{"currency": c, "target_date": d, "purposes": sorted(p)}
                for (c, d), p in sorted(self.needed.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def summarize_results(results):
    """Same counts as RateResolver.summary() for an arbitrary list of
    RateResult (used for per-row badges)."""
    counts = {SOURCE_OVERRIDE: 0, SOURCE_RATE_BOOK: 0, SOURCE_ESTIMATE: 0, SOURCE_MISSING: 0}
    for r in results:
        if r.source in counts:
            counts[r.source] += 1
    counts["official"] = counts[SOURCE_OVERRIDE] + counts[SOURCE_RATE_BOOK]
    counts["total"] = counts["official"] + counts[SOURCE_ESTIMATE] + counts[SOURCE_MISSING]
    return counts


def basis_badge(counts):
    """Short human text for a row/page: '3 SBI · 1 ECB est. · 1 missing'."""
    parts = []
    if counts["official"]:
        parts.append(f"{counts['official']} SBI")
    if counts[SOURCE_ESTIMATE]:
        parts.append(f"{counts[SOURCE_ESTIMATE]} ECB est.")
    if counts[SOURCE_MISSING]:
        parts.append(f"{counts[SOURCE_MISSING]} missing")
    return " · ".join(parts) or "—"
