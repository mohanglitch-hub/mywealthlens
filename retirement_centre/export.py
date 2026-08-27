"""
Retirement Centre — PDF Export
==================================
Phase D: Builds a complete, professionally formatted retirement
summary PDF for the current user only. Follows the same reportlab-
based architecture and dark/teal branding already used elsewhere in
MyWealthLens (app.py's export_pdf, insurance_centre's export_pdf).

Uses the Phase C maturity calculation service directly — never
duplicates the maturity formulas here (Section 31 of Phase D spec).

Contains document METADATA only, never the actual file bytes
(Section 30 of spec).
"""

from datetime import datetime as _dt
import io

from flask import send_file
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether,
)

from . import services
from .models import RetirementScheme, SchemeType
from .utils import format_inr, format_date

# ── Shared colours (matches app.py / insurance_centre's dark theme) ──────────
_TEAL   = colors.HexColor("#00d4aa")
_DARK   = colors.HexColor("#0d0f14")
_CARD   = colors.HexColor("#13161f")
_CARD2  = colors.HexColor("#1a1e2d")
_BORDER = colors.HexColor("#1e2130")
_WHITE  = colors.white
_MUTED  = colors.HexColor("#64748b")


def _styles():
    getSampleStyleSheet()  # ensure base registered
    def S(name, **kw):
        return ParagraphStyle(name, **kw)
    return {
        "title":    S("title", fontSize=22, textColor=_TEAL, leading=28, fontName="Helvetica-Bold"),
        "h1":       S("h1",    fontSize=13, textColor=_TEAL, leading=18, fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=4),
        "h2":       S("h2",    fontSize=10, textColor=_WHITE, leading=14, fontName="Helvetica-Bold", spaceAfter=2),
        "sub":      S("sub",   fontSize=9,  textColor=_MUTED, leading=14),
        "body":     S("body",  fontSize=8,  textColor=_WHITE, leading=12),
        "muted":    S("muted", fontSize=7.5, textColor=_MUTED, leading=11),
        "footer":   S("footer", fontSize=7, textColor=_MUTED, alignment=TA_CENTER),
    }


def _table_style(header_bg=None):
    hbg = header_bg or _CARD
    return TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0),  hbg),
        ("TEXTCOLOR",    (0, 0), (-1, 0),  _TEAL),
        ("FONTNAME",     (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, -1), 8),
        ("FONTNAME",     (0, 1), (-1, -1), "Helvetica"),
        ("TEXTCOLOR",    (0, 1), (-1, -1), _WHITE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_CARD, _CARD2]),
        ("GRID",         (0, 0), (-1, -1), 0.3, _BORDER),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING",   (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 5),
        ("VALIGN",       (0, 0), (-1, -1), "MIDDLE"),
    ])


def _dark_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(_DARK)
    canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
    canvas.restoreState()


def build_retirement_pdf(user):
    """
    Builds and returns a Flask send_file response containing the
    retirement summary PDF for `user` ONLY — every query below is
    scoped to user.id (Section 34 of spec: never expose another
    user's data).
    """
    stats   = services.RetirementStatisticsService(user.id)
    summary = stats.summary_dict()
    schemes = (RetirementScheme.query
               .filter_by(user_id=user.id, is_archived=False)
               .order_by(RetirementScheme.scheme_type).all())

    S = _styles()
    story = []
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
        title="MyWealthLens — Retirement Centre",
        author=user.name,
    )
    W = doc.width

    # ── Cover ──
    now_str = _dt.now().strftime("%d %B %Y, %I:%M %p")
    story += [
        Paragraph("MyWealthLens", S["title"]),
        Paragraph("Retirement Centre — Personal Summary", S["h1"]),
        Paragraph(f"Prepared for {user.name} ({user.email})", S["sub"]),
        Paragraph(f"Generated on {now_str}", S["sub"]),
        Spacer(1, 4*mm),
        HRFlowable(width="100%", color=_TEAL, thickness=1.5),
        Spacer(1, 4*mm),
    ]

    # ── Retirement Summary ──
    story.append(Paragraph("Retirement Summary", S["h1"]))
    story.append(Paragraph(
        "All figures reflect current balances and recorded contributions. "
        "Rates and return assumptions are informational and not guaranteed.",
        S["muted"]))
    story.append(Spacer(1, 2*mm))
    top_rows = [
        ["Total Retirement Balance", format_inr(summary["total_corpus"]),
         "Number of Schemes",        str(summary["active_schemes"])],
        ["Current FY Contributions", format_inr(summary["current_fy_contributions"]),
         "Upcoming Milestones",      str(len(summary["upcoming_milestones"]))],
    ]
    top_tbl = Table(top_rows, colWidths=[W*0.30, W*0.20, W*0.30, W*0.20])
    top_tbl.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,-1), _CARD),
        ("TEXTCOLOR",   (0,0), (0,-1),  _MUTED),
        ("TEXTCOLOR",   (2,0), (2,-1),  _MUTED),
        ("TEXTCOLOR",   (1,0), (1,-1),  _TEAL),
        ("TEXTCOLOR",   (3,0), (3,-1),  _TEAL),
        ("FONTNAME",    (1,0), (1,-1),  "Helvetica-Bold"),
        ("FONTNAME",    (3,0), (3,-1),  "Helvetica-Bold"),
        ("FONTSIZE",    (0,0), (-1,-1), 9),
        ("GRID",        (0,0), (-1,-1), 0.3, _BORDER),
        ("TOPPADDING",  (0,0), (-1,-1), 7),
        ("BOTTOMPADDING",(0,0),(-1,-1), 7),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
    ]))
    story += [top_tbl, Spacer(1, 4*mm)]

    # ── Category Summary ──
    if summary["category_breakdown"]:
        story.append(Paragraph("Scheme Type Breakdown", S["h1"]))
        cat_data = [["Scheme Type", "Schemes", "Current Balance", "Current FY Contributions"]]
        for c in summary["category_breakdown"]:
            cat_data.append([
                c["scheme_type"], str(c["count"]),
                format_inr(c["total_balance"]), format_inr(c["current_fy_contributions"]),
            ])
        cat_tbl = Table(cat_data, colWidths=[W*0.34, W*0.16, W*0.25, W*0.25])
        cat_tbl.setStyle(_table_style())
        story += [cat_tbl, Spacer(1, 4*mm)]

    # ── Individual Schemes ──
    story.append(Paragraph("Scheme Details", S["h1"]))

    for scheme in schemes:
        block = []
        block.append(HRFlowable(width="100%", color=_TEAL, thickness=0.5))
        block.append(Spacer(1, 1.5*mm))
        block.append(Paragraph(f"{scheme.display_type} — {scheme.institution or 'Institution not set'}", S["h2"]))

        core_rows = [
            ["Account Number", scheme.account_number or "—",
             "Status", scheme.status],
            ["Opening Date", format_date(scheme.opening_date),
             "Current Balance", format_inr(scheme.current_balance)],
            ["Balance Updated", format_date(scheme.balance_updated_at),
             "Growth Method", scheme.growth_method.replace("_", " ").title()],
        ]
        rate_display = (f"{scheme.rate_or_return_assumption}%"
                        if scheme.rate_or_return_assumption is not None else "—")
        core_rows.append(["Rate / Return Assumption", rate_display, "", ""])

        # Scheme-specific rows
        if scheme.scheme_type in (SchemeType.EPF, SchemeType.VPF):
            core_rows.append(["Employer", scheme.employer_name or "—",
                              "UAN", scheme.uan_number or "—"])
            core_rows.append(["Employee Contribution %",
                              f"{scheme.employee_contribution_pct}%" if scheme.employee_contribution_pct is not None else "—",
                              "Employer Contribution %",
                              f"{scheme.employer_contribution_pct}%" if scheme.employer_contribution_pct is not None else "—"])
            core_rows.append(["Target Retirement Year",
                              str(scheme.target_retirement_year or "—"), "", ""])
        elif scheme.scheme_type == SchemeType.PPF:
            core_rows.append(["Extension Opted",
                              "Yes" if scheme.extension_opted else "No", "", ""])
        elif scheme.scheme_type == SchemeType.NPS:
            core_rows.append(["PRAN", scheme.pran_number or "—",
                              "Tier", scheme.tier or "—"])
            core_rows.append(["Target Retirement Year",
                              str(scheme.target_retirement_year or "—"), "", ""])
        elif scheme.scheme_type == SchemeType.SSY:
            core_rows.append(["Girl Child Name", scheme.girl_child_name or "—",
                              "Date of Birth", format_date(scheme.girl_child_dob)])
        elif scheme.scheme_type == SchemeType.SUPERANNUATION:
            core_rows.append(["Employer", scheme.employer_name or "—",
                              "Target Retirement Year", str(scheme.target_retirement_year or "—")])

        core_tbl = Table(core_rows, colWidths=[W*0.28, W*0.22, W*0.28, W*0.22])
        core_tbl.setStyle(TableStyle([
            ("BACKGROUND",  (0,0), (-1,-1), _CARD),
            ("TEXTCOLOR",   (0,0), (-1,-1), _WHITE),
            ("TEXTCOLOR",   (0,0), (0,-1),  _MUTED),
            ("TEXTCOLOR",   (2,0), (2,-1),  _MUTED),
            ("FONTSIZE",    (0,0), (-1,-1), 8),
            ("FONTNAME",    (1,0), (1,-1),  "Helvetica-Bold"),
            ("FONTNAME",    (3,0), (3,-1),  "Helvetica-Bold"),
            ("GRID",        (0,0), (-1,-1), 0.3, _BORDER),
            ("LEFTPADDING", (0,0), (-1,-1), 6),
            ("TOPPADDING",  (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ]))
        block.append(core_tbl)

        # Maturity / target retirement
        maturity = services.compute_maturity_info(scheme)
        if maturity.get("available"):
            block.append(Spacer(1, 1.5*mm))
            if maturity["kind"] == "maturity":
                block.append(Paragraph(
                    f"Maturity Date: {format_date(maturity['date'])} "
                    f"({'Matured' if maturity['status']=='reached' else 'On track'})",
                    S["body"]))
            elif maturity["kind"] == "ssy":
                block.append(Paragraph(
                    f"Contribution Period Ends: {format_date(maturity['contribution_period_end'])}  |  "
                    f"Partial Withdrawal Eligible: {format_date(maturity['partial_withdrawal_date'])}  |  "
                    f"Maturity: {format_date(maturity['maturity_date'])}",
                    S["body"]))
            elif maturity["kind"] == "target_year":
                yrs = maturity["years_remaining"]
                block.append(Paragraph(
                    f"Target Retirement: {maturity['year']} "
                    f"({'Target year reached' if yrs <= 0 else f'{yrs} year(s) remaining'})",
                    S["body"]))

        # Contribution summary
        c_summary = services.contribution_summary(scheme.id)
        block.append(Spacer(1, 1.5*mm))
        block.append(Paragraph(
            f"Current FY Contributions: {format_inr(c_summary['current_fy_total'])}  |  "
            f"Total Contributions Recorded: {format_inr(c_summary['total_recorded'])}  |  "
            f"Number of Contributions: {c_summary['count']}",
            S["body"]))

        recent_contribs = services.get_contributions_for_scheme(scheme.id)[:10]
        if recent_contribs:
            c_data = [["Date", "Financial Year", "Amount", "Note"]]
            from .utils import financial_year_for_date
            for c in recent_contribs:
                c_data.append([
                    format_date(c.contribution_date),
                    financial_year_for_date(c.contribution_date),
                    format_inr(c.amount), (c.note or "—")[:40],
                ])
            c_tbl = Table(c_data, colWidths=[W*0.18, W*0.18, W*0.18, W*0.46])
            c_tbl.setStyle(_table_style())
            block.append(Spacer(1, 1.5*mm))
            block.append(c_tbl)

        # Nominees
        nominees = scheme.nominees.all()
        block.append(Spacer(1, 1.5*mm))
        if nominees:
            block.append(Paragraph("Nominees", S["muted"]))
            for n in nominees:
                pct = f" — {n.percentage}%" if n.percentage is not None else ""
                contact = f" | {n.contact}" if n.contact else ""
                block.append(Paragraph(f"• {n.name} ({n.relationship}){pct}{contact}", S["body"]))
        else:
            block.append(Paragraph("Nominees: Not added", S["muted"]))

        # Documents (metadata only)
        docs = scheme.documents.all()
        block.append(Spacer(1, 1.5*mm))
        if docs:
            block.append(Paragraph("Documents Stored", S["muted"]))
            for d in docs:
                block.append(Paragraph(
                    f"• {d.doc_type}: {d.original_name} ({format_date(d.uploaded_at)})",
                    S["body"]))
        else:
            block.append(Paragraph("Documents Stored: None", S["muted"]))

        if scheme.notes:
            block.append(Spacer(1, 1.5*mm))
            block.append(Paragraph(f"Notes: {scheme.notes}", S["muted"]))

        block.append(Spacer(1, 5*mm))
        story.append(KeepTogether(block))

    # ── Footer ──
    story += [
        Spacer(1, 4*mm),
        HRFlowable(width="100%", color=_BORDER, thickness=0.5),
        Spacer(1, 2*mm),
        Paragraph(
            "Generated locally by MyWealthLens • Personal Use Only • "
            "This is a personal record, not investment or financial advice.",
            S["footer"]),
    ]

    doc.build(story, onFirstPage=_dark_bg, onLaterPages=_dark_bg)
    buf.seek(0)
    fname = f"MyWealthLens_Retirement_{user.name.replace(' ', '_')}_{_dt.now().strftime('%Y%m%d')}.pdf"
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=fname)