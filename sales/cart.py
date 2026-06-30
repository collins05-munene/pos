from decimal import Decimal
from django.conf import settings
from products.models import ProductVariant

class CartItem:
    """Wrapper class ensuring unified property syntax across templates."""
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

    def add(self, variant_id, quantity=1, override_quantity=False):
        variant_id = str(variant_id)
        try:
            variant = ProductVariant.objects.select_related('product').get(id=variant_id)
        except ProductVariant.DoesNotExist:
            return False

        # Build display designation logic cleanly
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

        if override_quantity:
            self.cart[variant_id]['quantity'] = max(1, int(quantity))
        else:
            self.cart[variant_id]['quantity'] += max(1, int(quantity))
            
        self.save()
        return True

    def remove(self, variant_id):
        variant_id = str(variant_id)
        if variant_id in self.cart:
            del self.cart[variant_id]
            self.save()

    def update_quantity(self, variant_id, quantity):
        return self.add(variant_id, quantity, override_quantity=True)

    def save(self):
        self.session.modified = True

    def __iter__(self):
        """Yields instantiated CartItem wrappers."""
        for item in self.cart.values():
            yield CartItem(**item)

    @property
    def total_items(self):
        return sum(int(item['quantity']) for item in self.cart.values())

    @property
    def get_total_price(self):
        return sum(Decimal(item['price']) * int(item['quantity']) for item in self.cart.values())