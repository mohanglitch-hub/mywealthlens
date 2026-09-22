"""
Cashflow Centre — CSV Import
==============================
Implements the design agreed with Mohan (see the "Cashflow CSV Import —
Design Spec" doc): generic CSV + manual column mapping for v1, no
per-bank presets. Nothing here ever touches the database directly —
parsing/preview is pure, and the actual DB write happens in
services.py via the existing create_transaction() validation path, so
imported rows can never bypass the rules manual entry enforces.

Flow (see routes.py):
  1. Upload  -> parse_csv_header() shows columns + a few sample rows,
                guess_mapping() pre-fills a best-guess mapping.
  2. Map     -> parse_csv_rows() turns every data row into a candidate
                transaction (or an error), using the user's confirmed
                column mapping and date format.
  3. Review  -> mark_duplicates() flags candidates that look like an
                existing transaction, for the user to opt into anyway.
  4. Confirm -> routes.py validates each surviving row with the normal
                validate_transaction() and inserts them in one commit.

The raw CSV text is round-tripped through hidden form fields between
steps (no server-side temp files) — statements are small, and nothing
about someone's bank data sits on disk between requests, in keeping
with the app's local-first, minimal-footprint approach.
"""
import csv
import io
from datetime import datetime

from cashflow_centre.models import Transaction, TransactionType

MAX_CSV_BYTES = 2 * 1024 * 1024  # 2 MB — a statement CSV is a few hundred KB at most
MAX_ROWS = 2000  # generous headroom over a year of daily transactions

# Date formats we try to auto-detect and offer, in the order tried.
DATE_FORMATS = [
    ("%d/%m/%Y", "DD/MM/YYYY"),
    ("%d-%m-%Y", "DD-MM-YYYY"),
    ("%m/%d/%Y", "MM/DD/YYYY"),
    ("%Y-%m-%d", "YYYY-MM-DD"),
    ("%d/%m/%y", "DD/MM/YY"),
    ("%d %b %Y", "DD Mon YYYY"),
]
DATE_FORMAT_CHOICES = [fmt for fmt, _ in DATE_FORMATS]


class CsvImportError(Exception):
    """Raised for problems with the file itself (not row-level errors)."""
    pass


def _decode(file_bytes):
    """Decode uploaded bytes as UTF-8 (with BOM tolerance), or raise."""
    if len(file_bytes) > MAX_CSV_BYTES:
        raise CsvImportError(
            f"That file is larger than the {MAX_CSV_BYTES // (1024*1024)} MB limit for a statement import."
        )
    try:
        return file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvImportError(
            "Couldn't read that file as text. Please export the statement as a CSV (not Excel/PDF) and try again."
        )


def parse_csv_header(file_bytes):
    """
    Returns {headers: [...], sample_rows: [[...], ...], row_count: int,
    guessed_mapping: {...}, guessed_date_format: str or None}
    or raises CsvImportError for an empty/unreadable file.
    """
    text = _decode(file_bytes)
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise CsvImportError("That file appears to be empty.")

    headers = rows[0]
    data_rows = [r for r in rows[1:] if any(cell.strip() for cell in r)]
    if not data_rows:
        raise CsvImportError("That file has a header row but no data rows.")
    if len(data_rows) > MAX_ROWS:
        raise CsvImportError(
            f"That file has {len(data_rows)} rows — please split it into batches of {MAX_ROWS} or fewer."
        )

    guessed_mapping = guess_mapping(headers)
    guessed_date_format = guess_date_format([r[guessed_mapping["date_col"]] for r in data_rows[:10]
                                              if guessed_mapping["date_col"] is not None
                                              and len(r) > guessed_mapping["date_col"]])

    return {
        "headers": headers,
        "sample_rows": data_rows[:5],
        "row_count": len(data_rows),
        "guessed_mapping": guessed_mapping,
        "guessed_date_format": guessed_date_format,
    }


def guess_mapping(headers):
    """
    Best-effort column guess from header names. Always returns a dict
    with every key present (None where no guess was found) — the user
    confirms or corrects every field before anything is parsed.
    """
    lower = [h.strip().lower() for h in headers]

    def find(*keywords):
        for i, h in enumerate(lower):
            if any(kw in h for kw in keywords):
                return i
        return None

    date_col = find("date")
    desc_col = find("description", "narration", "particulars", "details", "payee", "remarks")
    debit_col = find("debit", "withdrawal", "dr")
    credit_col = find("credit", "deposit", "cr")
    amount_col = find("amount", "value")

    mode = "split" if (debit_col is not None and credit_col is not None) else "single"
    if mode == "single" and amount_col is None:
        amount_col = find("debit", "credit")  # fall back to whichever single one exists

    return {
        "date_col": date_col,
        "desc_col": desc_col,
        "mode": mode,
        "amount_col": amount_col,
        "debit_col": debit_col,
        "credit_col": credit_col,
        "negative_is_expense": True,
    }


def guess_date_format(sample_values):
    """Try each known format against the sample values; return the first that parses all of them."""
    samples = [v.strip() for v in sample_values if v and v.strip()]
    if not samples:
        return None
    for fmt, _ in DATE_FORMATS:
        try:
            for v in samples:
                datetime.strptime(v, fmt)
            return fmt
        except ValueError:
            continue
    return None


def _parse_amount(raw):
    """'-1,234.50' / '1234.5' -> float, or raises ValueError."""
    cleaned = (raw or "").strip().replace(",", "").replace("₹", "")
    if not cleaned:
        raise ValueError("empty")
    return float(cleaned)


def parse_csv_rows(file_bytes, mapping, date_format):
    """
    Parses every data row into a candidate transaction dict:
      {row_num, date (date obj or None), date_raw, description, amount
       (float or None), type, error (str or None)}
    Row-level errors never stop the rest of the file (see design spec
    §6) — a bad row is flagged and simply excluded from import unless
    the user fixes it.
    """
    text = _decode(file_bytes)
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    data_rows = [r for r in rows[1:] if any(cell.strip() for cell in r)]

    date_col = mapping.get("date_col")
    desc_col = mapping.get("desc_col")
    mode = mapping.get("mode", "single")
    amount_col = mapping.get("amount_col")
    debit_col = mapping.get("debit_col")
    credit_col = mapping.get("credit_col")
    negative_is_expense = mapping.get("negative_is_expense", True)

    results = []
    for i, row in enumerate(data_rows, start=1):
        def cell(idx):
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        date_raw = cell(date_col)
        description = cell(desc_col) or None

        parsed_date, error = None, None
        if not date_raw:
            error = "Missing date."
        else:
            try:
                parsed_date = datetime.strptime(date_raw, date_format).date()
            except ValueError:
                error = f"Couldn't parse date '{date_raw}' as {date_format}."

        amount, txn_type = None, None
        if not error:
            try:
                if mode == "split":
                    debit_raw, credit_raw = cell(debit_col), cell(credit_col)
                    if debit_raw:
                        amount = abs(_parse_amount(debit_raw))
                        txn_type = TransactionType.EXPENSE
                    elif credit_raw:
                        amount = abs(_parse_amount(credit_raw))
                        txn_type = TransactionType.INCOME
                    else:
                        error = "No debit or credit amount on this row."
                else:
                    raw_amount = _parse_amount(cell(amount_col))
                    if raw_amount == 0:
                        error = "Amount is zero."
                    else:
                        is_negative = raw_amount < 0
                        txn_type = (TransactionType.EXPENSE if is_negative == negative_is_expense
                                    else TransactionType.INCOME)
                        amount = abs(raw_amount)
            except ValueError:
                error = "Couldn't read the amount on this row."

        results.append({
            "row_num": i,
            "date": parsed_date,
            "date_raw": date_raw,
            "description": description,
            "amount": amount,
            "type": txn_type,
            "error": error,
        })

    return results


def mark_duplicates(user_id, candidates):
    """
    Flags each candidate row with is_duplicate: True when an existing
    transaction already matches on (user, date, amount, type) — the
    heuristic agreed in the design spec. Errored rows are left alone.
    Mutates and returns the same list.
    """
    valid = [c for c in candidates if not c["error"]]
    if not valid:
        return candidates

    dates = {c["date"] for c in valid}
    existing = Transaction.query.filter(
        Transaction.user_id == user_id,
        Transaction.date.in_(dates),
    ).all()
    existing_keys = {(e.date, round(e.amount, 2), e.type) for e in existing}

    for c in valid:
        c["is_duplicate"] = (c["date"], round(c["amount"], 2), c["type"]) in existing_keys
    return candidates
