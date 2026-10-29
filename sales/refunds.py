"""
sales/refunds.py

How a refund works (and why):

* The original Order and Receipt are NEVER edited. The receipt is what the customer holds; the
  sale is what the books recorded. A refund is a separate record (sales.Refund) pointing at the
  sale, so both facts stay visible and the audit trail is honest.

* A refund gives back exactly what the customer paid: quantity x the unit price on the original
  order line. There is no free-typed amount, so a refund can never be bigger than the sale, and
  several partial refunds of the same line can never add up to more than was sold.

* Cash: the money leaves the drawer TODAY, from the branch's currently OPEN register, whatever
  day the sale was made. It is deducted from the register's expected cash
  (CashRegisterSession.get_current_expected_cash) so closing the register still balances.
  Refunds are refused if there is no open register or the pool does not hold enough cash.

* Stock, per line, the manager chooses:
    - restock    the goods are resellable. Stock goes back through inventory.stock.change_stock,
                 so a returned box adds its tablets to the same shared pool, and the cost of the
                 goods is recovered (cogs_reversed).
    - write off  the goods are NOT resellable (expired, damaged, opened medicine). Stock is not
                 touched, no cost is recovered, so the full refund shows as lost profit.

* Profit: Order.total_profit is untouched. The refund records profit_reversed
  (refund amount - cost recovered) and reports subtract it, by the date of the refund.

* Idempotent on client_uuid, serialised per sale by a row lock, so a double click or two managers
  refunding the same sale at once cannot refund the same goods twice.
"""
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.db.models import Sum, F, Q, DecimalField, ExpressionWrapper
from django.utils import timezone

from inventory.stock import change_stock
from tenants.models import next_number as next_tenant_number

from .models import CashRegisterSession, Order, Refund, RefundItem
from .receipts import _line_name

TWO_PLACES = Decimal('0.01')


class RefundError(Exception):
    """A refund that cannot be made; the message is safe to show to the user."""


def refund_window_days():
    """How long after a sale a manager may refund it. Admins may go past it."""
    return int(getattr(settings, 'POS_REFUND_WINDOW_DAYS', 30))


def _money(value):
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _is_admin(user):
    return getattr(user, 'role', None) == 'ADMIN' or getattr(user, 'is_superuser', False)


def refundable_lines(order):
    """One dict per sale line: how many were sold, already refunded, and still refundable."""
    done = dict(
        RefundItem.objects.filter(order_item__order=order)
        .values('order_item_id').annotate(q=Sum('quantity')).values_list('order_item_id', 'q'))
    lines = []
    for item in order.items.select_related('variant__product', 'variant__base_variant').order_by('id'):
        refunded = done.get(item.pk, 0)
        lines.append({
            'item': item,
            'name': _line_name(item.variant),
            'sku': item.variant.sku,
            'unit_price': item.retail_price,
            'sold': item.quantity,
            'refunded': refunded,
            'refundable': item.quantity - refunded,
        })
    return lines


def process_refund(order, user, lines, reason, notes='', client_uuid=None):
    """
    lines: [{'item_id': int, 'quantity': int, 'restock': bool}, ...] (zero quantities are ignored)
    Returns (refund, created). created is False when client_uuid was already processed.
    Raises RefundError with a user-facing message.
    """
    if reason not in dict(Refund.REASONS):
        raise RefundError("Choose the reason for this refund.")

    with transaction.atomic():
        # Serialise everything that touches this sale.
        order = Order.objects.select_for_update().get(pk=order.pk)

        if client_uuid:
            existing = Refund.objects.filter(client_uuid=client_uuid).first()
            if existing:
                return existing, False

        age_days = (timezone.now() - order.created_at).days
        if age_days > refund_window_days() and not _is_admin(user):
            raise RefundError(
                f"This sale is {age_days} days old. Refunds are allowed within {refund_window_days()} days; "
                f"an admin can approve older ones.")

        branch = order.branch
        if branch is None:
            raise RefundError("This sale has no branch, so the refund cannot be paid from a register.")
        session = CashRegisterSession.get_active_session(branch)
        if session is None:
            raise RefundError("There is no open cash register. Open the register first: refunds are paid out of the cash drawer.")

        by_id = {l['item'].pk: l for l in refundable_lines(order)}
        wanted = []
        for raw in lines:
            try:
                item_id, qty = int(raw['item_id']), int(raw.get('quantity') or 0)
            except (KeyError, TypeError, ValueError):
                raise RefundError("The refund form was not understood. Please try again.")
            if qty == 0:
                continue
            line = by_id.get(item_id)
            if line is None:
                raise RefundError("A product in the refund does not belong to this sale.")
            if qty < 0 or qty > line['refundable']:
                raise RefundError(
                    f"{line['name']}: only {line['refundable']} can still be refunded "
                    f"(sold {line['sold']}, already refunded {line['refunded']}).")
            wanted.append((line, qty, bool(raw.get('restock'))))
        if not wanted:
            raise RefundError("Enter a quantity for at least one item.")

        total = Decimal('0.00')
        cogs_back = Decimal('0.00')
        built = []
        for line, qty, restock in wanted:
            item = line['item']
            amount = _money(item.retail_price * qty)
            cogs = _money(item.cost_price * qty) if restock else Decimal('0.00')
            total += amount
            cogs_back += cogs
            built.append((item, line, qty, restock, amount, cogs))

        available = session.get_current_expected_cash()
        if total > available:
            raise RefundError(
                f"The register only holds KSH {available} but this refund is KSH {total}. "
                f"Record a Cash In first.")

        for item, line, qty, restock, amount, cogs in built:
            if restock:
                change_stock(branch, item.variant, qty)       # same unit the sale was made in

        refund = Refund.objects.create(
            refund_number=next_tenant_number(order.tenant, 'refund', 'RFD-'),
            order=order,
            branch=branch,
            cash_session=session,
            processed_by=user,
            reason=reason,
            notes=(notes or '').strip(),
            total_amount=total,
            cogs_reversed=cogs_back,
            profit_reversed=total - cogs_back,
            shop_name=order.tenant.name,
            branch_name=str(branch.name),
            processed_by_name=user.get_full_name() or user.get_username(),
            client_uuid=client_uuid,
        )
        for item, line, qty, restock, amount, cogs in built:
            RefundItem(
                refund=refund, order_item=item, variant=item.variant,
                description=line['name'], sku=line['sku'], quantity=qty,
                unit_price=item.retail_price, unit_cost=item.cost_price,
                restocked=restock, amount=amount, cogs_reversed=cogs,
            ).save()
        return refund, True


def refund_totals(start=None, end=None):
    """
    Refund money/cost/profit for a period (by refund date), for reports: subtract from sales figures.

    cogs       full cost of every refunded item (the sale is undone, so its cost is reversed)
    writeoffs  cost of refunded items that were NOT restocked (lost stock, shown as its own line)
    profit     profit given up = amount - cogs + writeoffs  (== amount - restocked cost)
    """
    qs = Refund.objects.all()
    if start is not None:
        qs = qs.filter(created_at__gte=start)
    if end is not None:
        qs = qs.filter(created_at__lt=end)

    zero = Decimal('0.00')
    head = qs.aggregate(amount=Sum('total_amount'), profit=Sum('profit_reversed'))

    cost = ExpressionWrapper(F('quantity') * F('unit_cost'),
                             output_field=DecimalField(max_digits=14, decimal_places=2))
    items = RefundItem.objects.filter(refund__in=qs).aggregate(
        cogs=Sum(cost),
        writeoffs=Sum(cost, filter=Q(restocked=False)),
    )
    return {
        'amount': head['amount'] or zero,
        'profit': head['profit'] or zero,
        'cogs': items['cogs'] or zero,
        'writeoffs': items['writeoffs'] or zero,
    }