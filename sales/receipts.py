"""
sales/receipts.py

Builds the receipt record for a completed sale. A receipt is a frozen SNAPSHOT:
names, prices and totals are copied at sale time, so later price edits, product
renames or staff changes never alter a receipt that was already handed to a customer.
Cost price / profit are deliberately never copied -- a receipt is customer-facing.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from tenants.models import get_settings, next_number as next_tenant_number

from .models import Receipt

TWO_PLACES = Decimal('0.01')


def _money(value):
    return str(Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP))


def _line_name(variant):
    name = variant.product.name
    extras = [e for e in (getattr(variant, 'size', ''), getattr(variant, 'color', '')) if e]
    if extras:
        name += f" - {' / '.join(extras)}"
    if getattr(variant, 'base_variant_id', None):
        name += f" [{variant.pack_label}]"
    return name


def create_receipt(order, issued_at=None):
    """
    Idempotent: returns the existing receipt if this order already has one.
    Call inside the same transaction as the sale so the receipt number (and the
    sequence counter behind it) rolls back together with a failed sale.
    """
    existing = Receipt.objects.filter(order=order).first()
    if existing:
        return existing

    items = order.items.select_related('variant__product', 'variant__base_variant')
    lines = []
    for i in items:
        line = {
            'name': _line_name(i.variant),
            'sku': i.variant.sku,
            'quantity': i.quantity,
            # list price x quantity; a discount, if any, is shown on its own line beneath it
            'unit_price': _money(i.retail_price),
            'line_total': _money(i.retail_price * i.quantity),
        }
        if i.discount_per_unit > 0:
            line['discount'] = _money(i.discount_line)
            line['discount_name'] = i.discount_name
        lines.append(line)

    branch = order.branch
    cashier = order.cashier

    return Receipt.objects.create(
        order=order,
        receipt_number=next_tenant_number(order.tenant, 'receipt', 'RCT-'),
        branch=branch,
        cashier=cashier,
        shop_name=order.tenant.name,
        branch_name=str(branch.name) if branch else '',
        cashier_name=cashier.get_full_name() or cashier.get_username(),
        payment_method=order.payment_method,
        lines=lines,
        subtotal=order.subtotal,
        discount_total=order.discount_total,
        total=order.total_revenue,
        footer=get_settings(order.tenant).receipt_footer,
        amount_received=order.amount_received,
        change_given=order.change_given,
        issued_at=issued_at or timezone.now(),
    )