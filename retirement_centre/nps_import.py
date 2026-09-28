"""
NPS CRA Statement CSV Import (Batch 4, Sep 2026)
====================================================
Bulk-imports NPS contribution history from a "Statement of Transaction"
CSV exported from either NPS Central Recordkeeping Agency portal
(Protean/NSDL CRA at cra-nsdl.com, or KFintech's KCRA) into
RetirementContribution rows for one scheme, so an NPS subscriber
doesn't have to type in years of contributions by hand before a
per-scheme XIRR (see retirement_xirr.py) means anything.

Deliberately NOT the same multi-step "upload -> map columns -> review
-> confirm" flow as Cashflow Centre's generic bank-statement import
(cashflow_centre/csv_import.py) -- a bank statement's columns vary
wildly by bank, but an NPS CRA export's columns are drawn from a much
smaller, more predictable set across the two CRAs, so this is a
single-step "detect headers, parse, skip anything not a genuine
contribution, dedupe, import" flow instead -- closer to how the
existing CAS import works. If the headers can't be confidently
detected, this refuses to guess and reports exactly what it found
instead, since guessing wrong on someone's contribution history is
worse than asking them to check the file.

SCOPE (explicit, matching this app's convention of flagging what's
deliberately left out rather than silently underserving it): NPS
splits every contribution across up to four underlying asset classes
(Equity/Corporate Bonds/Government Securities/Alternative Assets),
each with its own NAV and unit count. Neither RetirementScheme nor
RetirementContribution models that split -- this import treats the
TOTAL contribution amount per transaction as a single deposit, matching
how every other scheme type in this app is already tracked (one
balance, one contribution history, no sub-asset-class breakdown).
Units/NAV columns in the source file, if present, are ignored.

This import does NOT touch RetirementScheme.current_balance. That
field is maintained separately (via the Edit Scheme form, or by each
individual manual "Add Contribution" going forward) and importing a
YEAR of historical contributions in bulk would otherwise inflate it by
their sum on top of whatever the user already has recorded there --
see add_contribution() in services.py for where a single new
contribution SHOULD move the balance (a live event happening now,
not a backfill of the past).
"""
import csv
import io
from datetime import datetime

MAX_CSV_BYTES = 2 * 1024 * 1024  # 2 MB — a multi-year NPS statement is a few hundred KB at most
MAX_ROWS = 5000  # generous headroom over a working lifetime of contributions

# Header aliases seen across NSDL/Protean CRA and KFintech (KCRA) CSV
# exports, matched case-insensitively as a substring of the header —
# same defensive-but-simple approach as AMFI's NAVAll.txt parser
# (price_refresh.py) and Cashflow's guess_mapping().
DATE_HEADER_KEYWORDS = ["transaction date", "txn date", "posting date", "value date", "date"]
AMOUNT_HEADER_KEYWORDS = ["contribution amount", "transaction amount", "txn amount", "amount (rs.)", "amount(rs.)", "amount"]
DESC_HEADER_KEYWORDS = ["transaction type", "txn type", "transaction description", "particulars", "description", "narration"]

# Row classification, by substring match on the description column
# (case-insensitive). Checked in this order: an excluded keyword wins
# even if "contribution" also appears (e.g. a reversed contribution is
# money coming back, not a fresh deposit).
EXCLUDE_KEYWORDS = ["revers", "refund", "switch", "withdrawal", "annuity", "charge", "tax", "redemption"]
INTEREST_KEYWORDS = ["interest"]
CONTRIBUTION_KEYWORDS = ["contribution"]

DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d-%b-%Y", "%d %b %Y", "%m/%d/%Y"]


class NpsImportError(Exception):
    """Raised for problems with the file itself, before any row is parsed."""
    pass


def _decode(file_bytes):
    if len(file_bytes) > MAX_CSV_BYTES:
        raise NpsImportError(f"That file is larger than the {MAX_CSV_BYTES // (1024*1024)} MB limit for a statement import.")
    try:
        return file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise NpsImportError("Couldn't read that file as text. Please export the CRA statement as a CSV and try again.")


def _find_header(headers_lower, keywords):
    for i, h in enumerate(headers_lower):
        if any(kw in h for kw in keywords):
            return i
    return None


def _parse_date(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(raw):
    cleaned = (raw or "").strip().replace(",", "").replace("₹", "")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def _classify(description):
    """Returns 'deposit', 'interest', or 'excluded' (see module docstring)."""
    text = (description or "").lower()
    if any(kw in text for kw in EXCLUDE_KEYWORDS):
        return "excluded"
    if any(kw in text for kw in INTEREST_KEYWORDS):
        return "interest"
    if any(kw in text for kw in CONTRIBUTION_KEYWORDS):
        return "deposit"
    return "unrecognized"


def parse_nps_cra_csv(file_bytes):
    """
    Parses an NPS CRA statement CSV into a list of candidate
    contribution dicts: {row_num, date, amount, description,
    classification, error}. Raises NpsImportError only for a
    file-level problem (unreadable, empty, no recognizable date/amount
    columns at all) -- a single bad ROW is recorded with an error and
    simply not imported, never allowed to stop the rest of the file.
    """
    text = _decode(file_bytes)
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise NpsImportError("That file appears to be empty.")

    headers = rows[0]
    headers_lower = [h.strip().lower() for h in headers]
    date_col = _find_header(headers_lower, DATE_HEADER_KEYWORDS)
    amount_col = _find_header(headers_lower, AMOUNT_HEADER_KEYWORDS)
    desc_col = _find_header(headers_lower, DESC_HEADER_KEYWORDS)

    if date_col is None or amount_col is None:
        raise NpsImportError(
            "Couldn't find a date and an amount column in this file's headers "
            f"({', '.join(headers) or 'no headers found'}). "
            "This doesn't look like an NPS CRA transaction statement export -- "
            "please double-check the file, or let Mohan know the exact column names "
            "so the import can be adjusted."
        )

    data_rows = [r for r in rows[1:] if any(cell.strip() for cell in r)]
    if not data_rows:
        raise NpsImportError("That file has a header row but no data rows.")
    if len(data_rows) > MAX_ROWS:
        raise NpsImportError(f"That file has {len(data_rows)} rows — please split it into batches of {MAX_ROWS} or fewer.")

    def cell(row, idx):
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    results = []
    for i, row in enumerate(data_rows, start=1):
        date_raw = cell(row, date_col)
        amount_raw = cell(row, amount_col)
        description = cell(row, desc_col) or None

        parsed_date = _parse_date(date_raw)
        amount = _parse_amount(amount_raw)
        error = None
        if parsed_date is None:
            error = f"Couldn't parse date '{date_raw}'."
        elif amount is None:
            error = f"Couldn't parse amount '{amount_raw}'."

        classification = _classify(description) if not error else None

        results.append({
            "row_num": i,
            "date": parsed_date,
            "amount": amount,
            "description": description,
            "classification": classification,
            "error": error,
        })

    return results


def import_nps_contributions(db, scheme, user_id, file_bytes):
    """
    Parses file_bytes (see parse_nps_cra_csv) and inserts new
    RetirementContribution rows for every row classified as a genuine
    deposit or interest credit, skipping:
      - rows with a parse error (bad date/amount)
      - rows classified 'excluded' (switch, withdrawal, tax, reversal, ...)
      - rows classified 'unrecognized' (description didn't match any
        known contribution/interest/excluded keyword — reported
        separately so nothing is silently dropped without a count)
      - rows that duplicate a contribution already on this scheme
        (same date + amount + entry_type). Uses a multiset match (a
        Counter) rather than a plain set, so two genuinely separate
        contributions of the same amount on the same day (a real,
        if unusual, possibility) aren't wrongly collapsed into one —
        only re-importing the SAME row from an overlapping statement
        period gets skipped.

    Does NOT touch scheme.current_balance — see module docstring.
    Returns a summary dict and never raises for row-level problems
    (only NpsImportError, from parse_nps_cra_csv, for a file-level one).
    """
    from collections import Counter
    from .models import RetirementContribution, RetirementTimeline, RetirementTimelineEvent, ContributionEntryType

    rows = parse_nps_cra_csv(file_bytes)

    existing = (RetirementContribution.query
                .filter_by(scheme_id=scheme.id)
                .with_entities(RetirementContribution.contribution_date,
                               RetirementContribution.amount,
                               RetirementContribution.entry_type)
                .all())
    seen = Counter((d, round(a, 2), t) for d, a, t in existing)

    summary = {
        "imported": 0, "skipped_duplicate": 0, "skipped_excluded": 0,
        "skipped_unrecognized": 0, "skipped_invalid": 0,
    }

    entry_type_by_classification = {
        "deposit": ContributionEntryType.DEPOSIT,
        "interest": ContributionEntryType.INTEREST,
    }

    for row in rows:
        if row["error"]:
            summary["skipped_invalid"] += 1
            continue
        if row["classification"] == "excluded":
            summary["skipped_excluded"] += 1
            continue
        if row["classification"] == "unrecognized":
            summary["skipped_unrecognized"] += 1
            continue

        entry_type = entry_type_by_classification[row["classification"]]
        key = (row["date"], round(row["amount"], 2), entry_type)
        if seen[key] > 0:
            seen[key] -= 1
            summary["skipped_duplicate"] += 1
            continue

        contribution = RetirementContribution(
            scheme_id=scheme.id, user_id=user_id,
            contribution_date=row["date"], amount=row["amount"],
            entry_type=entry_type,
            note=(f"Imported from NPS CRA statement: {row['description']}"
                  if row["description"] else "Imported from NPS CRA statement"),
        )
        db.session.add(contribution)
        summary["imported"] += 1

    if summary["imported"]:
        timeline = RetirementTimeline(
            scheme_id=scheme.id, user_id=user_id,
            event_type=RetirementTimelineEvent.CONTRIBUTION_ADDED,
            description=(f"Imported {summary['imported']} contribution(s) from an NPS CRA statement "
                         f"({summary['skipped_duplicate']} already on file, skipped)"),
        )
        db.session.add(timeline)

    db.session.commit()
    return summary
