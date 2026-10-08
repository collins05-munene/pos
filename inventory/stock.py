"""
The ONLY place stock quantities should change.

Stock is held once per BASE variant, in the product's base unit (tablets, kg ...).
A selling unit (packet, box, 500 g ...) converts to base units here, so purchases,
sales and adjustments in any unit always update the same single number.
"""
from decimal import Decimal

from .models import StockLevel


class InsufficientStock(Exception):
    def __init__(self, available):
        self.available = available
        super().__init__(f"Only {available} in stock (in base units).")


def change_stock(branch, variant, qty):
    """
    qty is in the variant's OWN unit: +10 boxes received, -3 tablets sold, -2 packets damaged.
    Positive adds, negative removes. Must be called inside transaction.atomic().
    """
    stock, _ = StockLevel.objects.select_for_update().get_or_create(
        branch=branch, variant=variant.stock_variant, defaults={'quantity': 0})
    new_qty = Decimal(stock.quantity) + variant.to_base_units(qty)
    if new_qty < 0:
        raise InsufficientStock(stock.quantity)
    stock.quantity = new_qty
    stock.save(update_fields=['quantity'])
    return stock


def available_units(branch, variant):
    """Whole units of THIS variant's own unit that current stock could supply."""
    qty = (StockLevel.objects.filter(branch=branch, variant=variant.stock_variant)
           .values_list('quantity', flat=True).first()) or Decimal('0')
    return Decimal(qty) // Decimal(variant.units_per_pack)