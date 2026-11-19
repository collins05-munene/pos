"""
Tables and orders: open a check, add items with customizations, discounts, move/merge/split.
Nothing here talks to the kitchen or takes money (see kitchen.py and billing.py).
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from tenants.context import get_current_tenant
from tenants.models import get_settings, next_number

from ..models import (LineModifier, MenuItem, Modifier, OrderLine, RestaurantOrder, Table, ZERO, get_restaurant_settings,
                      money)
from . import costing, stock
from .errors import RestaurantError

OPEN = RestaurantOrder.Status.OPEN
T = RestaurantOrder.Type


# ======================================================================================
# helpers
# ======================================================================================
def _lock(order):
    """Reload the order under a row lock; every change goes through this so two tablets can't clash."""
    return RestaurantOrder.objects.select_for_update().get(pk=order.pk)


def _require_open(order):
    if order.status != OPEN:
        raise RestaurantError(f"Order {order.number} is {order.get_status_display().lower()} and can't be changed.")


def recalculate(order):
    """Bring subtotal, discount, service charge and total in step with the lines."""
    lines = [l for l in order.lines.all() if l.status != OrderLine.Status.VOID]
    subtotal = sum((l.line_total for l in lines), ZERO)
    discount = ZERO
    if order.discount_type == RestaurantOrder.DiscountType.PERCENT:
        discount = money(subtotal * Decimal(order.discount_value) / 100)
    elif order.discount_type == RestaurantOrder.DiscountType.AMOUNT:
        discount = min(money(order.discount_value), subtotal)
    net = subtotal - discount
    service = money(net * Decimal(order.service_charge_percent) / 100) if order.service_charge_percent else ZERO
    order.subtotal = subtotal
    order.discount_total = discount
    order.service_charge = service
    order.total = net + service + Decimal(order.delivery_fee)
    order.save(update_fields=["subtotal", "discount_total", "service_charge", "total", "updated_at"])
    return order


# ======================================================================================
# opening an order
# ======================================================================================
def table_open_orders(table):
    return RestaurantOrder.objects.filter(table=table, status=OPEN)


@transaction.atomic
def create_order(*, user, branch, order_type=T.DINE_IN, table=None, guests=1, customer_name="", customer_phone="",
                 delivery_address="", delivery_notes="", delivery_zone=None, notes="", source="POS",
                 external_ref="", needs_acceptance=False, scheduled_for=None, waiter=None):
    tenant = get_current_tenant()
    rs = get_restaurant_settings(tenant)

    if order_type == T.DINE_IN:
        if table is None:
            raise RestaurantError("Choose a table for a dine-in order.")
        if table.branch_id != branch.pk or not table.is_active:
            raise RestaurantError("That table isn't available at this branch.")
        if table.merged_into_id:
            raise RestaurantError(f"{table.name} is joined to {table.merged_into.name}. Use {table.merged_into.name}.")
        if table.hold == Table.Hold.BLOCKED:
            raise RestaurantError(f"{table.name} is out of service.")
        if table.hold != Table.Hold.NONE:          # seating a reserved / being-cleaned table clears the hold
            table.hold, table.reserved_for = Table.Hold.NONE, ""
            table.save(update_fields=["hold", "reserved_for"])
        if waiter is None:
            waiter = table.waiter or (user if user.role == "WAITER" else None)
    else:
        table = None
        if order_type == T.DELIVERY and not (customer_phone.strip() and delivery_address.strip()):
            raise RestaurantError("A delivery needs the customer's phone number and address.")
        if order_type == T.TAKEAWAY and not (customer_name.strip() or customer_phone.strip()):
            raise RestaurantError("Enter the customer's name or phone so the order can be collected.")

    pct = rs.service_charge_percent
    if rs.service_charge_dine_in_only and order_type != T.DINE_IN:
        pct = Decimal(0)

    fee = Decimal(0)
    if order_type == T.DELIVERY:
        fee = Decimal(delivery_zone.fee if delivery_zone else rs.default_delivery_fee)

    order = RestaurantOrder.objects.create(
        number=next_number(tenant, "rorder", "ORD-", 5), branch=branch, order_type=order_type, table=table,
        guests=max(1, int(guests or 1)), waiter=waiter, created_by=user, customer_name=customer_name.strip(),
        customer_phone=customer_phone.strip(), delivery_address=delivery_address.strip(),
        delivery_notes=delivery_notes.strip(), delivery_zone=delivery_zone, notes=notes.strip(),
        source=source, external_ref=external_ref, needs_acceptance=needs_acceptance,
        scheduled_for=scheduled_for, service_charge_percent=pct, delivery_fee=fee)
    return recalculate(order)


@transaction.atomic
def update_details(order, **fields):
    """Edit guest count, notes, customer and delivery details."""
    order = _lock(order)
    _require_open(order)
    allowed = {"guests", "notes", "customer_name", "customer_phone", "delivery_address", "delivery_notes",
               "delivery_zone", "scheduled_for"}
    for k, v in fields.items():
        if k in allowed:
            setattr(order, k, v)
    if order.order_type == T.DELIVERY:
        if not (order.customer_phone.strip() and order.delivery_address.strip()):
            raise RestaurantError("A delivery needs the customer's phone number and address.")
        if "delivery_zone" in fields and fields["delivery_zone"] is not None:
            order.delivery_fee = fields["delivery_zone"].fee
    order.save()
    return recalculate(order)


@transaction.atomic
def set_delivery_fee(order, fee):
    order = _lock(order)
    _require_open(order)
    if order.order_type != T.DELIVERY:
        raise RestaurantError("Only delivery orders have a delivery fee.")
    fee = money(fee)
    if fee < 0:
        raise RestaurantError("The delivery fee can't be negative.")
    order.delivery_fee = fee
    order.save(update_fields=["delivery_fee"])
    return recalculate(order)


# ======================================================================================
# lines
# ======================================================================================
def _modifier_key(mods):
    return tuple(sorted(m.pk for m in mods))


@transaction.atomic
def add_line(order, menu_item, quantity=1, modifier_ids=(), note=""):
    order = _lock(order)
    _require_open(order)
    if order.needs_acceptance:
        raise RestaurantError("Accept this online order before changing it.")
    if not menu_item.is_active or not menu_item.variant.is_active:
        raise RestaurantError(f"{menu_item.name} is no longer on the menu.")
    if not menu_item.is_available:
        raise RestaurantError(f"{menu_item.name} is sold out (86'd).")
    quantity = int(quantity)
    if quantity < 1:
        raise RestaurantError("Quantity must be at least 1.")

    mods = list(Modifier.objects.filter(pk__in=list(modifier_ids), is_active=True).select_related("ingredient__variant"))
    if len(mods) != len(set(modifier_ids)):
        raise RestaurantError("One of the chosen options isn't available.")
    for m in mods:
        if m.menu_items.exists() and not m.menu_items.filter(pk=menu_item.pk).exists():
            raise RestaurantError(f"'{m.name}' isn't offered on {menu_item.name}.")

    note = (note or "").strip()[:200]
    key = _modifier_key(mods)
    for line in order.lines.filter(status=OrderLine.Status.DRAFT, menu_item=menu_item, note=note):
        if _modifier_key([lm.modifier for lm in line.modifiers.all() if lm.modifier_id]) == key and \
                line.modifiers.count() == len(mods):
            line.quantity += quantity
            line.save(update_fields=["quantity"])
            recalculate(order)
            return line

    base = Decimal(menu_item.variant.retail_price)
    extras = sum((Decimal(m.price) for m in mods), ZERO)
    cost = costing.recipe_cost(menu_item) + sum((costing.modifier_cost(m) for m in mods), ZERO)
    line = OrderLine.objects.create(
        order=order, menu_item=menu_item, name=menu_item.name, quantity=quantity, base_price=base,
        unit_price=money(base + extras), unit_cost=money(cost), note=note, station=menu_item.station)
    for m in mods:
        LineModifier.objects.create(
            line=line, modifier=m, name=m.name, kind=m.kind, price=m.price,
            ingredient=m.ingredient, ingredient_qty=m.ingredient_qty)
    recalculate(order)
    return line


@transaction.atomic
def set_line_quantity(line, quantity):
    order = _lock(line.order)
    _require_open(order)
    line = OrderLine.objects.select_for_update().get(pk=line.pk)
    if line.status != OrderLine.Status.DRAFT:
        raise RestaurantError("This item was already sent to the kitchen. Add another one or ask a manager to cancel it.")
    quantity = int(quantity)
    if quantity <= 0:
        line.delete()
    else:
        line.quantity = quantity
        line.save(update_fields=["quantity"])
    return recalculate(order)


@transaction.atomic
def void_line(line, user, reason=""):
    """
    Remove an item. Not-yet-sent items just disappear. Sent items are kept on the check as cancelled
    (the kitchen sees them struck through), and any stock already taken is put back.
    """
    order = _lock(line.order)
    _require_open(order)
    line = OrderLine.objects.select_for_update().get(pk=line.pk)
    if line.status == OrderLine.Status.VOID:
        return order
    if line.status == OrderLine.Status.DRAFT:
        line.delete()
        return recalculate(order)
    stock.reverse_line(line)
    line.status = OrderLine.Status.VOID
    line.voided_at = timezone.now()
    line.void_reason = (reason or "Cancelled")[:120]
    line.save(update_fields=["status", "voided_at", "void_reason"])
    if line.ticket_id:
        from . import kitchen
        kitchen.refresh_ticket(line.ticket)
    return recalculate(order)


@transaction.atomic
def cancel_order(order, user, reason=""):
    """Cancel a whole check that has no payments. Stock already used is returned."""
    order = _lock(order)
    _require_open(order)
    if order.payments.filter(is_void=False).exists():
        raise RestaurantError("This order already has payments. Cancel it only after they are voided.")
    from . import kitchen
    for line in list(order.lines.exclude(status=OrderLine.Status.VOID)):
        if line.status == OrderLine.Status.DRAFT:
            line.delete()
            continue
        stock.reverse_line(line)
        line.status = OrderLine.Status.VOID
        line.voided_at = timezone.now()
        line.void_reason = (reason or "Order cancelled")[:120]
        line.save(update_fields=["status", "voided_at", "void_reason"])
    for t in order.tickets.all():
        kitchen.refresh_ticket(t)
    order.status = RestaurantOrder.Status.VOID
    order.void_reason = (reason or "")[:200]
    order.closed_at = timezone.now()
    order.save(update_fields=["status", "void_reason", "closed_at"])
    return recalculate(order)


# ======================================================================================
# discount
# ======================================================================================
@transaction.atomic
def set_discount(order, user, discount_type, value=0, reason=""):
    """
    Same safety rules as the shop's discount settings: a master switch, a hard percentage cap, and
    (unless allowed) never selling the whole bill below cost.
    """
    order = _lock(order)
    _require_open(order)
    ts = get_settings(order.tenant)
    if discount_type == RestaurantOrder.DiscountType.NONE or not Decimal(value or 0):
        order.discount_type, order.discount_value, order.discount_reason, order.discount_by = \
            RestaurantOrder.DiscountType.NONE, ZERO, "", None
        order.save(update_fields=["discount_type", "discount_value", "discount_reason", "discount_by"])
        return recalculate(order)
    if not ts.discounts_enabled:
        raise RestaurantError("Discounts are switched off in Settings.")
    value = Decimal(value)
    if value <= 0:
        raise RestaurantError("Enter a discount above zero.")
    recalculate(order)
    subtotal = order.subtotal
    if subtotal <= 0:
        raise RestaurantError("Add items before giving a discount.")
    amount = money(subtotal * value / 100) if discount_type == RestaurantOrder.DiscountType.PERCENT else money(value)
    if amount > subtotal:
        raise RestaurantError("The discount can't be more than the bill.")
    pct = amount / subtotal * 100
    cap = Decimal(ts.max_discount_percent)
    if user.role == "MANAGER":
        cap = min(cap, Decimal(ts.manager_max_discount_percent))
    if pct > cap:
        raise RestaurantError(f"You can discount at most {cap.normalize():f}% of a bill.")
    if ts.below_cost_policy == ts.BelowCost.LIMIT:
        cost = sum((l.unit_cost * l.quantity for l in order.active_lines), ZERO)
        if subtotal - amount < cost:
            raise RestaurantError("That discount would sell the bill below cost (see Settings > Discounts).")
    order.discount_type, order.discount_value = discount_type, value
    order.discount_reason, order.discount_by = (reason or "Bill discount")[:120], user
    order.save(update_fields=["discount_type", "discount_value", "discount_reason", "discount_by"])
    return recalculate(order)


# ======================================================================================
# moving things around
# ======================================================================================
@transaction.atomic
def transfer_order(order, to_table):
    order = _lock(order)
    _require_open(order)
    if order.order_type != T.DINE_IN:
        raise RestaurantError("Only dine-in orders sit at a table.")
    if to_table.branch_id != order.branch_id or not to_table.is_active:
        raise RestaurantError("That table isn't available.")
    if to_table.merged_into_id:
        raise RestaurantError(f"{to_table.name} is joined to {to_table.merged_into.name}. Choose {to_table.merged_into.name}.")
    if to_table.hold == Table.Hold.BLOCKED:
        raise RestaurantError(f"{to_table.name} is out of service.")
    if to_table.pk == order.table_id:
        raise RestaurantError("The order is already at that table.")
    if table_open_orders(to_table).exists():
        raise RestaurantError(f"{to_table.name} already has an open order. Merge the two orders instead.")
    if to_table.hold != Table.Hold.NONE:
        to_table.hold, to_table.reserved_for = Table.Hold.NONE, ""
        to_table.save(update_fields=["hold", "reserved_for"])
    order.table = to_table
    if to_table.waiter_id:
        order.waiter = to_table.waiter
    order.save(update_fields=["table", "waiter"])
    return order


@transaction.atomic
def merge_orders(source, target):
    """Move every item of `source` onto `target` (e.g. two checks at neighbouring tables become one)."""
    source, target = _lock(source), _lock(target)
    _require_open(source)
    _require_open(target)
    if source.pk == target.pk:
        raise RestaurantError("Choose a different order to merge into.")
    if source.branch_id != target.branch_id:
        raise RestaurantError("Orders from different branches can't be merged.")
    if source.payments.filter(is_void=False).exists():
        raise RestaurantError(f"{source.number} already has payments and can't be merged.")
    OrderLine.objects.filter(order=source).update(order=target)
    source.tickets.update(order=target)
    source.status = RestaurantOrder.Status.VOID
    source.void_reason = f"Merged into {target.number}"
    source.closed_at = timezone.now()
    source.save(update_fields=["status", "void_reason", "closed_at"])
    recalculate(source)
    return recalculate(target)


@transaction.atomic
def split_order(order, line_ids, user):
    """Pull some items out into a new check at the same table (e.g. one guest pays separately)."""
    order = _lock(order)
    _require_open(order)
    if order.payments.filter(is_void=False).exists():
        raise RestaurantError("Take payments after splitting, not before.")
    chosen = list(order.lines.exclude(status=OrderLine.Status.VOID).filter(pk__in=list(line_ids)))
    if not chosen:
        raise RestaurantError("Tick the items that go on the new bill.")
    if len(chosen) == len(order.active_lines):
        raise RestaurantError("Leave at least one item on the original bill.")
    child = RestaurantOrder.objects.create(
        number=next_number(order.tenant, "rorder", "ORD-", 5), branch=order.branch, order_type=order.order_type,
        table=order.table, guests=1, waiter=order.waiter, created_by=user, split_from=order, source=order.source,
        customer_name=order.customer_name, customer_phone=order.customer_phone,
        service_charge_percent=order.service_charge_percent, notes=f"Split from {order.number}")
    for line in chosen:
        line.order = child
        line.save(update_fields=["order"])
    recalculate(order)
    recalculate(child)
    return child


def equal_shares(order, parts):
    """The bill cut into `parts` equal amounts (cents that don't divide go to the last share)."""
    parts = int(parts)
    if parts < 2 or parts > 50:
        raise RestaurantError("Split between 2 and 50 people.")
    each = (Decimal(order.balance) / parts).quantize(Decimal("0.01"), rounding="ROUND_DOWN")
    shares = [each] * parts
    shares[-1] = money(Decimal(order.balance) - each * (parts - 1))
    return shares


# ======================================================================================
# tables
# ======================================================================================
@transaction.atomic
def merge_tables(primary, others):
    if primary.merged_into_id:
        raise RestaurantError(f"{primary.name} is itself joined to another table.")
    for t in others:
        if t.pk == primary.pk:
            continue
        if t.branch_id != primary.branch_id:
            raise RestaurantError("Tables from different branches can't be joined.")
        if table_open_orders(t).exists():
            raise RestaurantError(f"{t.name} has an open order. Transfer or pay it first.")
        for child in list(t.merged_tables.all()):       # joining a group: keep a single level
            child.merged_into = primary
            child.save(update_fields=["merged_into"])
        t.merged_into = primary
        t.hold, t.reserved_for = Table.Hold.NONE, ""
        t.save(update_fields=["merged_into", "hold", "reserved_for"])
    return primary


@transaction.atomic
def split_tables(primary):
    n = primary.merged_tables.update(merged_into=None)
    if not n:
        raise RestaurantError(f"{primary.name} isn't joined to any other table.")
    return n


def assign_waiter(table, waiter):
    if waiter is not None and (waiter.tenant_id != table.tenant_id or waiter.role not in ("WAITER", "MANAGER", "ADMIN", "CASHIER")):
        raise RestaurantError("Choose one of your staff.")
    table.waiter = waiter
    table.save(update_fields=["waiter"])
    for t in table.merged_tables.all():
        t.waiter = waiter
        t.save(update_fields=["waiter"])
    table_open_orders(table).update(waiter=waiter)
    return table


def set_hold(table, hold, reserved_for=""):
    if hold != Table.Hold.NONE and table_open_orders(table).exists():
        raise RestaurantError(f"{table.name} has an open order.")
    table.hold = hold
    table.reserved_for = reserved_for[:80] if hold == Table.Hold.RESERVED else ""
    table.save(update_fields=["hold", "reserved_for"])
    return table


# ======================================================================================
# floor snapshot (what the live floor plan polls)
# ======================================================================================
def table_state(table, orders):
    if table.merged_into_id:
        return "MERGED"
    if table.hold == Table.Hold.BLOCKED:
        return "BLOCKED"
    if orders:
        return "OCCUPIED"
    if table.hold == Table.Hold.RESERVED:
        return "RESERVED"
    if table.hold == Table.Hold.CLEANING:
        return "CLEANING"
    return "AVAILABLE"


def floor_snapshot(branch):
    from ..models import KitchenTicket
    tables = list(Table.objects.filter(branch=branch, is_active=True)
                  .select_related("zone", "waiter", "merged_into").prefetch_related("merged_tables"))
    open_orders = list(RestaurantOrder.objects.filter(branch=branch, status=OPEN, order_type=T.DINE_IN)
                       .select_related("waiter").prefetch_related("lines", "payments"))
    by_table = {}
    for o in open_orders:
        by_table.setdefault(o.table_id, []).append(o)
    ready_orders = set(KitchenTicket.objects.filter(
        branch=branch, status=KitchenTicket.Status.READY, order__in=open_orders).values_list("order_id", flat=True))
    now = timezone.now()
    out = []
    for t in tables:
        orders = by_table.get(t.pk, [])
        state = table_state(t, orders)
        out.append({
            "id": t.pk, "name": t.name, "label": t.label, "seats": t.seats, "capacity": t.capacity,
            "shape": t.shape, "x": t.x, "y": t.y, "w": t.w, "h": t.h,
            "zone_id": t.zone_id, "zone": t.zone.name if t.zone_id else "",
            "state": state, "reserved_for": t.reserved_for,
            "merged_into": t.merged_into_id, "merged_into_name": t.merged_into.name if t.merged_into_id else "",
            "waiter_id": t.waiter_id, "waiter": (t.waiter.get_full_name() or t.waiter.username.split("__")[-1]) if t.waiter_id else "",
            "food_ready": any(o.pk in ready_orders for o in orders),
            "orders": [{
                "id": o.pk, "number": o.number, "total": str(o.total), "balance": str(o.balance),
                "items": len(o.active_lines), "minutes": int((now - o.created_at).total_seconds() // 60),
                "waiter": (o.waiter.get_full_name() or o.waiter.username.split("__")[-1]) if o.waiter_id else "",
            } for o in orders],
        })
    return out
