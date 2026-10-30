"""
Subscription receipts.

* `issue_receipt(invoice)`   - idempotent: one Receipt per PAID invoice (called from mark_invoice_paid,
                               and on demand for invoices paid before receipts existed).
* `render_receipt_pdf(r)`    - the downloadable PDF, used by the shop's billing page and platform admin.
* `pdf_response(r)`          - an HttpResponse wrapping that PDF with a sensible filename.
* `verify_url(r, base)`      - the public page a QR code on the receipt points to (see views.VerifyReceiptView).
* `mask_name(name)`          - "Mama Njeri Mini Mart" -> "Mama N**** M*** M***" for that public page.

Needs `reportlab` (pip install reportlab).
"""
import io
import logging
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from . import conf
from .models import BillingCredit, Invoice, MpesaPayment, Receipt

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- creating receipts
def _payment_details(invoice, reference):
    """-> (method, reference text, payer name, payer phone) for a paid invoice."""
    ref = (reference or "").strip()
    M = Receipt.Method

    stk = None
    if ref and ref != "credit":
        stk = MpesaPayment.objects.filter(invoice=invoice, mpesa_receipt=ref).first()
    elif not ref:   # asked for later (old invoice): fall back to whatever succeeded
        stk = (MpesaPayment.objects.filter(invoice=invoice, status=MpesaPayment.Status.SUCCESS)
               .order_by("-completed_at").first())
    if stk:
        return M.MPESA_STK, stk.mpesa_receipt or ref, "", stk.phone

    topups = list(BillingCredit.objects
                  .filter(invoice=invoice, kind=BillingCredit.Kind.TOPUP, mpesa_code__isnull=False)
                  .select_related("mpesa_code").order_by("created_at", "id"))
    codes = []
    for t in topups:
        if t.mpesa_code.code not in codes:
            codes.append(t.mpesa_code.code)
    if ref and ref != "credit" and ref not in codes:
        codes.append(ref)
    if codes:
        last = topups[-1].mpesa_code if topups else None
        return (M.MPESA_MANUAL, ", ".join(codes),
                last.sender_name if last else "", last.sender_phone if last else "")
    if ref == "credit":
        return M.CREDIT, "", "", ""
    return M.OTHER, ref, "", ""


def issue_receipt(invoice, *, reference=None, generated_by=None):
    """
    Return the invoice's receipt, creating it if needed. Safe to call any number of times and
    from concurrent requests. Raises ValueError for an invoice that has not been paid.
    """
    existing = Receipt.objects.filter(invoice=invoice).first()
    if existing:
        return existing
    if invoice.status != Invoice.Status.PAID:
        raise ValueError("Only paid invoices have a receipt.")

    method, ref, payer_name, payer_phone = _payment_details(invoice, reference)
    tenant = invoice.tenant
    try:
        with transaction.atomic():
            return Receipt.objects.create(
                invoice=invoice, tenant=tenant, amount=invoice.total,
                paid_at=invoice.paid_at or timezone.now(),
                payment_method=method, payment_reference=ref[:255],
                payer_name=payer_name[:120], payer_phone=(payer_phone or "")[:20],
                customer_name=tenant.name[:150], customer_phone=(tenant.phone or "")[:20],
                customer_email=tenant.email or "",
                generated_by=generated_by if getattr(generated_by, "pk", None) else None,
            )
    except IntegrityError:          # another request created it between our check and insert
        return Receipt.objects.get(invoice=invoice)


# --------------------------------------------------------------------------- PDF
def _money(value):
    value = Decimal(value)
    body = f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"
    return f"{conf.get('CURRENCY')} {body}"


def _day(dt):
    return timezone.localtime(dt).strftime("%d %b %Y") if dt else ""


def _stamp(dt):
    return timezone.localtime(dt).strftime("%d %b %Y, %H:%M") if dt else ""


def _line_items(invoice):
    """[(description, detail, amount)] that add up to invoice.total."""
    term = f"{invoice.term_months} month{'s' if invoice.term_months != 1 else ''}"
    branches = f"{invoice.branch_slots} branch{'es' if invoice.branch_slots != 1 else ''}"
    rows = []
    if invoice.is_upgrade:
        rows.append(("Extra branch add-on (pro-rated)",
                     f"{branches} in total, until {_day(invoice.period_end)}",
                     invoice.list_price_amount))
    else:
        period = (f"{_day(invoice.period_start)} to {_day(invoice.period_end)}, "
                  if invoice.period_start and invoice.period_end else "")
        rows.append((f"Subscription: {term}", f"{period}{branches}", invoice.list_price_amount))
    if invoice.discount_amount:
        rows.append(("Discount", "", -invoice.discount_amount))
    if invoice.install_fee_amount:
        rows.append(("One-time installation fee", "", invoice.install_fee_amount))
    return rows


def _find_logo():
    candidates = []
    try:
        from django.contrib.staticfiles import finders
        found = finders.find("img/unisale-logo.png")
        if found:
            candidates.append(Path(found))
    except Exception:       # staticfiles not configured (e.g. in tests)
        pass
    candidates.append(Path(__file__).resolve().parent.parent / "static" / "img" / "unisale-logo.png")
    return next((p for p in candidates if p.is_file()), None)


def mask_name(name):
    """Keep the first word, reduce the rest to their first letter: enough to recognise, not to harvest."""
    words = (name or "").split()
    if not words:
        return ""
    if len(words) == 1:
        w = words[0]
        return w[:3] + "*" * max(len(w) - 3, 0)
    return " ".join([words[0]] + [w[0] + "*" * (len(w) - 1) for w in words[1:]])


def verify_url(receipt, base_url=""):
    path = reverse("billing:verify", args=[receipt.verify_code])
    return f"{base_url.rstrip('/')}{path}" if base_url else ""


def _qr(url, size):
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    widget = QrCodeWidget(url, barLevel="M")
    x0, y0, x1, y1 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    drawing.add(widget)
    return drawing


def render_receipt_pdf(receipt, base_url=""):
    """-> bytes of an A4 PDF for `receipt`. `base_url` (https://yoursite.com) enables the QR code."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    INK, SOFT = colors.HexColor("#14171c"), colors.HexColor("#4a4f57")
    LINE, GREEN = colors.HexColor("#d8d4c6"), colors.HexColor("#078930")
    GREEN_DIM, PAPER = colors.HexColor("#e5f0e6"), colors.HexColor("#f2f1e9")

    def style(name, **kw):
        base = dict(fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK)
        base.update(kw)
        return ParagraphStyle(name, **base)

    body = style("body")
    soft = style("soft", textColor=SOFT, fontSize=8.5)
    label = style("label", textColor=SOFT, fontSize=7.5, leading=10, fontName="Helvetica-Bold")
    right = style("right", alignment=TA_RIGHT)
    right_bold = style("rb", alignment=TA_RIGHT, fontName="Helvetica-Bold")
    title = style("title", fontName="Helvetica-Bold", fontSize=22, leading=26, alignment=TA_RIGHT)
    RED = colors.HexColor("#9a3327")
    void = bool(getattr(receipt, "is_void", False))
    paid = style("paid", fontName="Helvetica-Bold", fontSize=9, textColor=RED if void else GREEN,
                 alignment=TA_RIGHT)

    def p(text, st=body):
        return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), st)

    invoice = receipt.invoice
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
        bottomMargin=16 * mm, title=f"Receipt {receipt.number}",
        author=conf.get("COMPANY_NAME"), subject=f"Subscription payment, {invoice.number}")
    width = A4[0] - 36 * mm
    story = []

    # ---- header: logo / company on the left, RECEIPT on the right
    logo = _find_logo()
    left = []
    if logo:
        left.append(Image(str(logo), width=44 * mm, height=44 * mm * 282 / 818))
    else:
        left.append(p(conf.get("COMPANY_NAME"), style("brand", fontName="Helvetica-Bold", fontSize=15, leading=18)))
    company_lines = [conf.get("COMPANY_ADDRESS"), conf.get("COMPANY_PHONE"), conf.get("COMPANY_EMAIL")]
    if any(company_lines):
        left.append(Spacer(1, 2 * mm))
        left.append(p("\n".join(x for x in company_lines if x), soft))
    header = Table([[left, [p("RECEIPT", title), p(receipt.number, right), Spacer(1, 1.5 * mm),
                            p("VOID" if void else "PAID IN FULL", paid)]]],
                   colWidths=[width * 0.55, width * 0.45])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [header, Spacer(1, 6 * mm)]

    # ---- who paid / payment facts
    contact = "\n".join(x for x in [receipt.customer_phone, receipt.customer_email] if x)
    who = [p("RECEIVED FROM", label), Spacer(1, 1 * mm),
           p(receipt.customer_name, style("who", fontName="Helvetica-Bold", fontSize=11, leading=14))]
    if contact:
        who.append(p(contact, soft))

    paid_by = " ".join(x for x in [receipt.payer_name, receipt.payer_phone] if x)
    facts = [("Date paid", _stamp(receipt.paid_at)),
             ("Invoice", invoice.number),
             ("Payment method", receipt.get_payment_method_display())]
    if receipt.payment_reference:
        facts.append(("M-Pesa reference" if receipt.payment_method != receipt.Method.OTHER else "Reference",
                      receipt.payment_reference))
    if paid_by:
        facts.append(("Paid by", paid_by))
    facts_tbl = Table([[p(k, soft), p(v, right)] for k, v in facts], colWidths=[28 * mm, width * 0.45 - 28 * mm])
    facts_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    info = Table([[who, facts_tbl]], colWidths=[width * 0.5, width * 0.5])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [info, Spacer(1, 8 * mm)]

    # ---- line items
    label_right = style("labelr", textColor=SOFT, fontSize=7.5, leading=10, fontName="Helvetica-Bold",
                        alignment=TA_RIGHT)
    rows = [[p("DESCRIPTION", label), p("AMOUNT", label_right)]]
    for desc, detail, amount in _line_items(invoice):
        cell = [p(desc)] + ([p(detail, soft)] if detail else [])
        rows.append([cell, p(("- " + _money(-amount)) if amount < 0 else _money(amount), right)])
    items = Table(rows, colWidths=[width * 0.75, width * 0.25])
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), PAPER), ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
    story += [items, Spacer(1, 4 * mm)]

    # ---- total
    total = Table([[p("AMOUNT PAID", style("tl", fontName="Helvetica-Bold", fontSize=9, textColor=GREEN)),
                    p(_money(receipt.amount), style("tv", fontName="Helvetica-Bold", fontSize=15, leading=18,
                                                    alignment=TA_RIGHT, textColor=GREEN))]],
                  colWidths=[width * 0.5, width * 0.5])
    total.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), GREEN_DIM), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                               ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10)]))
    story += [total, Spacer(1, 12 * mm)]

    # ---- verification block: QR code + code to type in
    url = verify_url(receipt, base_url)
    code = getattr(receipt, "verify_code_display", "") or ""
    if code:
        story.append(Spacer(1, 4 * mm))
        text = [p("VERIFY THIS RECEIPT", label), Spacer(1, 1 * mm)]
        if url:
            text.append(p("Scan the QR code, or open the address below and check that the details match "
                          "this receipt.", soft))
            text.append(p(url, style("url", fontName="Courier", fontSize=8, leading=11)))
        text.append(Spacer(1, 1 * mm))
        text.append(p(f"Verification code: {code}", style("vc", fontName="Courier-Bold", fontSize=10, leading=14)))
        cells = [[_qr(url, 26 * mm) if url else "", text]]
        box = Table(cells, colWidths=[30 * mm, width - 30 * mm])
        box.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOX", (0, 0), (-1, -1), 0.5, LINE),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 6),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        story += [box, Spacer(1, 8 * mm)]
    if void:
        reason = getattr(receipt, "void_reason", "")
        story.append(p("This receipt has been withdrawn and is no longer valid." + (f" Reason: {reason}" if reason else ""),
                       style("voidnote", alignment=1, textColor=RED, fontName="Helvetica-Bold")))
        story.append(Spacer(1, 2 * mm))

    # ---- footer
    note = conf.get("RECEIPT_FOOTER")
    if note:
        story.append(p(note, style("note", alignment=1, fontSize=10)))
        story.append(Spacer(1, 2 * mm))
    story.append(p("This receipt was generated by computer and is valid without a signature. "
                   f"Issued {_stamp(receipt.issued_at or timezone.now())}.",
                   style("fine", alignment=1, textColor=SOFT, fontSize=8)))

    brand = (conf.get("COMPANY_NAME") or "").upper()

    def watermark(canvas, _doc):
        """Faint tiled brand text on every page, plus a big PAID (or VOID) across the middle."""
        w, h = A4
        canvas.saveState()
        canvas.translate(w / 2, h / 2)
        canvas.rotate(32)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.setFillColor(colors.Color(0.08, 0.09, 0.11, alpha=0.05))
        for y in range(-700, 700, 64):
            for x in range(-700 + (y // 64 % 2) * 110, 700, 260):
                canvas.drawString(x, y, f"{brand}  RECEIPT")
        canvas.setFont("Helvetica-Bold", 150)
        canvas.setFillColor(colors.Color(0.60, 0.20, 0.15, alpha=0.14) if void
                            else colors.Color(0.03, 0.54, 0.19, alpha=0.07))
        canvas.drawCentredString(0, -50, "VOID" if void else "PAID")
        canvas.restoreState()

    doc.build(story, onFirstPage=watermark, onLaterPages=watermark)
    return buf.getvalue()


def site_base_url(request=None):
    """settings.BILLING["SITE_URL"] if set (recommended), else the host this request came in on."""
    configured = (conf.get("SITE_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    return request.build_absolute_uri("/").rstrip("/") if request is not None else ""


def pdf_response(receipt, *, inline=False, request=None):
    resp = HttpResponse(render_receipt_pdf(receipt, base_url=site_base_url(request)),
                        content_type="application/pdf")
    disposition = "inline" if inline else "attachment"
    resp["Content-Disposition"] = f'{disposition}; filename="{receipt.number}.pdf"'
    resp["Cache-Control"] = "private, no-store"
    return resp
