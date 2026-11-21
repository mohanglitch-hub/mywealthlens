#!/usr/bin/env python3
"""
mask_cas.py - make a parsed CAS (CAMS / KFintech / NSDL / CDSL statement) safe to share.
==========================================================================================

WHAT IT DOES
  Reads the JSON that `casparser` produces from a statement, removes everything that
  identifies you, and writes a new "_masked.json" with the SAME structure. It is built for
  sharing a sample so a parser/importer can be developed against real statement shapes.

  Masked (replaced by placeholders):
    your name, email, mobile, address, PAN, folio numbers (mapped to F0001, F0002 ... so
    "same folio" stays "same folio"), folio-holder names, NOMINEE names, advisor/ARN codes,
    demat DP-id / client-id, NPS PRAN, UCC, and any gift-folio number.
  Scrubbed from free text (transaction descriptions, warnings ...): emails, PANs, phone
    numbers, any 6+ digit number, your folio numbers, and every word of your names.
  Scaled: every unit count and rupee amount is multiplied by the same whole number
    (random 2-9, or --factor N). NAVs, prices and percentages are NOT changed, so
    balances still add up exactly and NAV x units still equals value - but the file no
    longer reveals the size of your portfolio.
  Kept as-is (public information): fund and AMC names, ISINs, AMFI codes, dates.

  SAFETY: after masking it re-scans the result. If anything that looks personal is still
  there, it prints where (never the value) and writes NOTHING. It also refuses to write
  inside a git repository, because committing a statement to a public repo is exactly the
  mistake this tool exists to prevent.

HOW TO USE (Windows PowerShell; keep these files OUTSIDE your project folder)
  1. Parse your statement to JSON (needs your CAS password):
        casparser "C:\\path\\to\\statement.pdf" -p YOUR_PASSWORD -o "C:\\path\\statement.json"
  2. Mask it:
        py mask_cas.py "C:\\path\\statement.json"
     -> writes C:\\path\\statement_masked.json and prints a summary
  3. Open the _masked.json in a text editor and skim it. Only then share it.
  4. Delete the unmasked statement.json when you no longer need it.

  Other options:
     --no-scale        keep the real amounts/units (only identity is masked)
     --factor N        use this whole-number multiplier instead of a random one
     --check FILE      only scan an existing file for anything that looks personal
     --self-test       prove on your own machine that this script works

Needs only Python 3.8+ - no extra packages.
"""
import argparse
import json
import random
import re
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ── patterns ─────────────────────────────────────────────────────────
PAN_RE = re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
LONG_DIGITS_RE = re.compile(r"(?<![\d.])\d{6,}(?![\d.])")
WORD_RE = re.compile(r"[^\W\d_]{3,}")   # letters in any script, 3 or more
STOPWORDS = {"and", "the", "mrs", "miss", "shri", "smt", "dr", "mr", "ms", "ltd", "pvt", "huf"}

# ── field knowledge (from casparser's own data model) ────────────────
ALWAYS_MASK = {                      # key -> placeholder
    "email": "masked@example.com", "mobile": "0000000000", "address": "Masked address",
    "PAN": "XXXXX0000X", "pan": "XXXXX0000X", "pran": "000000000000",
    "dp_id": "00000000", "client_id": "00000000", "ucc": "MASKED", "advisor": "ARN-000000",
}
FOLIO_KEYS = {"folio", "gift_folio"}
IDENT_KEYS = set(ALWAYS_MASK) - {"address"}
SCALE_KEYS = {"amount", "units", "balance", "open", "close", "close_calculated", "cost", "value",
              "num_shares", "num_bonds", "total_cost", "pnl"}
PUBLIC_KEYS = {"scheme", "amc", "isin", "amfi", "rta", "rta_code", "type", "symbol", "exchange", "date",
               "from_", "to", "cas_type", "file_type", "nps_sp", "fund_manager", "tier", "asset_class",
               "coupon_frequency", "maturity_date", "KYC", "PANKYC"}
PERSON_HINTS = {"PAN", "pan", "schemes", "accounts", "folios", "owners", "equities", "mutual_funds",
                "bonds", "email", "mobile", "address"}
NOT_PERSON = {"isin", "num_shares", "num_bonds"}      # a dict with these is a security, not a person


def is_person(d):
    keys = set(d)
    return bool(keys & PERSON_HINTS) and not (keys & NOT_PERSON)


class Masker:
    def __init__(self, scale_factor):
        self.factor = scale_factor
        self.names, self.folios, self.idents = set(), set(), set()
        self.folio_parts = {}            # a digit/letter chunk of a folio number -> the full folio it came from
        self.holder_ids, self.folio_ids = {}, {}
        self.counts = {}
        self.unscaled_numeric_keys = set()

    def _count(self, what, n=1):
        self.counts[what] = self.counts.get(what, 0) + n

    # -- pass 1: learn what the identifying values are -------------------------
    def collect(self, node):
        if isinstance(node, dict):
            person = is_person(node)
            for k, v in node.items():
                if k == "name" and person and isinstance(v, str):
                    self._add_name(v)
                elif k == "nominees" and isinstance(v, list):
                    for n in v:
                        if isinstance(n, str):
                            self._add_name(n)
                elif k in FOLIO_KEYS and isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip():
                    self._add_folio(str(v).strip())
                elif k in IDENT_KEYS and isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip():
                    self.idents.add(str(v).strip())
                self.collect(v)
        elif isinstance(node, list):
            for x in node:
                self.collect(x)

    def _add_folio(self, text):
        self.folios.add(text)
        for part in re.findall(r"[A-Za-z0-9]{5,}", text):
            if part != text:
                self.folio_parts[part] = text

    def folio_tokens(self):
        """Every string that should be treated as a folio number: the full value and its main chunks."""
        tokens = {f: f for f in self.folios}
        tokens.update(self.folio_parts)
        return tokens

    def _add_name(self, text):
        for w in WORD_RE.findall(text):
            if w.lower() not in STOPWORDS:
                self.names.add(w.lower())

    # -- text scrubbing ---------------------------------------------------------
    def scrub(self, s):
        out = s
        # 1. exact identifiers first (a phone number must not be half-eaten by a folio replacement)
        for ident in sorted(self.idents, key=len, reverse=True):
            if len(ident) >= 4:
                out, n = re.subn(re.escape(ident), "MASKED", out, flags=re.I)
                self._count("identifiers in text", n)
        # 2. anything shaped like an email, PAN or phone number
        for rx, tag in ((EMAIL_RE, "masked@example.com"), (PAN_RE, "XXXXX0000X"), (PHONE_RE, "0000000000")):
            out, n = rx.subn(tag, out)
            self._count("emails/PANs/phones in text", n)
        # 3. folio numbers, matched whole (never inside a longer run of digits or letters)
        for token, orig in sorted(self.folio_tokens().items(), key=lambda kv: len(kv[0]), reverse=True):
            out, n = re.subn(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", self._folio_id(orig), out)
            self._count("folio numbers in text", n)
        # 4. every word of your names
        if self.names:
            def swap(m):
                return "NAME" if m.group(0).lower() in self.names else m.group(0)
            swapped = WORD_RE.sub(swap, out)
            if swapped != out:
                self._count("name words in text")
            out = swapped
        # 5. any remaining long whole number (account numbers, etc.); decimals like 20000.00 are left alone
        out, n = LONG_DIGITS_RE.subn("<REDACTED-NUMBER>", out)
        self._count("long numbers in text", n)
        return out

    def _folio_id(self, orig):
        if orig not in self.folio_ids:
            self.folio_ids[orig] = f"F{len(self.folio_ids) + 1:04d}"
        return self.folio_ids[orig]

    def _holder_id(self, orig, first_is_investor=False):
        key = " ".join(w.lower() for w in WORD_RE.findall(orig)) or orig.lower()
        if key not in self.holder_ids:
            self.holder_ids[key] = "Investor" if (first_is_investor and not self.holder_ids) else f"Holder {len(self.holder_ids) + 1}"
        return self.holder_ids[key]

    # -- numbers ----------------------------------------------------------------
    def scale(self, v):
        if self.factor == 1 or v is None or isinstance(v, bool):
            return v
        try:
            if isinstance(v, str):
                d = Decimal(v.replace(",", ""))
                self._count("numbers scaled")
                return format(d * self.factor, "f")
            if isinstance(v, (int, Decimal)):
                self._count("numbers scaled")
                return v * self.factor
        except InvalidOperation:
            return v
        return v

    # -- pass 2: build the masked copy -------------------------------------------
    def mask(self, node, under_investor_info=False, key=None):
        if isinstance(node, list):
            return [self.mask(x, key=key) for x in node]
        if isinstance(node, str):                      # a string sitting directly in a list (e.g. parse_warnings)
            return node if key in PUBLIC_KEYS else self.scrub(node)
        if not isinstance(node, dict):
            return node
        person = is_person(node)
        out = {}
        for k, v in node.items():
            if k == "name" and person and isinstance(v, str):
                out[k] = self._holder_id(v, first_is_investor=under_investor_info)
                self._count("names")
            elif k in ALWAYS_MASK and isinstance(v, (str, int)) and not isinstance(v, bool):
                out[k] = ALWAYS_MASK[k]
                self._count(f"{k} fields")
            elif k in FOLIO_KEYS and isinstance(v, (str, int)) and not isinstance(v, bool):
                out[k] = self._folio_id(str(v).strip()) if str(v).strip() else v
                self._count("folio fields")
            elif k == "nominees" and isinstance(v, list):
                out[k] = [f"Nominee {i}" for i in range(1, len(v) + 1)]
                self._count("nominees", len(v))
            elif k in SCALE_KEYS and not isinstance(v, (dict, list)):
                out[k] = self.scale(v)
            elif isinstance(v, (dict, list)):
                out[k] = self.mask(v, under_investor_info=(k == "investor_info"), key=k)
            elif isinstance(v, str) and k not in PUBLIC_KEYS:
                t = v.strip()
                if re.fullmatch(r"-?\d+(?:\.\d+)?", t) and ("." in t or len(t.lstrip("-")) < 6):
                    self.unscaled_numeric_keys.add(k)      # a price/NAV/rate stored as text: kept, but listed for review
                    out[k] = v
                else:
                    out[k] = self.scrub(v)
            else:
                if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
                    self.unscaled_numeric_keys.add(k)
                out[k] = v
        return out


# ── verification: scan a result for anything that still looks personal ─────────
PLACEHOLDERS = ("XXXXX0000X", "masked@example.com", "0000000000", "<REDACTED-NUMBER>")


def _strings(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}/{k} (field name)", str(k)
            yield from _strings(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def verify(masked, masker=None):
    """Returns a list of (path, kind). Empty list means nothing suspicious was found.
    With a masker it also checks for the original names, folios and identifiers."""
    problems = []
    folio_rx = []
    if masker is not None:
        folio_rx = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])") for t in masker.folio_tokens()]
    for path, text in _strings(masked):
        probe = text
        for ph in PLACEHOLDERS:
            probe = probe.replace(ph, "")
        last_key = path.split(" (field name)")[0].split("/")[-1].split("[")[0]
        if PAN_RE.search(probe):
            problems.append((path, "looks like a PAN"))
        if EMAIL_RE.search(probe):
            problems.append((path, "looks like an email address"))
        if PHONE_RE.search(probe):
            problems.append((path, "looks like a phone number"))
        if masker is None:
            continue
        low = text.lower()
        if any(rx.search(text) for rx in folio_rx):
            problems.append((path, "contains an original folio number"))
        for ident in masker.idents:
            if len(ident) >= 4 and ident.lower() in low:
                problems.append((path, "contains an original identifier"))
        if last_key not in PUBLIC_KEYS:
            for w in WORD_RE.findall(text):
                if w.lower() in masker.names:
                    problems.append((path, "contains a word from your name"))
                    break
    return problems


def in_git_repo(path):
    for p in [path] + list(path.parents):
        if (p / ".git").exists():
            return p
    return None


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f, parse_float=Decimal)


def dump_json(data, path):
    def enc(o):
        if isinstance(o, Decimal):
            return float(o)
        raise TypeError(f"cannot serialise {type(o)}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=enc)


def mask_data(data, factor):
    m = Masker(factor)
    m.collect(data)
    masked = m.mask(data)
    return masked, m


# ── self-test with an invented statement ──────────────────────────────────────
def _sample():
    return {
        "statement_period": {"from": "01-Apr-2025", "to": "31-Mar-2026"},
        "file_type": "CAMS", "cas_type": "DETAILED",
        "investor_info": {"name": "Rahul Kumar Sharma", "email": "rahul.sharma@gmail.com",
                          "address": "12 MG Road, Bengaluru 560001", "mobile": "9876543210"},
        "folios": [{
            "folio": "12345678 / 45", "amc": "HDFC Mutual Fund", "name": "RAHUL KUMAR SHARMA",
            "PAN": "ABCDE1234F", "KYC": "OK", "PANKYC": "OK",
            "schemes": [{
                "scheme": "HDFC Flexi Cap Fund - Direct Plan - Growth", "advisor": "ARN-123456",
                "rta_code": "HDFC123", "rta": "CAMS", "type": "EQUITY", "isin": "INF179K01UT0", "amfi": "101762",
                "nominees": ["Priya Sharma", "Anil Sharma"],
                "open": "0.000", "close": "30.500", "close_calculated": "30.500",
                "valuation": {"date": "2026-03-31", "nav": "1500.1234", "cost": "40000.00", "value": "45753.77"},
                "transactions": [
                    {"date": "2025-05-01", "description": "Purchase - folio 12345678 for RAHUL SHARMA, mob 9876543210",
                     "amount": "20000.00", "units": "15.250", "nav": "1311.4754", "balance": "15.250", "type": "PURCHASE",
                     "dividend_rate": None, "gift_folio": None},
                    {"date": "2025-09-01", "description": "SIP via sharma.r@work.com PAN ABCDE1234F",
                     "amount": "20000.00", "units": "15.250", "nav": "1311.4754", "balance": "30.500", "type": "PURCHASE_SIP",
                     "dividend_rate": None, "gift_folio": "87654321"},
                ]}]}],
        "parse_warnings": ["Could not match opening balance for folio 12345678 / 45"],
    }


SENSITIVE_SAMPLE_TEXT = ["Rahul", "Sharma", "SHARMA", "ABCDE1234F", "9876543210", "12345678", "87654321",
                         "rahul.sharma", "sharma.r@work", "ARN-123456", "Priya", "Anil", "MG Road", "560001"]


def self_test():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + name)
        ok = ok and cond

    original = _sample()
    masked, m = mask_data(json.loads(json.dumps(original), parse_float=Decimal), 3)
    text = json.dumps(masked, default=float)
    check("no original personal text survives anywhere", not [s for s in SENSITIVE_SAMPLE_TEXT if s in text])
    check("verification finds nothing in the masked result", verify(masked, m) == [])

    def shape(x):
        if isinstance(x, dict):
            return {k: shape(v) for k, v in x.items()}
        if isinstance(x, list):
            return [shape(v) for v in x]
        return None
    check("structure (keys and list lengths) is identical", shape(original) == shape(masked))
    s0, o0 = masked["folios"][0]["schemes"][0], original["folios"][0]["schemes"][0]
    check("fund name, ISIN, AMFI, dates and NAV are unchanged",
          (s0["scheme"], s0["isin"], s0["amfi"], s0["valuation"]["nav"], s0["transactions"][0]["date"])
          == (o0["scheme"], o0["isin"], o0["amfi"], o0["valuation"]["nav"], o0["transactions"][0]["date"]))
    check("units and amounts are scaled by exactly 3",
          s0["close"] == "91.500" and s0["transactions"][0]["amount"] == "60000.00")
    tx = s0["transactions"]
    check("balances still add up exactly", Decimal(tx[0]["balance"]) + Decimal(tx[1]["units"]) == Decimal(tx[1]["balance"]))
    check("the same folio keeps the same placeholder everywhere",
          masked["folios"][0]["folio"] == "F0001" and "F0001" in tx[0]["description"] and "F0001" in masked["parse_warnings"][0])
    check("nominees are masked but counted", s0["nominees"] == ["Nominee 1", "Nominee 2"])
    check("the investor and the same-named folio holder get the same placeholder",
          masked["investor_info"]["name"] == "Investor" and masked["folios"][0]["name"] == "Investor")

    leaky = json.loads(json.dumps(masked), parse_float=Decimal)
    leaky["folios"][0]["schemes"][0]["scheme"] = "Axis Fund PQRST9876Z"
    check("verification catches a deliberately planted PAN", ("/folios[0]/schemes[0]/scheme", "looks like a PAN") in verify(leaky, m))
    check("--check mode (patterns only) catches a planted email",
          bool(verify({"x": {"note": "write to a.b@c.in"}})))

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        (repo / ".git").mkdir(parents=True)
        check("refuses to write inside a git repository", in_git_repo(repo / "out.json") is not None)
        check("allows a normal folder", in_git_repo(Path(td) / "elsewhere" / "out.json") is None)
    print("\nSELF-TEST PASSED" if ok else "\nSELF-TEST FAILED")
    return 0 if ok else 1


# ── command line ──────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="Mask personal data in a parsed CAS JSON so it is safe to share.")
    ap.add_argument("input", nargs="?", help="the JSON produced by casparser")
    ap.add_argument("-o", "--output", help="where to write the masked file (default: <input>_masked.json)")
    ap.add_argument("--no-scale", action="store_true", help="keep real amounts and units")
    ap.add_argument("--factor", type=int, help="whole-number multiplier for units/amounts (default: random 2-9)")
    ap.add_argument("--allow-in-repo", action="store_true", help="allow writing inside a git repository (not recommended)")
    ap.add_argument("--check", metavar="FILE", help="only scan FILE for anything that looks personal")
    ap.add_argument("--self-test", action="store_true", help="run the built-in self-test")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.check:
        problems = verify(load_json(args.check))
        if problems:
            print(f"{len(problems)} thing(s) look personal in {args.check}:")
            for path, kind in problems[:30]:
                print(f"   {path}: {kind}")
            return 1
        print("Nothing that looks like a PAN, email address or phone number was found.")
        return 0
    if not args.input:
        ap.print_help()
        return 2

    src = Path(args.input)
    if not src.is_file():
        print(f"Cannot find {src}")
        return 2
    out = Path(args.output) if args.output else src.with_name(src.stem + "_masked.json")
    repo = in_git_repo(out.resolve())
    if repo and not args.allow_in_repo:
        print(f"REFUSING to write {out}: it is inside a git repository ({repo}).")
        print("Move the statement files to a folder outside your project (for example the Desktop) and run again.")
        return 2
    if in_git_repo(src.resolve()):
        print("WARNING: the input file is inside a git repository. Move it out and delete it from there.")

    try:
        data = load_json(src)
    except (OSError, ValueError) as e:
        print(f"Could not read {src} as JSON: {e}")
        return 2
    factor = 1 if args.no_scale else (args.factor if args.factor and args.factor > 0 else random.randint(2, 9))
    masked, m = mask_data(data, factor)

    problems = verify(masked, m)
    if problems:
        print(f"STOPPED: {len(problems)} spot(s) still look personal after masking, so nothing was written:")
        for path, kind in problems[:30]:
            print(f"   {path}: {kind}")
        print("Send me this message (it contains no personal values) and I will extend the script.")
        return 1

    dump_json(masked, out)
    print(f"Wrote {out}")
    print(f"  amounts and units multiplied by {factor}" if factor != 1 else "  amounts and units left as they are (--no-scale)")
    for what, n in sorted(m.counts.items()):
        print(f"  masked {n} x {what}")
    if m.unscaled_numeric_keys:
        print("  numeric fields left unchanged: " + ", ".join(sorted(m.unscaled_numeric_keys)))
    print("Next: open the file, skim it, and only then share it. Delete the unmasked JSON afterwards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
