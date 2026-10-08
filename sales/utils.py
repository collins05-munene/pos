import uuid
from django.db import transaction
from django.utils import timezone
from decimal import Decimal, ROUND_HALF_UP
from .models import Order, OrderItem
from inventory.models import Branch
from inventory.stock import change_stock, available_units, InsufficientStock
from products.models import ProductVariant
from .exceptions import InsufficientStockError


def resolve_user_branch(user):
    """
    The branch a user operates from, used to look up the ONE shared
    cash-pool session for that branch.

    Branch.objects is tenant-scoped (TenantManager), so .first() here
    already only ever sees the current tenant's branches.
    """
    if hasattr(user, 'branch') and user.branch:
        return user.branch
    if hasattr(user, 'profile') and hasattr(user.profile, 'branch'):
        return user.profile.branch
    return Branch.objects.first()


def complete_pos_sale(cart, payment_method, cashier, cash_session=None, amount_received=None):
    """
    amount_received is only meaningful for CASH sales. When provided, it is
    validated against the cart total computed here (server-side, not trusted
    from the client) and change_given is derived from it. Raises ValueError if
    amount_received is less than the total due.

    The sale's branch is the branch the cash session belongs to.

    Stock moves through change_stock(): selling 1 box of a 30-tablet pack
    removes 30 base units from the single shared stock row, so tablets, packets
    and boxes can never drift apart. Each order line stores the cost price of
    the unit actually sold, so COGS and profit are right per unit.
    """
    with transaction.atomic():
        branch = cash_session.branch if cash_session else resolve_user_branch(cashier)
        if branch is None:
            raise ValueError("Could not determine which branch this sale belongs to.")

        invoice_id = f"INV-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

        order = Order.objects.create(
            invoice_number=invoice_id,
            branch=branch,
            cashier=cashier,
            payment_method=payment_method,
            cash_session=cash_session
        )

        running_revenue = Decimal('0.00')
        running_cogs = Decimal('0.00')

        for loop_var in cart:
            if isinstance(loop_var, (tuple, list)):
                item_key = loop_var[0]
                item = loop_var[1]
            else:
                item = loop_var
                item_key = getattr(item, 'id', None)

            if isinstance(item, dict):
                raw_price = item.get('price', 0)
                raw_qty = item.get('quantity', 1)
            else:
                raw_price = getattr(item, 'price', 0)
                raw_qty = getattr(item, 'quantity', 1)

            variant_obj = getattr(item, 'variant', None) or getattr(item, 'product', None)
            if variant_obj and hasattr(variant_obj, 'id'):
                variant = variant_obj
            else:
                variant_id = getattr(item, 'variant_id', None) or item_key
                variant = ProductVariant.objects.select_related('base_variant').get(id=variant_id)

            retail_price = Decimal(str(raw_price))
            quantity_sold = int(raw_qty)
            cost_price = getattr(variant, 'cost_price', Decimal('0.00'))

            try:
                change_stock(branch, variant, -quantity_sold)
            except InsufficientStock:
                raise InsufficientStockError(variant, int(available_units(branch, variant)), quantity_sold)

            OrderItem(
                order=order,
                variant=variant,
                quantity=quantity_sold,
                retail_price=retail_price,
                cost_price=cost_price
            ).save()

            running_revenue += retail_price * quantity_sold
            running_cogs += cost_price * quantity_sold

        order.total_revenue = running_revenue
        order.total_cogs = running_cogs
        order.total_profit = running_revenue - running_cogs

        if payment_method == 'CASH' and amount_received is not None:
            amount_received = Decimal(str(amount_received)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            if amount_received < running_revenue:
                raise ValueError(
                    f"Amount received (KSH {amount_received}) is less than the total due (KSH {running_revenue})."
                )
            order.amount_received = amount_received
            order.change_given = (amount_received - running_revenue).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        order.save()

        return order