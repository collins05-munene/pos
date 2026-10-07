from .models import StockLevel

class InsufficientStock(Exception):
    pass

def change_stock(branch, variant, qty):
    """qty is in the variant's own unit (+2 boxes received, -3 tablets sold). Call inside transaction.atomic()."""
    stock, _ = StockLevel.objects.select_for_update().get_or_create(
        branch=branch, variant=variant.stock_variant, defaults={'quantity': 0})
    new_qty = stock.quantity + variant.to_base_units(qty)
    if new_qty < 0:
        raise InsufficientStock(f"Only {stock.quantity} base units in stock.")
    stock.quantity = new_qty
    stock.save(update_fields=['quantity'])
    return stock

def available_units(branch, variant):
    qty = (StockLevel.objects.filter(branch=branch, variant=variant.stock_variant)
           .values_list('quantity', flat=True).first()) or 0
    return qty // variant.units_per_pack