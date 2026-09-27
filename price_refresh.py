"""
Price Refresh — Stocks & Mutual Funds (production-readiness, Sep 2026)
==========================================================================
Answers two of the NaviPlan feature-parity gaps: a manual "Refresh
Prices" button, and an unattended `flask prices refresh` CLI command
for scheduled automatic refresh (Windows Task Scheduler, same pattern
as `flask wealth snapshot` and `flask backup run`).

Two data sources, matching what's already used elsewhere in this app:
  - Stocks: the same yfinance-based fetch already used at CAS/tradebook
    import time (see fetch_live_price_by_isin() in app.py) — reused
    here rather than duplicated, tried against the stock's own stored
    `ticker` first (known-good from a previous successful fetch) before
    falling back to a fresh ISIN/name-based lookup.
  - Mutual funds: AMFI's own daily NAVAll.txt bulk file
    (https://www.amfiindia.com/spages/NAVAll.txt) — the same fallback
    data source NaviPlan itself was flagged as using. It's AMFI's own
    free, public, no-key-required daily NAV file, keyed by AMFI scheme
    code (already stored on every MutualFund row as `amfi_code`).

Neither source is contacted per-row for mutual funds — the whole
NAVAll.txt file (~150k schemes) is downloaded ONCE per refresh run,
regardless of how many funds or users are being refreshed, and kept
in memory as a scheme_code -> nav dict for the duration of that run.

Failure handling: a single holding that can't be refreshed (no ticker
match, no amfi_code, scheme not found in that day's AMFI file, a stock
that's since been delisted) is skipped and counted, never raised — the
whole point of a scheduled/bulk refresh is that one bad row can't stop
everyone else's. Only a structural failure (AMFI file unreachable at
all) is raised, since that means the mutual-fund half of the refresh
genuinely couldn't happen at all this run.
"""
import re
import urllib.request
import urllib.error
from datetime import datetime


AMFI_NAVALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
AMFI_FETCH_TIMEOUT = 20  # seconds — NAVAll.txt is a few MB, plain text


class PriceRefreshError(Exception):
    """Raised only for a structural failure (e.g. AMFI file unreachable)."""
    pass


def parse_amfi_navall_text(text):
    """
    Parses AMFI's NAVAll.txt into {scheme_code: nav}. The file isn't a
    clean CSV — it's semicolon-delimited data rows interleaved with
    blank lines and section headers (fund-house names, "Open Ended
    Schemes(...)" category labels). A row is treated as real NAV data
    only when the first field is a plain integer scheme code —
    everything else (headers, blanks, the "Scheme Code;ISIN..." header
    line itself) is silently skipped rather than erroring, since that
    structure is normal for this file, not a corruption.

    AMFI has used two column layouts in the wild (confirmed against a
    real download, Sep 2026 — the classic 6-field layout with Plan and
    Option folded into "Scheme Name" isn't what they serve any more):
      6 fields: Scheme Code;ISIN Payout;ISIN Reinvest;Scheme Name;NAV;Date
      8 fields: Scheme Code;ISIN Payout;ISIN Reinvest;Scheme Name;Plan;Option;NAV;Date
    NAV is always the second-to-last field and Scheme Code always the
    first, in both, so both are supported by field count rather than
    hardcoding one layout — if AMFI adds yet another column the same
    way, this keeps working without a code change.
    """
    nav_map = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ";" not in line:
            continue
        fields = line.split(";")
        if len(fields) not in (6, 8):
            continue
        scheme_code = fields[0].strip()
        nav_raw = fields[-2].strip()
        if not scheme_code.isdigit():
            continue
        try:
            nav = float(nav_raw)
        except ValueError:
            continue
        if nav <= 0:
            continue
        nav_map[scheme_code] = nav
    return nav_map


def fetch_amfi_nav_map():
    """Downloads and parses the current AMFI NAVAll.txt. Raises PriceRefreshError on any network/parse problem."""
    try:
        req = urllib.request.Request(AMFI_NAVALL_URL, headers={"User-Agent": "MyWealthLens/1.0"})
        with urllib.request.urlopen(req, timeout=AMFI_FETCH_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as e:
        raise PriceRefreshError(f"Couldn't reach AMFI's NAV file: {e}")

    nav_map = parse_amfi_navall_text(text)
    if not nav_map:
        raise PriceRefreshError("AMFI's NAV file downloaded but no scheme rows could be parsed from it — the file's format may have changed.")
    return nav_map


def refresh_stock_price(stock):
    """
    Refreshes one Stock row's live_price/value/ticker/price_updated_at
    in place. Tries the stock's own stored `ticker` first (already
    known to have worked at a previous import/refresh), then falls
    back to the same ISIN/name-based lookup used at CAS import.
    Returns True if the price was updated, False otherwise (leaves the
    row untouched on failure — never zeroes out a value we couldn't
    refresh).
    """
    from app import fetch_live_price_by_isin
    import yfinance as yf

    price = None
    ticker = stock.ticker

    if ticker:
        try:
            t = yf.Ticker(ticker)
            p = float(t.fast_info.last_price)
            if p and p > 0:
                price = round(p, 2)
        except Exception:
            pass

    if price is None:
        resolved_ticker, p = fetch_live_price_by_isin(stock.isin, stock.name)
        if p:
            price = p
            ticker = resolved_ticker

    if price is None:
        return False

    stock.live_price = price
    stock.ticker = ticker
    stock.value = round(stock.quantity * price, 2)
    stock.price_updated_at = datetime.utcnow()
    return True


def refresh_mf_nav(mf, nav_map):
    """
    Refreshes one MutualFund row's nav/value/nav_updated_at in place
    from the given {scheme_code: nav} map. Returns one of:
      "updated"  — nav found and applied
      "no_code"  — this holding has no amfi_code to look up (can't
                   ever be refreshed this way; needs a CAS re-upload)
      "not_found" — has a code, but it wasn't in today's AMFI file
                    (rare — usually a matured/merged/inactive scheme)
    """
    if not mf.amfi_code:
        return "no_code"

    nav = nav_map.get(mf.amfi_code.strip())
    if nav is None:
        return "not_found"

    mf.nav = nav
    mf.value = round(mf.units * nav, 2)
    mf.nav_updated_at = datetime.utcnow()
    return "updated"


def refresh_all_prices(db, user_id=None):
    """
    Refreshes every Stock and MutualFund row for user_id, or for every
    user when user_id is None (the scheduled/CLI path). The AMFI file
    is downloaded once regardless of scope. Commits at the end of a
    successful run; on an AMFI failure, stock prices refreshed so far
    in this call are still committed (that half of the run still
    succeeded) and the summary reports the mutual-fund failure
    separately rather than discarding real work.

    Returns a summary dict:
      {stocks_updated, stocks_failed, mfs_updated, mfs_failed,
       mfs_no_code, amfi_error}
    `mfs_failed` is not_found; `mfs_no_code` is holdings that can
    never be refreshed this way (surfaced separately since the fix
    for those is different — re-upload the CAS, not "try again later").
    """
    from models import Stock, MutualFund

    summary = {
        "stocks_updated": 0, "stocks_failed": 0,
        "mfs_updated": 0, "mfs_failed": 0, "mfs_no_code": 0,
        "amfi_error": None,
    }

    stock_query = Stock.query if user_id is None else Stock.query.filter_by(user_id=user_id)
    for stock in stock_query.all():
        if refresh_stock_price(stock):
            summary["stocks_updated"] += 1
        else:
            summary["stocks_failed"] += 1

    mf_query = MutualFund.query if user_id is None else MutualFund.query.filter_by(user_id=user_id)
    mf_rows = mf_query.all()
    if mf_rows:
        try:
            nav_map = fetch_amfi_nav_map()
        except PriceRefreshError as e:
            summary["amfi_error"] = str(e)
            nav_map = None

        if nav_map is not None:
            for mf in mf_rows:
                result = refresh_mf_nav(mf, nav_map)
                if result == "updated":
                    summary["mfs_updated"] += 1
                elif result == "no_code":
                    summary["mfs_no_code"] += 1
                else:
                    summary["mfs_failed"] += 1

    db.session.commit()
    return summary
