import uuid
from django.db import transaction
from django.utils import timezone
from decimal import Decimal, ROUND_HALF_UP
from .models import Order, OrderItem
from inventory.models import StockLevel, Branch
from products.models import ProductVariant
from .exceptions import InsufficientStockError


def resolve_user_branch(user):
    """
    The branch a user operates from, used to look up the ONE shared
    cash-pool session for that branch. Sessions are no longer tied to
    an individual cashier, so every view that needs "the active
    session" should go through this instead of get_active_session(user).
    """
    if hasattr(user, 'branch') and user.branch:
        return user.branch
    if hasattr(user, 'profile') and hasattr(user.profile, 'branch'):
        return user.profile.branch
    return Branch.objects.first()


def complete_pos_sale(cart, payment_method, cashier, cash_session=None, amount_received=None):
    """
    amount_received is only meaningful for CASH sales. When provided,
    it is validated against the cart total computed here (server-side,
    not trusted from the client) and change_given is derived from it.
    Raises ValueError if amount_received is less than the total due.
    """
    with transaction.atomic():
        global_branch, _ = Branch.objects.get_or_create(
            name="Main Branch",
            defaults={"location": "Headquarters", "is_active": True}
        )

        invoice_id = f"INV-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

        order = Order.objects.create(
            invoice_number=invoice_id,
            branch=global_branch,
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
                variant = ProductVariant.objects.get(id=variant_id)

            retail_price = Decimal(str(raw_price))
            quantity_sold = int(raw_qty)
            cost_price = getattr(variant, 'cost_price', Decimal('0.00'))

            stock_record, _ = StockLevel.objects.select_for_update().get_or_create(
                branch=global_branch,
                variant=variant,
                defaults={'quantity': 0}
            )
            if stock_record.quantity < quantity_sold:
                raise InsufficientStockError(variant, stock_record.quantity, quantity_sold)

            stock_record.quantity -= quantity_sold
            stock_record.save()

            order_item = OrderItem(
                order=order,
                variant=variant,
                quantity=quantity_sold,
                retail_price=retail_price,
                cost_price=cost_price
            )
            order_item.save()

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