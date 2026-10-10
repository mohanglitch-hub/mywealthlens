"""
International Investing Centre — report exports (Batch 10.4, Oct 2026)
======================================================================
PDF and CSV downloads for the five tax/compliance reports (Schedule FA,
Capital Gains, DTAA / Form 67, RSU-ESPP Perquisite, LRS Remittances)
plus the Dividend Income view added in 10.5.

Design: every report is first turned into ONE neutral `ReportData`
(title, period, columns, rows, totals, notes) by a builder function that
calls the SAME service function the HTML page uses — no figure is ever
recalculated here. The CSV and PDF writers then both render that one
object, so the two downloads (and the on-screen page) can't disagree.

Two PDF-specific traps this module handles on purpose:
  * ReportLab's base Helvetica cannot draw the rupee glyph (it isn't in
    WinAnsiEncoding) — the Sep 2026 glyph audit (tests/
    test_pdf_glyph_safety.py) exists because of exactly this. Amounts
    here are therefore always plain numbers with an explicit currency
    CODE in the column header ("Gain (USD)"), and every string is passed
    through pdf_safe() so a stray ₹, arrow or non-Latin holding name
    degrades to a safe character instead of a black box.
  * Reports are in fixed currencies (USD or INR, as the page shows) —
    NOT converted to the user's display-currency preference the way the
    dashboard totals are, because a tax working must match the page it
    came from.

CSV specifics: UTF-8 with a BOM so Excel on Windows reads it correctly;
numbers are written raw (no thousands separators) so they stay numeric in
a spreadsheet; dates are ISO; any text cell starting with = + - @ is
prefixed with an apostrophe so a holding named "=HYPERLINK(...)" can't
execute as a formula (CSV/formula injection).
"""
import csv
import io
from xml.sax.saxutils import escape as _xml_escape
from dataclasses import dataclass, field
from datetime import date, datetime

from international_centre import services
from international_centre.models import LRS_ANNUAL_LIMIT_USD
from international_centre.utils import fy_bounds
from wealth.timezone_utils import today_ist

# Column kinds drive both number formatting (PDF) and alignment.
TEXT, MONEY, QTY, PCT, DATE = "text", "money", "qty", "pct", "date"


@dataclass
class Column:
    header: str
    kind: str = TEXT
    width: float = 1.0          # relative weight in the PDF table


@dataclass
class ReportData:
    key: str                    # url/file slug, e.g. "schedule_fa"
    title: str
    period: str                 # e.g. "Calendar year 2025" / "FY 2025-26"
    columns: list
    rows: list                  # list of lists, raw python values (None = blank)
    totals: list = None         # optional list of total rows, each aligned with columns
    notes: list = field(default_factory=list)   # disclaimers / caveats
    filename_part: str = ""     # e.g. "2025" or "FY2025-26"


# ── Safe-text helpers ────────────────────────────────────────────────

# Characters people actually meet in this module that Helvetica/WinAnsi
# can't draw, mapped to readable ASCII. Anything else outside cp1252
# becomes "?" rather than a missing-glyph box.
_PDF_REPLACEMENTS = {
    "\u20b9": "Rs.", "\u2192": "->", "\u2190": "<-", "\u2265": ">=", "\u2264": "<=",
    "\u2248": "~", "\u2713": "ok", "\u2717": "x", "\u2022": "-", "\u00a0": " ",
    "\u2212": "-", "\u2011": "-",
}


def pdf_safe(value):
    """Make any value safe for ReportLab's base fonts."""
    if value is None:
        return ""
    text = str(value)
    for bad, good in _PDF_REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text.encode("cp1252", errors="replace").decode("cp1252")


def _p_text(value):
    """pdf_safe + XML-escape. ReportLab's Paragraph parses its text as
    markup, so a holding called "AT&T <Inc>" would otherwise raise a parse
    error and take the whole download down with it."""
    return _xml_escape(pdf_safe(value))


def csv_safe(value):
    """Neutralise spreadsheet formula injection in text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _sum(values):
    """Sum for a totals row, rounded to cents. Raw float addition gives
    things like 13253.009999999998, which would land in the CSV as-is."""
    return round(sum((v or 0) for v in values), 2)


def _iso(d):
    if isinstance(d, (date, datetime)):
        return d.isoformat()
    if isinstance(d, float):
        return round(d, 6)   # strips float noise; keeps real precision (quantities need > 2dp)
    return d


def _fmt(value, kind):
    """Human formatting for the PDF (the CSV keeps raw values)."""
    if value is None or value == "":
        return "-"
    if kind == MONEY:
        return f"{value:,.2f}"
    if kind == QTY:
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if kind == PCT:
        return f"{value:.2f}%"
    if kind == DATE:
        return value.strftime("%d %b %Y") if isinstance(value, (date, datetime)) else str(value)
    return str(value)


# ── Report builders (each reuses the page's own service) ─────────────

_ASSUMPTION_NOTE = ("Tracking aid built from what you have recorded in MyWealthLens - not a filing document. "
                    "Confirm all figures with a Chartered Accountant before filing.")


_RATES_NOTE_ALL_OK = "Every rupee figure uses an SBI TT buying rate (typed on the entry or from the SBI rate book)."


def _rates_note(summary):
    """One line saying where the rupee figures' rates came from, for every report."""
    counts = summary["rates"]
    if counts["total"] and counts["official"] == counts["total"]:
        return _RATES_NOTE_ALL_OK
    if not counts["total"]:
        return "No currency conversion was needed for this period."
    return ("RATE BASIS: " + summary["rates_badge"] + ". An ECB estimate is a market reference rate, NOT the SBI TT "
            "buying rate a tax return needs, and a missing rate leaves the figure blank. Enter SBI rates in the "
            "SBI rate book in MyWealthLens, then download again.")


def _label_of(rate, label):
    return f"{rate:.4f} ({label})" if rate else label


def build_schedule_fa(user_id, year):
    s = services.get_schedule_fa_summary(user_id, year)
    rows = [[r["entity_name"], r["country"], r["entity_address"], r["entity_zip"], r["entity_nature"],
             r["acquisition_date"], r["initial_inr"], r["peak_inr"], r["peak_date"], r["closing_inr"],
             r["gross_paid_inr"], r["proceeds_inr"], r["rates_badge"],
             ("; ".join(r["missing"]) if r["missing"] else "")] for r in s["rows"]]
    notes = [_ASSUMPTION_NOTE, _rates_note(s),
             f"Layout follows Schedule FA, Table A3 (foreign equity and debt interests), one row per holding. "
             f"Rate dates: {s['fa_basis_label']} for the acquisition, peak, dividend and sale figures; the closing "
             "value uses 31 December itself. Confirm the form's exact date convention and the figures with a CA.",
             "Table A1 (bank accounts), A2 (brokerage-account totals) and B (property) are not covered by this export."]
    if s["any_incomplete"]:
        notes.append("Rows with an incomplete value history use the current value for peak/closing unless broker "
                     "figures were typed in; check them before use.")
    return ReportData(
        key="schedule_fa", title="Schedule FA Report",
        period=f"Calendar year {year} (1 Jan - 31 Dec {year})",
        columns=[Column("Entity", TEXT, 1.9), Column("Country", TEXT, 1.0), Column("Address", TEXT, 1.9),
                 Column("ZIP", TEXT, 0.95), Column("Nature", TEXT, 1.4), Column("Acquired", DATE, 1.0),
                 Column("Initial value (INR)", MONEY, 1.2), Column("Peak value (INR)", MONEY, 1.2),
                 Column("Peak date", DATE, 1.0), Column("Closing 31 Dec (INR)", MONEY, 1.2),
                 Column("Gross paid (INR)", MONEY, 1.1), Column("Sale proceeds (INR)", MONEY, 1.1),
                 Column("Rates", TEXT, 1.0), Column("Still needed", TEXT, 1.6)],
        rows=rows,
        totals=[["Total", "", "", "", "", "", s["total_initial_inr"], s["total_peak_inr"], "", s["total_closing_inr"],
                 s["total_gross_paid_inr"], s["total_proceeds_inr"], "", ""]] if rows else None,
        notes=notes, filename_part=str(year))


def build_capital_gains(user_id, fy_start_year):
    s = services.get_capital_gains_summary(user_id, fy_start_year)
    rows = [[g["holding"].name, g["sell_date"], g["acquisition_date"], g["quantity"], g["currency"],
             g["cost_basis_native"], g["proceeds_native"], g["gain_native"],
             g["cost_inr"], g["proceeds_inr"], g["gain_inr"],
             _label_of(g["acq_rate"], g["acq_rate_label"]), _label_of(g["sell_rate"], g["sell_rate_label"]),
             g["classification"]] for g in s["rows"]]
    notes = [_ASSUMPTION_NOTE, _rates_note(s),
             "Conversion method: " + s["cg_method_label"] + ". Practitioners differ on this point; confirm with a CA. "
             "Rates are for the last day of the month before each acquisition and sale (Rule 115).",
             "Lots are matched FIFO. LTCG = held more than 24 months (the module's classification for unlisted "
             "foreign shares/funds). The USD figures in the app are tracking approximations and are not in this export."]
    totals = [["LTCG total (INR)", "", "", None, "", None, None, None, None, None, s["ltcg_total_inr"], "", "", ""],
              ["STCG total (INR)", "", "", None, "", None, None, None, None, None, s["stcg_total_inr"], "", "", ""]] if rows else None
    return ReportData(
        key="capital_gains", title="Capital Gains (LTCG / STCG)", period=s["fy_label"],
        columns=[Column("Holding", TEXT, 2.0), Column("Sold", DATE, 1.0), Column("Acquired", DATE, 1.0),
                 Column("Qty", QTY, 0.8), Column("Ccy", TEXT, 0.5), Column("Cost basis", MONEY, 1.0),
                 Column("Proceeds", MONEY, 1.0), Column("Gain", MONEY, 0.9),
                 Column("Cost (INR)", MONEY, 1.1), Column("Proceeds (INR)", MONEY, 1.1), Column("Gain (INR)", MONEY, 1.1),
                 Column("Cost rate used", TEXT, 1.6), Column("Sale rate used", TEXT, 1.6), Column("Class", TEXT, 0.7)],
        rows=rows, totals=totals, notes=notes, filename_part=s["fy_label"].replace(" ", ""))


def build_dtaa(user_id, fy_start_year):
    s = services.get_dtaa_summary(user_id, fy_start_year)
    rows = [[r["holding"].name, r["country"], r["currency"], r["gross_native"], r["withheld_native"],
             r["net_native"], r["gross_inr"], r["withheld_inr"], r["rates_badge"]] for r in s["rows"]]
    notes = [_ASSUMPTION_NOTE, _rates_note(s),
             "Each dividend is converted at the SBI TT buying rate for the last day of the previous month (Rule 115, "
             "income). A holding's rupee total is blank if any of its dividends has no rate. Gross is shown as the net "
             "amount for older dividends where no gross/withholding was recorded. This lists the input figures for "
             "Form 67 (Form 44 from FY 2026-27); it does not compute the foreign tax credit."]
    return ReportData(
        key="dtaa", title="DTAA / Foreign Tax Credit (Form 67)", period=s["fy_label"],
        columns=[Column("Holding", TEXT, 2.2), Column("Country", TEXT, 1.2), Column("Ccy", TEXT, 0.6),
                 Column("Gross dividend", MONEY, 1.2), Column("Tax withheld", MONEY, 1.2),
                 Column("Net received", MONEY, 1.2), Column("Gross (INR)", MONEY, 1.3),
                 Column("Withheld (INR)", MONEY, 1.3), Column("Rates", TEXT, 1.2)],
        rows=rows,
        totals=[["Total (INR)", "", "", None, None, None, s["total_gross_inr"], s["total_withheld_inr"], ""]] if rows else None,
        notes=notes, filename_part=s["fy_label"].replace(" ", ""))


def build_vesting(user_id, fy_start_year):
    s = services.get_vesting_perquisite_summary(user_id, fy_start_year)
    rows = [[r["holding"].name, r["plan_type"], r["vest_date"], r["quantity"], r["currency"], r["fmv_native"],
             r["purchase_price_native"], r["perquisite_native"], r["perquisite_inr"],
             _label_of(r["rate"], r["rate_label"])] for r in s["rows"]]
    notes = [_ASSUMPTION_NOTE, _rates_note(s),
             "Perquisite = (fair market value - price paid) x quantity, taxed as salary in India at vest, converted at "
             "the SBI TT buying rate on the last day of the month before the vest month (Rule 115, salary). Your "
             "employer's Form 12BA / Form 16 figure is what counts for filing."]
    return ReportData(
        key="vesting", title="RSU / ESPP Perquisite", period=s["fy_label"],
        columns=[Column("Holding", TEXT, 2.0), Column("Plan", TEXT, 0.7), Column("Vest date", DATE, 1.0),
                 Column("Qty", QTY, 0.8), Column("Ccy", TEXT, 0.5), Column("FMV / unit", MONEY, 1.0),
                 Column("Paid / unit", MONEY, 1.0), Column("Perquisite", MONEY, 1.1),
                 Column("Perquisite (INR)", MONEY, 1.2), Column("SBI rate used", TEXT, 1.8)],
        rows=rows, totals=[["Total (INR)", "", None, None, "", None, None, None, s["total_perquisite_inr"], ""]] if rows else None,
        notes=notes, filename_part=s["fy_label"].replace(" ", ""))


def build_lrs(user_id, fy_start_year):
    anchor = date(fy_start_year, 4, 1)
    s = services.get_lrs_status(user_id, anchor_date=anchor)
    # Oldest first reads better on paper than the page's newest-first.
    remits = sorted(s["remittances"], key=lambda r: (r.date, r.id))
    rows = [[r.date, r.amount_inr, r.amount_usd, r.tcs_amount_inr,
             r.purpose + (" (loan-funded)" if r.education_loan_funded else ""), r.remitting_bank or ""]
            for r in remits]
    notes = [_ASSUMPTION_NOTE,
             f"LRS cap used here: USD {LRS_ANNUAL_LIMIT_USD:,.0f} per financial year (RBI's published limit; may change by "
             "notification). TCS is an ESTIMATE from the rules in force on each remittance date - your bank collects the "
             "real figure from your total across all banks, which this report cannot see.",
             f"Used: USD {s['total_usd']:,.2f} of {LRS_ANNUAL_LIMIT_USD:,.0f} ({s['pct_used']}%). "
             f"Estimated TCS for the year: INR {s['total_tcs_inr']:,.2f}."]
    return ReportData(
        key="lrs", title="LRS Remittances & TCS", period=s["fy_label"],
        columns=[Column("Date", DATE, 1.1), Column("Amount (INR)", MONEY, 1.3), Column("Amount (USD)", MONEY, 1.3),
                 Column("Est. TCS (INR)", MONEY, 1.2), Column("Purpose", TEXT, 2.4), Column("Bank", TEXT, 1.5)],
        rows=rows, totals=[["Total", _sum(r[1] for r in rows), _sum(r[2] for r in rows), _sum(r[3] for r in rows), "", ""]] if rows else None,
        notes=notes, filename_part=s["fy_label"].replace(" ", ""))


def build_dividends(user_id, fy_start_year):
    s = services.get_dividend_income(user_id, fy_start_year)
    rows = [[r["name"], r["country"], r["currency"], r["payments"], r["gross_native"], r["withheld_native"],
             r["net_native"], r["net_usd"], r["net_inr"], r["ttm_yield_pct"]] for r in s["rows"]]
    notes = [_ASSUMPTION_NOTE,
             "The INR figures convert each dividend at the SBI TT buying rate for the last day of the previous month "
             "(from a rate typed on the dividend, else your SBI rate book, else a labelled ECB estimate); the USD "
             "figure is a tracking approximation. Yield = dividends received in the last 12 months / current value, "
             "in the holding's own currency."]
    return ReportData(
        key="dividends", title="Dividend Income", period=s["fy_label"],
        columns=[Column("Holding", TEXT, 2.2), Column("Country", TEXT, 1.2), Column("Ccy", TEXT, 0.6),
                 Column("Payments", QTY, 0.8), Column("Gross", MONEY, 1.1), Column("Withheld", MONEY, 1.1),
                 Column("Net", MONEY, 1.1), Column("Net (USD)", MONEY, 1.1), Column("Net (INR)", MONEY, 1.2),
                 Column("Yield (12m)", PCT, 0.9)],
        rows=rows, totals=[["Total", "", "", None, None, None, None, s["total_net_usd"], s["total_net_inr"], None]] if rows else None,
        notes=notes, filename_part=s["fy_label"].replace(" ", ""))


# key -> (builder, kind of period argument)
REPORTS = {
    "schedule_fa": (build_schedule_fa, "year"),
    "capital_gains": (build_capital_gains, "fy"),
    "dtaa": (build_dtaa, "fy"),
    "vesting": (build_vesting, "fy"),
    "lrs": (build_lrs, "fy"),
    "dividends": (build_dividends, "fy"),
}


def default_period(kind):
    """Default `year` (calendar) or `fy` (FY start year) argument."""
    today = today_ist()
    return today.year if kind == "year" else fy_bounds(today)[0].year


def clamp_period(raw, kind):
    """Parse a user-supplied year, falling back to the default for junk or
    absurd values (so ?year=99999999 can't reach date() and 500)."""
    default = default_period(kind)
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return value if 1990 <= value <= default + 1 else default


def build_report(key, user_id, period):
    if key not in REPORTS:
        raise KeyError(key)
    builder, _kind = REPORTS[key]
    return builder(user_id, period)


# ── CSV ──────────────────────────────────────────────────────────────

def to_csv_bytes(report):
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow([c.header for c in report.columns])
    for row in report.rows:
        w.writerow([csv_safe(_iso(v)) if v is not None else "" for v in row])
    for total_row in (report.totals or []):
        w.writerow([csv_safe(_iso(v)) if v is not None else "" for v in total_row])
    # utf-8-sig writes the BOM Excel needs to read non-ASCII names correctly
    return buf.getvalue().encode("utf-8-sig")


# ── PDF ──────────────────────────────────────────────────────────────

def to_pdf_bytes(report, user_name):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ACCENT, LIGHT = colors.HexColor("#0F766E"), colors.HexColor("#F0FDF9")
    BORDER, DARK, MUTED = colors.HexColor("#E2E8F0"), colors.HexColor("#0F172A"), colors.HexColor("#64748B")

    brand = ParagraphStyle("Brand", fontSize=10, textColor=ACCENT, fontName="Helvetica-Bold", spaceAfter=4)
    title = ParagraphStyle("Title", fontSize=20, textColor=DARK, fontName="Helvetica-Bold", leading=24, spaceAfter=4)
    sub = ParagraphStyle("Sub", fontSize=9.5, textColor=MUTED, leading=14)
    cell = ParagraphStyle("Cell", fontSize=7.5, textColor=DARK, leading=9.5)
    cell_r = ParagraphStyle("CellR", parent=cell, alignment=2)
    head = ParagraphStyle("Head", fontSize=7.5, textColor=colors.white, fontName="Helvetica-Bold", leading=9.5)
    head_r = ParagraphStyle("HeadR", parent=head, alignment=2)
    tot = ParagraphStyle("Tot", parent=cell, fontName="Helvetica-Bold")
    tot_r = ParagraphStyle("TotR", parent=tot, alignment=2)
    note = ParagraphStyle("Note", fontSize=7.5, textColor=MUTED, leading=10.5, spaceAfter=3)

    page = landscape(A4)
    usable = page[0] - 3.2 * cm
    generated = datetime.now().strftime("%d %b %Y, %I:%M %p")

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(1.6 * cm, 0.9 * cm, pdf_safe("Generated locally by MyWealthLens - personal use only - a tracking aid, "
                                                      "not a filing document"))
        canvas.drawRightString(page[0] - 1.6 * cm, 0.9 * cm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=page, leftMargin=1.6 * cm, rightMargin=1.6 * cm, topMargin=1.5 * cm,
                            bottomMargin=1.7 * cm, title=pdf_safe(f"MyWealthLens - {report.title}"),
                            author=pdf_safe(user_name))

    story = [Paragraph("MyWealthLens - International Investing", brand),
             Paragraph(_p_text(report.title), title),
             Paragraph(_p_text(f"{report.period}  |  Prepared for {user_name}  |  Generated {generated}"), sub),
             Spacer(1, 0.25 * cm), HRFlowable(width="100%", color=ACCENT, thickness=1.5), Spacer(1, 0.3 * cm)]

    if report.rows:
        weights = [c.width for c in report.columns]
        widths = [usable * w / sum(weights) for w in weights]
        right = {i for i, c in enumerate(report.columns) if c.kind in (MONEY, QTY, PCT)}

        def para(text, style_l, style_r, i):
            return Paragraph(_p_text(text), style_r if i in right else style_l)

        data = [[para(c.header, head, head_r, i) for i, c in enumerate(report.columns)]]
        for row in report.rows:
            data.append([para(_fmt(v, report.columns[i].kind), cell, cell_r, i) for i, v in enumerate(row)])
        for total_row in (report.totals or []):
            data.append([para("" if v is None else (_fmt(v, report.columns[i].kind) if not isinstance(v, str) else v),
                              tot, tot_r, i) for i, v in enumerate(total_row)])
        tbl = Table(data, colWidths=widths, repeatRows=1)
        style = [("BACKGROUND", (0, 0), (-1, 0), ACCENT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]), ("GRID", (0, 0), (-1, -1), 0.3, BORDER),
                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                 ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]
        if report.totals:
            first_total = len(data) - len(report.totals)
            style += [("LINEABOVE", (0, first_total), (-1, first_total), 1, ACCENT),
                      ("BACKGROUND", (0, first_total), (-1, -1), colors.white)]
        tbl.setStyle(TableStyle(style))
        story.append(tbl)
    else:
        story.append(Paragraph("No records for this period.", sub))

    story += [Spacer(1, 0.4 * cm), HRFlowable(width="100%", color=BORDER, thickness=0.5), Spacer(1, 0.2 * cm)]
    for n in report.notes:
        story.append(Paragraph(_p_text("- " + n), note))

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def export_filename(report, ext):
    part = (report.filename_part or "").replace("/", "-")
    return f"mywealthlens_{report.key}_{part}.{ext}".replace("__", "_")
