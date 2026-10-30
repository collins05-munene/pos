"""
sales/report_pdf.py

Builds the downloadable business performance report (PDF) from the same context the sales
dashboard uses, so the PDF can never disagree with what is on screen.

Uses ReportLab (pure Python, no system libraries needed on Render). Add `reportlab` to
requirements.txt.
"""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

INK = colors.HexColor('#1f2937')
MUTED = colors.HexColor('#6b7280')
LINE = colors.HexColor('#e5e7eb')
SOFT = colors.HexColor('#f3f4f6')
BLUE = colors.HexColor('#2563eb')
GREEN = colors.HexColor('#15803d')
RED = colors.HexColor('#b91c1c')


def _kes(value, decimals=2):
    return f'KSH {value:,.{decimals}f}'


def _styles():
    base = getSampleStyleSheet()['Normal']
    def style(name, **kw):
        kw.setdefault('textColor', INK)
        return ParagraphStyle(name, parent=base, **kw)
    return {
        'title': style('title', fontName='Helvetica-Bold', fontSize=20, leading=24),
        'sub': style('sub', fontSize=10.5, leading=14, textColor=MUTED),
        'h2': style('h2', fontName='Helvetica-Bold', fontSize=13, leading=17, spaceBefore=16, spaceAfter=6),
        'note': style('note', fontSize=9, leading=12, textColor=MUTED),
        'cell': style('cell', fontSize=10, leading=13),
        'cell_r': style('cell_r', fontSize=10, leading=13, alignment=TA_RIGHT),
        'head': style('head', fontName='Helvetica-Bold', fontSize=9.5, leading=12, textColor=MUTED),
        'head_r': style('head_r', fontName='Helvetica-Bold', fontSize=9.5, leading=12,
                        textColor=MUTED, alignment=TA_RIGHT),
    }


def _table(rows, col_widths, header=True):
    table = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
    commands = [
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('LINEBELOW', (0, 0), (-1, -1), 0.5, LINE),
        ('BOX', (0, 0), (-1, -1), 0.5, LINE),
    ]
    if header:
        commands.append(('BACKGROUND', (0, 0), (-1, 0), SOFT))
    table.setStyle(TableStyle(commands))
    return table


def _page_footer(shop_name):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(doc.leftMargin, 12 * mm, shop_name)
        canvas.drawRightString(A4[0] - doc.rightMargin, 12 * mm, f'Page {doc.page}')
        canvas.restoreState()
    return draw


def build_sales_report_pdf(context, *, shop_name, generated_by, generated_at):
    """Returns the finished PDF as bytes. `context` is SalesDashboardView's context."""
    st = _styles()
    metrics = context['metrics']
    cash = context['cash_metrics']
    cumulative = context['cumulative_cash']
    period = context['period']
    scope = context['scope_label']
    shop = escape(shop_name or 'Business')

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=20 * mm,
        title=f'Business performance report - {scope}', author=shop_name or '',
    )
    width = A4[0] - doc.leftMargin - doc.rightMargin
    story = []

    # --- heading ---
    story.append(Paragraph('Business performance report', st['title']))
    story.append(Spacer(1, 3))
    story.append(Paragraph(f'<b>{shop}</b> &ndash; {escape(scope)}', st['sub']))
    story.append(Paragraph(escape(period['label']), st['sub']))
    story.append(Paragraph(
        f'Prepared {generated_at:%d %b %Y, %H:%M} by {escape(generated_by)}', st['note']))

    # --- headline numbers ---
    profit_colour = RED if metrics['profit'] < 0 else GREEN
    hero = Table([
        [Paragraph('Sales', st['head']), Paragraph('Profit', st['head'])],
        [Paragraph(f'<font size="17" color="#2563eb"><b>{_kes(metrics["revenue"], 0)}</b></font>', st['cell']),
         Paragraph(f'<font size="17" color="{profit_colour.hexval().replace("0x", "#")}"><b>{_kes(metrics["profit"], 0)}</b></font>', st['cell'])],
        [Paragraph('Money received from customers, after refunds.', st['note']),
         Paragraph('What is left after paying for the goods sold.', st['note'])],
    ], colWidths=[width / 2, width / 2])
    hero.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.5, LINE),
        ('LINEAFTER', (0, 0), (0, -1), 0.5, LINE),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, -1), (-1, -1), 10),
    ]))
    story.append(Spacer(1, 12))
    story.append(hero)

    # --- sales and profit detail ---
    orders = period['order_count']
    rows = [
        ['Sales (after refunds)', _kes(metrics['revenue'])],
        ['Cost of the goods sold', _kes(metrics['cogs'])],
        ['Profit', _kes(metrics['profit'])],
        ['Profit on every KSH 100 sold', f'KSH {metrics["margin"]:,.0f}'],
        ['Number of orders', f'{orders:,}'],
        ['Refunded to customers', _kes(metrics['refunds'])],
        ['Returned goods thrown away (already taken off profit)', _kes(metrics['writeoffs'])],
    ]
    body = [[Paragraph(str(a), st['cell']), Paragraph(str(b), st['cell_r'])] for a, b in rows]
    story.append(Paragraph('Sales and profit', st['h2']))
    story.append(_table(body, [width * 0.68, width * 0.32], header=False))

    # --- branches ---
    branch_rows = context.get('branch_rows') or []
    if branch_rows:
        data = [[Paragraph('Branch', st['head']), Paragraph('Sales', st['head_r']),
                 Paragraph('Profit', st['head_r']), Paragraph('Orders', st['head_r'])]]
        for row in branch_rows:
            data.append([
                Paragraph(escape(row['name']), st['cell']),
                Paragraph(_kes(row['revenue'], 0), st['cell_r']),
                Paragraph(_kes(row['profit'], 0), st['cell_r']),
                Paragraph(f'{row["orders"]:,}', st['cell_r']),
            ])
        block = [Paragraph('How each branch did', st['h2'])]
        top = context.get('top_branch')
        if top:
            block.append(Paragraph(
                f'{escape(top["name"])} sold the most: {_kes(top["revenue"], 0)}.', st['note']))
            block.append(Spacer(1, 4))
        block.append(_table(data, [width * 0.38, width * 0.25, width * 0.25, width * 0.12]))
        story.append(KeepTogether(block))

    # --- cash ---
    diff = cash['discrepancies']
    diff_note = 'cash was short' if diff < 0 else 'there was extra cash' if diff > 0 else 'counted cash matched'
    cash_rows = [
        ('Cash that should be in the drawer now', _kes(cumulative['live_pool_balance'])),
        ('Registers open now', f'{cash["active_sessions_count"]}'),
        ('Cash added in this period', _kes(cash['cash_in'])),
        ('Cash taken out in this period', _kes(cash['cash_out'])),
        (f'Cash difference at closing ({diff_note})', _kes(diff)),
        ('Cash sales, all time (after refunds)', _kes(cumulative['total_cash_revenue'])),
        ('Cash counted at last closing', _kes(cumulative['total_counted_cash'])),
        ('Cash difference, all time', _kes(cumulative['total_discrepancy'])),
    ]
    data = [[Paragraph(a, st['cell']), Paragraph(b, st['cell_r'])] for a, b in cash_rows]
    story.append(KeepTogether([
        Paragraph('Cash', st['h2']),
        Paragraph(f'{escape(scope)}. Drawer balance and all-time lines ignore the dates above.', st['note']),
        Spacer(1, 4),
        _table(data, [width * 0.68, width * 0.32], header=False),
    ]))

    footer = _page_footer(shop_name or 'Business')
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
