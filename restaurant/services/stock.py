"""
Ingredient and stocked-drink deduction, according to the restaurant's configured policy, and waste.

All quantity changes go through inventory.stock.change_stock (the one place stock may change).
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from inventory.models import StockAdjustment
from inventory.stock import InsufficientStock, change_stock

from ..models import MenuItem, OrderLine, RestaurantSettings, StockUse, WasteRecord, get_restaurant_settings, money
from . import costing
from .errors import RestaurantError

log = logging.getLogger(__name__)

POLICY_RANK = {"ON_SEND": 0, "ON_READY": 1, "ON_SERVED": 2, "ON_PAID": 3}
EVENT_RANK = {"SEND": 0, "READY": 1, "SERVED": 2, "PAID": 3}


def _alert_low_stock(tenant, branch_id, variants):
    from notifications.checks import check_stock_after_sale
    variants = list(variants)
    if variants:
        transaction.on_commit(lambda: check_stock_after_sale(tenant, branch_id, variants))


def deduct_line(line, *, allow_negative):
    """Take the stock this line uses. Safe to call twice: it only ever deducts once."""
    if line.stock_deducted or line.status == OrderLine.Status.VOID:
        return False
    order = line.order
    # Use the snapshot on the line (the modifier may have been edited since it was ordered).
    modifiers = []
    for lm in line.modifiers.select_related("ingredient__variant"):
        modifiers.append(_SnapshotModifier(lm))
    usage = costing.line_usage(line.menu_item, line.quantity, modifiers)
    touched = []
    with transaction.atomic():
        for variant, qty in usage.items():
            try:
                change_stock(order.branch, variant, -qty, allow_negative=allow_negative)
            except InsufficientStock as exc:
                raise RestaurantError(
                    f"Not enough {variant.product.name} in stock for {line.name} "
                    f"(only {exc.available} left). Receive stock or switch on 'keep serving when stock is out'.")
            StockUse.objects.create(branch=order.branch, line=line, variant=variant, quantity=qty)
            touched.append(variant)
        line.stock_deducted = True
        line.save(update_fields=["stock_deducted"])
    _alert_low_stock(order.tenant, order.branch_id, touched)
    return True


class _SnapshotModifier:
    """Quacks like Modifier, but reads the values frozen on the order line."""
    def __init__(self, lm):
        self.kind = lm.kind
        self.ingredient_id = lm.ingredient_id
        self.ingredient = lm.ingredient
        self.ingredient_qty = lm.ingredient_qty


def apply_policy(lines, event, *, settings_obj=None):
    """
    Called at SEND / READY / SERVED / PAID.  A line is deducted the first time an event at or after
    the configured policy point is reached, so a line that skips a step is still deducted once.
    """
    s = settings_obj or get_restaurant_settings()
    threshold = POLICY_RANK[s.deduction_policy]
    if EVENT_RANK[event] < threshold:
        return 0
    allow = s.allow_negative_stock or event == "PAID"      # never block taking money for goods already served
    n = 0
    for line in lines:
        if deduct_line(line, allow_negative=allow):
            n += 1
    return n


def reverse_line(line):
    """A cancelled line puts back exactly what it took."""
    if not line.stock_deducted:
        return 0
    n, touched = 0, []
    with transaction.atomic():
        for use in line.stock_uses.select_related("variant").filter(reversed_at__isnull=True):
            change_stock(line.order.branch, use.variant, use.quantity, allow_negative=True)
            use.reversed_at = timezone.now()
            use.save(update_fields=["reversed_at"])
            touched.append(use.variant)
            n += 1
        line.stock_deducted = False
        line.save(update_fields=["stock_deducted"])
    return n


# ------------------------------------------------------------------------------ waste
def record_waste(*, branch, user, reason, notes="", ingredient=None, quantity=None, menu_item=None, portions=None):
    """
    Ingredient waste: `quantity` in the ingredient's recipe unit.
    Dish waste (a burnt burger): `portions` -- takes the whole recipe out of stock.
    Either way a StockAdjustment is written too, so the normal stock history shows it.
    """
    if (ingredient is None) == (menu_item is None):
        raise RestaurantError("Choose either an ingredient or a dish.")
    adj_type = "EXPIRY" if reason == WasteRecord.Reason.EXPIRED else "DAMAGE"
    with transaction.atomic():
        if ingredient is not None:
            qty = Decimal(quantity or 0)
            if qty <= 0:
                raise RestaurantError("Enter how much was wasted.")
            usage = {ingredient.variant: costing.q3(qty / Decimal(ingredient.units_per_base))}
            subject_qty = qty
        else:
            portions = Decimal(portions or 0)
            if portions <= 0:
                raise RestaurantError("Enter how many portions were wasted.")
            usage = costing.line_usage(menu_item, portions)
            if not usage:
                raise RestaurantError(f"{menu_item.name} has no recipe or stock, so there is nothing to take out of stock.")
            subject_qty = portions
        cost = Decimal(0)
        for variant, qty in usage.items():
            change_stock(branch, variant, -qty, allow_negative=True)
            cost += qty * Decimal(variant.cost_price)
            adj = StockAdjustment.objects.create(
                branch=branch, variant=variant, adjustment_type=adj_type, quantity_changed=-qty,
                reason=f"Restaurant waste ({reason.lower()}) {notes}".strip()[:500], user=user)
            from accounting.hooks import notify      # write-off to Stock Losses (after commit)
            notify("stock_adjustment", adj.pk)
        rec = WasteRecord.objects.create(
            branch=branch, ingredient=ingredient, menu_item=menu_item, quantity=subject_qty, reason=reason,
            notes=notes[:200], cost=money(cost), recorded_by=user)
    _alert_low_stock(branch.tenant, branch.pk, usage.keys())
    return rec
