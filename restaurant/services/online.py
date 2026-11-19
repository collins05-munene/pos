"""
Online ordering integration.

A website, app or aggregator talks to the restaurant through two small JSON endpoints
(restaurant/api_views.py).  They are authenticated with a per-restaurant API key.  Keys are shown
once and only a hash is stored, like a password.
"""
import hashlib
import hmac
import secrets
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction

from products.models import ProductVariant

from ..models import (BillPayment, DeliveryZone, MenuItem, Modifier, RestaurantOrder, RestaurantSettings,
                      get_restaurant_settings)
from . import billing, kitchen, orders
from .errors import RestaurantError

KEY_PREFIX = "us"


def _hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_api_key(tenant):
    """Create (or replace) the restaurant's key. Returns the full key ONCE; only its hash is stored."""
    rs = get_restaurant_settings(tenant)
    key_id = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    rs.online_key_id, rs.online_key_hash = key_id, _hash(secret)
    rs.save(update_fields=["online_key_id", "online_key_hash"])
    return f"{KEY_PREFIX}_{key_id}_{secret}"


def revoke_api_key(tenant):
    rs = get_restaurant_settings(tenant)
    rs.online_key_id, rs.online_key_hash = "", ""
    rs.save(update_fields=["online_key_id", "online_key_hash"])


def authenticate(header_value):
    """'Bearer us_<id>_<secret>' -> the tenant it belongs to, or None."""
    if not header_value or not header_value.lower().startswith("bearer "):
        return None
    key = header_value[7:].strip()
    parts = key.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        return None
    rs = RestaurantSettings.all_objects.select_related("tenant").filter(online_key_id=parts[1]).first()
    if rs is None or not rs.online_key_hash or not hmac.compare_digest(rs.online_key_hash, _hash(parts[2])):
        return None
    if not rs.online_ordering_enabled:
        return None
    return rs.tenant


def public_menu():
    items = (MenuItem.objects.filter(is_active=True, variant__is_active=True)
             .select_related("variant__product__category", "station"))
    mods = list(Modifier.objects.filter(is_active=True).prefetch_related("menu_items"))
    out = []
    for it in items:
        out.append({
            "id": it.pk, "sku": it.variant.sku, "name": it.name, "category": it.category.name,
            "kind": it.kind, "price": str(it.price), "available": it.is_available,
            "description": it.variant.product.description,
            "options": [{"id": m.pk, "name": m.name, "kind": m.kind, "price": str(m.price)}
                        for m in mods if not m.menu_items.all() or it in m.menu_items.all()],
        })
    zones = [{"id": z.pk, "name": z.name, "fee": str(z.fee)} for z in DeliveryZone.objects.filter(is_active=True)]
    return {"items": out, "delivery_zones": zones}


@transaction.atomic
def create_order_from_payload(tenant, branch, payload):
    """
    payload = {
      "external_ref": "WEB-1042",                 (required; makes retries safe)
      "type": "DELIVERY" | "TAKEAWAY",
      "customer": {"name": "...", "phone": "..."},
      "address": "...", "notes": "...", "delivery_zone_id": 3,
      "items": [{"menu_item_id": 5 | "sku": "M00012", "quantity": 2, "note": "...", "option_ids": [1, 4]}],
      "paid": true, "payment_reference": "..."    (prepaid online orders)
    }
    Returns (order, created).  Sending the same external_ref again returns the first order.
    """
    ref = str(payload.get("external_ref") or "").strip()[:80]
    if not ref:
        raise RestaurantError("external_ref is required.")
    existing = RestaurantOrder.objects.filter(external_ref=ref).first()
    if existing:
        return existing, False

    kind = str(payload.get("type") or "DELIVERY").upper()
    if kind not in (RestaurantOrder.Type.DELIVERY, RestaurantOrder.Type.TAKEAWAY):
        raise RestaurantError("type must be DELIVERY or TAKEAWAY.")
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise RestaurantError("items must be a non-empty list.")
    cust = payload.get("customer") or {}
    zone = None
    if payload.get("delivery_zone_id"):
        zone = DeliveryZone.objects.filter(pk=payload["delivery_zone_id"], is_active=True).first()
        if zone is None:
            raise RestaurantError("Unknown delivery zone.")

    rs = get_restaurant_settings(tenant)
    actor = _system_user(tenant)
    try:
        order = orders.create_order(
            user=actor, branch=branch, order_type=kind, customer_name=str(cust.get("name") or ""),
            customer_phone=str(cust.get("phone") or ""), delivery_address=str(payload.get("address") or ""),
            notes=str(payload.get("notes") or "")[:300], delivery_zone=zone, source="ONLINE", external_ref=ref,
            needs_acceptance=not rs.auto_accept_online)
    except IntegrityError:
        existing = RestaurantOrder.objects.filter(external_ref=ref).first()
        if existing:
            return existing, False
        raise

    # lines are added before the acceptance flag matters, so temporarily allow them
    order.needs_acceptance = False
    order.save(update_fields=["needs_acceptance"])
    for raw in items:
        try:
            qty = int(raw.get("quantity", 1))
        except (TypeError, ValueError):
            raise RestaurantError("Item quantity must be a whole number.")
        item = None
        if raw.get("menu_item_id"):
            item = MenuItem.objects.filter(pk=raw["menu_item_id"]).first()
        elif raw.get("sku"):
            item = MenuItem.objects.filter(variant__sku=str(raw["sku"])).first()
        if item is None:
            raise RestaurantError(f"Unknown menu item: {raw.get('menu_item_id') or raw.get('sku')}.")
        orders.add_line(order, item, qty, modifier_ids=raw.get("option_ids") or [], note=str(raw.get("note") or ""))

    order = RestaurantOrder.objects.get(pk=order.pk)
    if not rs.auto_accept_online:
        order.needs_acceptance = True
        order.save(update_fields=["needs_acceptance"])
        from notifications.models import Notification
        from notifications.utils import notify
        notify(tenant, f"New online order {order.number}",
               f"{order.customer_name or order.customer_phone}: KSH {order.total}. Accept it to send it to the kitchen.",
               level=Notification.Level.INFO, roles=("ADMIN", "MANAGER", "CASHIER"))
    else:
        kitchen.send_to_kitchen(order, actor)

    if payload.get("paid"):
        order = RestaurantOrder.objects.get(pk=order.pk)
        BillPayment.objects.create(order=order, method=BillPayment.Method.ONLINE, amount=order.total,
                                   reference=str(payload.get("payment_reference") or "")[:60], received_by=None)
    return RestaurantOrder.objects.get(pk=order.pk), True


def _system_user(tenant):
    """Online orders are attributed to the owner account (there is no human at the keyboard)."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    u = User.objects.filter(tenant=tenant, is_owner=True).first() or User.objects.filter(tenant=tenant, role="ADMIN").first()
    if u is None:
        raise RestaurantError("This business has no admin account to attribute online orders to.")
    return u
