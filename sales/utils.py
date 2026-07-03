import uuid
from django.db import transaction
from django.utils import timezone
from decimal import Decimal
from .models import Order, OrderItem
from inventory.models import StockLevel, Branch
from products.models import ProductVariant
from .exceptions import InsufficientStockError

def complete_pos_sale(cart, payment_method, cashier):
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
            payment_method=payment_method
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
            
            # Save line item values (OrderItem's custom save handles its internal metrics)
            order_item = OrderItem(
                order=order,
                variant=variant,
                quantity=quantity_sold,
                retail_price=retail_price,
                cost_price=cost_price
            )
            order_item.save() 
            
            # Aggregate totals accurately using local variables
            running_revenue += retail_price * quantity_sold
            running_cogs += cost_price * quantity_sold
            
        # FIX 3: Commit structural totals safely back into the parent Order
        order.total_revenue = running_revenue
        order.total_cogs = running_cogs
        order.total_profit = running_revenue - running_cogs
        order.save()
        
        return order