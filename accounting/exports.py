"""PDF (ReportLab) and Excel (openpyxl) output for any Doc. Same figures as the screen: one source."""
from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .reporting import fmt_money

INK, MUTED, LINE, SOFT, BLUE = (colors.HexColor(h) for h in ("#1f2937", "#6b7280", "#d1d5db", "#f3f4f6", "#1d4ed8"))


def _header_lines(doc):
    t = doc.tenant
    details = " | ".join(x for x in (getattr(t, "phone", ""), getattr(t, "email", "")) if x)
    lines = [("Business", t.name)]
    if details:
        lines.append(("Details", details))
    lines += [("Branch", doc.branch_label), ("Period", doc.period_label), ("Basis", doc.basis_label),
              ("Currency", doc.currency), ("Generated", doc.generated_at.strftime("%d %b %Y %H:%M"))]
    return lines


def _cell(v, money):
    if isinstance(v, Decimal):
        return fmt_money(v)
    return "" if v is None else str(v)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def to_pdf(doc):
    buf = BytesIO()
    wide = max((len(t.columns) for t in doc.tables), default=2) > 5
    page = landscape(A4) if wide else A4
    pdf = SimpleDocTemplate(buf, pagesize=page, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"{doc.title} - {doc.tenant.name}", author="Unisale Systems")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=17, textColor=INK, alignment=0, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading3"], fontSize=11, textColor=BLUE, spaceBefore=10, spaceAfter=4)
    small = ParagraphStyle("sm", parent=ss["Normal"], fontSize=8, textColor=MUTED, leading=10)
    cellp = ParagraphStyle("cell", parent=ss["Normal"], fontSize=8, leading=10, textColor=INK)
    story = [Paragraph(escape(doc.tenant.name), h1), Paragraph(escape(doc.title), ParagraphStyle("t", parent=h2, fontSize=13, spaceBefore=0)), Spacer(1, 3)]
    meta = Table([[Paragraph(f"<b>{escape(k)}</b>", small), Paragraph(escape(v), small)] for k, v in _header_lines(doc)],
                 colWidths=[24 * mm, None], hAlign="LEFT")
    meta.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1)]))
    story += [meta, Spacer(1, 6)]
    avail = page[0] - 28 * mm
    for tb in doc.tables:
        story.append(Paragraph(escape(tb.title), h2))
        ncol = len(tb.columns)
        first = max(avail * (0.30 if ncol > 4 else 0.55), 40 * mm)
        rest = (avail - first) / max(ncol - 1, 1)
        widths = [first] + [rest] * (ncol - 1)
        data = [[Paragraph(f"<b>{escape(str(c))}</b>", cellp) for c in tb.columns]]
        styles = [("BACKGROUND", (0, 0), (-1, 0), SOFT), ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
                  ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
        for i, r in enumerate(tb.rows, start=1):
            row = []
            for j, v in enumerate(r.cells):
                txt = escape(_cell(v, j in tb.money_cols))
                if j == 0 and r.indent:
                    txt = "&nbsp;" * (4 * r.indent) + txt
                bold = r.style in ("section", "subtotal", "total")
                row.append(Paragraph(f"<b>{txt}</b>" if bold else txt, cellp))
            data.append(row)
            if r.style in ("subtotal", "total"):
                styles.append(("LINEABOVE", (0, i), (-1, i), 0.5, INK))
            if r.style == "total":
                styles.append(("BACKGROUND", (0, i), (-1, i), SOFT))
            if r.style == "section":
                styles.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#eff6ff")))
        for j in range(ncol):
            if j in tb.money_cols or (j > 0 and all(isinstance(r.cells[j], (Decimal, int)) or str(r.cells[j]).endswith("%") or r.cells[j] in ("", "n/a", "-")
                                                      for r in tb.rows if len(r.cells) > j)):
                styles.append(("ALIGN", (j, 0), (j, -1), "RIGHT"))
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle(styles + [("LINEBELOW", (0, 1), (-1, -1), 0.25, LINE)]))
        story.append(t)
    for line in doc.alerts + doc.notes:
        story += [Spacer(1, 4), Paragraph(escape(line), small)]

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(14 * mm, 8 * mm, f"{doc.tenant.name} | {doc.title} | {doc.period_label}")
        canvas.drawRightString(page[0] - 14 * mm, 8 * mm, f"Page {d.page}  |  Unisale Systems")
        canvas.restoreState()
    pdf.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------
def to_xlsx(doc):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    wb = Workbook()
    ws = wb.active
    ws.title = doc.title[:31]
    bold = Font(bold=True)
    ws["A1"], ws["A1"].font = doc.tenant.name, Font(bold=True, size=14)
    ws["A2"], ws["A2"].font = doc.title, Font(bold=True, size=12, color="1D4ED8")
    r = 3
    for k, v in _header_lines(doc):
        ws.cell(r, 1, k).font = bold
        ws.cell(r, 2, v)
        r += 1
    r += 1
    fill = PatternFill("solid", fgColor="F3F4F6")
    top = Border(top=Side(style="thin"))
    for tb in doc.tables:
        ws.cell(r, 1, tb.title).font = Font(bold=True, color="1D4ED8")
        r += 1
        for j, c in enumerate(tb.columns, 1):
            cell = ws.cell(r, j, c)
            cell.font, cell.fill = bold, fill
            if j - 1 in tb.money_cols:
                cell.alignment = Alignment(horizontal="right")
        r += 1
        for row in tb.rows:
            for j, v in enumerate(row.cells, 1):
                cell = ws.cell(r, j, float(v) if isinstance(v, Decimal) else v)
                if isinstance(v, Decimal):
                    cell.number_format = '#,##0.00;(#,##0.00)'
                    cell.alignment = Alignment(horizontal="right")
                if j == 1 and row.indent:
                    cell.alignment = Alignment(indent=row.indent * 2)
                if row.style in ("section", "subtotal", "total"):
                    cell.font = bold
                if row.style in ("subtotal", "total"):
                    cell.border = top
                if row.style == "total":
                    cell.fill = fill
            r += 1
        r += 1
    for line in doc.alerts + doc.notes:
        ws.cell(r, 1, line).font = Font(italic=True, color="6B7280")
        r += 1
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 12), 48)
    ws.freeze_panes = "A8"
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
