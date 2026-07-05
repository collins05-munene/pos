from decimal import Decimal
from django.conf import settings
from products.models import ProductVariant
from .exceptions import InsufficientStockError
from inventory.models import Branch, StockLevel

class CartItem:
    def __init__(self, variant_id, name, sku, price, quantity):
        self.variant_id = int(variant_id)
        self.name = name
        self.sku = sku
        self.price = Decimal(price)
        self.quantity = int(quantity)

    @property
    def total_price(self):
        return self.price * self.quantity


class POSCart:
    def __init__(self, request):
        self.session = request.session
        cart = self.session.get('pos_cart')
        if not cart:
            cart = self.session['pos_cart'] = {}
        self.cart = cart

    def _get_branch(self):
        branch, _ = Branch.objects.get_or_create(
            name="Main Branch",
            defaults={"location": "Headquarters", "is_active": True}
        )
        return branch
    
    def _get_available_stock(self, variant, branch=None):
        branch = branch or self._get_branch()

        try:
            stock_level = StockLevel.objects.get(branch=branch, variant=variant)
            return stock_level.quantity
        except StockLevel.DoesNotExist:
            return Decimal('0.000')

    def add(self, variant_id, quantity=1, override_quantity=False):
        variant_id = str(variant_id)
        try:
            variant = ProductVariant.objects.select_related('product').get(id=variant_id)
        except ProductVariant.DoesNotExist:
            return False

        branch = self._get_branch()
        available = self._get_available_stock(variant, branch)

        current_qty_in_cart = int(self.cart.get(variant_id, {}).get('quantity', 0))
        requested_qty = max(1, int(quantity))

        new_total_qty = requested_qty if override_quantity else current_qty_in_cart + requested_qty

        if new_total_qty > available:
            raise InsufficientStockError(variant, available, new_total_qty)
        
        info = f" - {variant.size}" if variant.size else ""
        info += f" / {variant.color}" if variant.color else ""
        display_name = f"{variant.product.name}{info}"

        
        if variant_id not in self.cart:
            self.cart[variant_id] = {
                'variant_id': int(variant_id),
                'name': display_name,
                'sku': variant.sku,
                'price': str(variant.retail_price),
                'quantity': 0 
            }

        self.cart[variant_id]['quantity'] = new_total_qty

        self.save()
        return True

    def remove(self, variant_id):
        variant_id = str(variant_id)
        if variant_id in self.cart:
            del self.cart[variant_id]
            self.save()

    def update_quantity(self, variant_id, quantity):
        return self.add(variant_id, quantity, override_quantity=True)
    
    def validate_stock(self, branch=None):
        branch = branch or self._get_branch()
        problems = []

        for item in self.cart.values():
            variant_id = item['variant_id']
            requested_qty = int(item['quantity'])

            try:
                variant = ProductVariant.objects.get(id=variant_id)
            except ProductVariant.DoesNotExist:
                problems.append({
                    'variant_id': variant_id,
                    'name': item.get('name', ''),
                    'requested': requested_qty,
                    'available': 0,
                    'reason': 'Product no longer exists.'
                })
                continue
            
            available = self._get_available_stock(variant, branch)
            if requested_qty > available:
                problems.append({
                    'variant_id': variant_id,
                    'name': item.get('name', ''),
                    'requested': requested_qty,
                    'available': 0,
                    'reason': 'Product no longer exists.'
                })
        return problems

    def save(self):
        self.session.modified = True

    def __iter__(self):
        for item in self.cart.values():
            yield CartItem(**item)

    @property
    def total_items(self):
        return sum(int(item['quantity']) for item in self.cart.values())

    @property
    def get_total_price(self):
        return sum(Decimal(item['price']) * int(item['quantity']) for item in self.cart.values())