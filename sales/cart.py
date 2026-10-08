from decimal import Decimal

from products.models import ProductVariant
from inventory.models import StockLevel
from .exceptions import InsufficientStockError
from .utils import resolve_user_branch


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
    """
    Session cart. Stock checks use the SAME branch the sale will be deducted from
    (the user's branch / open register), and treat every selling unit of a product as
    drawing on one shared pool of base units, so '40 tablets + 1 box (30)' is checked
    against the real stock rather than each line separately.
    """

    def __init__(self, request):
        self.session = request.session
        self.user = request.user
        cart = self.session.get('pos_cart')
        if not cart:
            cart = self.session['pos_cart'] = {}
        self.cart = cart

    def _get_branch(self):
        branch = resolve_user_branch(self.user)
        if branch is None:
            raise ValueError("Could not determine which branch this cart belongs to.")
        return branch

    def _pool_quantity(self, variant, branch):
        """Base units on hand for this variant's stock pool."""
        return (StockLevel.objects
                .filter(branch=branch, variant=variant.stock_variant)
                .values_list('quantity', flat=True).first()) or Decimal('0')

    def _get_available_stock(self, variant, branch=None):
        """Whole units of THIS variant's own unit the pool could supply (ignores the cart)."""
        branch = branch or self._get_branch()
        return self._pool_quantity(variant, branch) // variant.units_per_pack

    def _base_units_in_cart(self, root_id, exclude_id=None):
        """Base units already claimed by other cart lines drawing on the same stock pool."""
        ids = [int(k) for k in self.cart if k != exclude_id]
        total = Decimal('0')
        if not ids:
            return total
        rows = ProductVariant.objects.filter(id__in=ids).values_list('id', 'base_variant_id', 'units_per_pack')
        for vid, base_id, per_pack in rows:
            if (base_id or vid) == root_id:
                total += int(self.cart[str(vid)]['quantity']) * per_pack
        return total

    def add(self, variant_id, quantity=1, override_quantity=False):
        variant_id = str(variant_id)
        try:
            variant = ProductVariant.objects.select_related('product', 'base_variant').get(id=variant_id)
        except ProductVariant.DoesNotExist:
            return False

        branch = self._get_branch()

        current_qty_in_cart = int(self.cart.get(variant_id, {}).get('quantity', 0))
        requested_qty = max(1, int(quantity))
        new_total_qty = requested_qty if override_quantity else current_qty_in_cart + requested_qty

        pool = self._pool_quantity(variant, branch)
        others = self._base_units_in_cart(variant.stock_variant.id, exclude_id=variant_id)
        free_units = max(Decimal('0'), (pool - others) // variant.units_per_pack)

        if new_total_qty > free_units:
            raise InsufficientStockError(variant, int(free_units), new_total_qty)

        info = f" - {variant.size}" if variant.size else ""
        info += f" / {variant.color}" if variant.color else ""
        display_name = f"{variant.product.name}{info}"
        if variant.base_variant_id:
            display_name += f" [{variant.pack_label}]"

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

        ids = [int(i['variant_id']) for i in self.cart.values()]
        variants = {v.id: v for v in
                    ProductVariant.objects.select_related('base_variant').filter(id__in=ids)}
        pools, used = {}, {}

        for item in self.cart.values():
            vid = int(item['variant_id'])
            qty = int(item['quantity'])
            variant = variants.get(vid)
            if variant is None:
                problems.append({'variant_id': vid, 'name': item.get('name', ''), 'requested': qty,
                                 'available': 0, 'reason': 'Product no longer exists.'})
                continue

            root = variant.stock_variant.id
            if root not in pools:
                pools[root] = self._pool_quantity(variant, branch)
            before = used.get(root, Decimal('0'))
            free = max(Decimal('0'), (pools[root] - before) // variant.units_per_pack)

            if qty > free:
                problems.append({'variant_id': vid, 'name': item.get('name', ''), 'requested': qty,
                                 'available': int(free), 'reason': 'Not enough stock.'})
            used[root] = before + qty * variant.units_per_pack
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