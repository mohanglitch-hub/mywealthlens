"""
Family Centre — PDF Export
=============================
Builds a printable "In Case of Emergency" summary — deliberately
scoped to what a family member would actually need if reading this
during a crisis, not what's useful to the account holder day-to-day.

Includes: every person and what they're connected to (nominee
share, what a Wealth asset came from or will go to), plus Coverage
Gaps (a real part of "what happens if I'm not around"). Deliberately
EXCLUDES Possible Duplicates and Relationship Mismatches — those are
data-quality tools for whoever maintains this app, not something a
family member reading a printed page needs to see.

Follows the same reportlab-based architecture and dark/teal branding
already used elsewhere in MyWealthLens (app.py's export_pdf,
insurance_centre's and retirement_centre's own export.py).
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

from .routes import _build_people, _coverage_gaps

# ── Shared colours (matches app.py / insurance_centre / retirement_centre) ──
_TEAL   = colors.HexColor("#00d4aa")
_DARK   = colors.HexColor("#0d0f14")
_CARD   = colors.HexColor("#13161f")
_CARD2  = colors.HexColor("#1a1e2d")
_BORDER = colors.HexColor("#1e2130")
_WHITE  = colors.white
_MUTED  = colors.HexColor("#64748b")
_AMBER  = colors.HexColor("#f59e0b")


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
        ("BACKGROUND",   (0,0), (-1,0),  hbg),
        ("TEXTCOLOR",    (0,0), (-1,0),  _TEAL),
        ("TEXTCOLOR",    (0,1), (-1,-1), _WHITE),
        ("FONTNAME",     (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0,0), (-1,-1), 8),
        ("GRID",         (0,0), (-1,-1), 0.3, _BORDER),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [_CARD, _CARD2]),
        ("TOPPADDING",   (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0), (-1,-1), 5),
        ("LEFTPADDING",  (0,0), (-1,-1), 6),
    ])


def _dark_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(_DARK)
    canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
    canvas.restoreState()


def build_family_pdf(user):
    """
    Builds and returns a Flask send_file response containing the
    Family Centre "In Case of Emergency" summary for `user` ONLY —
    both _build_people and _coverage_gaps are already scoped to a
    single user_id, so nothing here can pull another user's data.
    """
    people = _build_people(user.id)
    gaps = _coverage_gaps(user.id)

    S = _styles()
    story = []
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=15*mm, bottomMargin=15*mm,
        title="MyWealthLens — Family Centre",
        author=user.name,
    )
    W = doc.width

    # ── Cover ──
    now_str = _dt.now().strftime("%d %B %Y, %I:%M %p")
    story += [
        Paragraph("MyWealthLens", S["title"]),
        Paragraph("Family Centre — In Case of Emergency", S["h1"]),
        Paragraph(f"Prepared for {user.name} ({user.email})", S["sub"]),
        Paragraph(f"Generated on {now_str}", S["sub"]),
        Spacer(1, 4*mm),
        HRFlowable(width="100%", color=_TEAL, thickness=1.5),
        Spacer(1, 4*mm),
    ]

    # ── Coverage Gaps ──
    if gaps:
        story.append(Paragraph("Coverage Gaps", S["h1"]))
        story.append(Paragraph(
            "The following have no nominee recorded, or nominees whose "
            "shares don't add up to 100% — a portion of the payout has "
            "nowhere defined to go.", S["muted"]))
        story.append(Spacer(1, 2*mm))
        gap_data = [["Source", "Item", "Issue"]]
        for g in gaps:
            gap_data.append([g["source"], g["item_name"], g["issue"]])
        gap_tbl = Table(gap_data, colWidths=[W*0.20, W*0.45, W*0.35])
        gap_tbl.setStyle(_table_style(header_bg=colors.HexColor("#4a3510")))
        story += [gap_tbl, Spacer(1, 4*mm)]

    # ── People ──
    story.append(Paragraph("People &amp; Their Connections", S["h1"]))
    if not people:
        story.append(Paragraph(
            "No nominees, family-linked assets, or family members recorded yet.",
            S["muted"]))
    for person in people:
        block = []
        block.append(HRFlowable(width="100%", color=_TEAL, thickness=0.5))
        block.append(Spacer(1, 1.5*mm))
        block.append(Paragraph(person["display_name"], S["h2"]))

        for e in person["entries"]:
            if e["direction"] == "nominee":
                pct = f" — {e['percentage']:.0f}% share" if e.get("percentage") else ""
                line = f"• Nominee on {e['item_name']} ({e.get('relationship') or 'relationship not set'}){pct} — {e['source']}"
            elif e["direction"] == "heir":
                rel = f" ({e['relationship']})" if e.get("relationship") else ""
                pct = f" — {e['percentage']:.0f}% share" if e.get("percentage") else ""
                line = f"• Will inherit: {e['item_name']}{rel}{pct}"
            elif e["direction"] == "benefactor":
                rel = f" ({e['relationship']})" if e.get("relationship") else ""
                received = f", received {e['date_received'].strftime('%d %b %Y')}" if e.get("date_received") else ""
                line = f"• Gave you: {e['item_name']}{rel} — {e.get('detail') or ''}{received}"
            else:
                rel = f" — {e['relationship']}" if e.get("relationship") else ""
                line = f"• Family member{rel} (added directly)"
            block.append(Paragraph(line, S["body"]))

        block.append(Spacer(1, 3*mm))
        story.append(KeepTogether(block))

    # ── Footer ──
    story += [
        Spacer(1, 4*mm),
        HRFlowable(width="100%", color=_BORDER, thickness=0.5),
        Spacer(1, 2*mm),
        Paragraph(
            "Generated locally by MyWealthLens • Personal Use Only • "
            "People are grouped by name; identically-spelled entries are "
            "treated as the same person.",
            S["footer"]),
    ]

    doc.build(story, onFirstPage=_dark_bg, onLaterPages=_dark_bg)
    buf.seek(0)
    fname = f"MyWealthLens_Family_{user.name.replace(' ', '_')}_{_dt.now().strftime('%Y%m%d')}.pdf"
    return send_file(buf, mimetype="application/pdf",
                     as_attachment=True, download_name=fname)