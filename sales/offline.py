"""
sales/offline.py

Server half of offline selling.

    build_snapshot()        what a till needs to keep selling with no connection
    process_offline_sale()  replays ONE sale that was rung up offline

Design rules (see also sales/utils.complete_pos_sale):

* A sale that was rung up offline has ALREADY HAPPENED: the customer paid cash and
  walked out with the goods. Sync therefore never "declines" a sale because stock
  or a price changed in the meantime. It records it, lets stock go negative if it
  must, and flags it for a manager. It only refuses a sale that is structurally
  impossible to record (unknown product, no cash register to put the cash in ...).
* Everything is idempotent on the browser-generated client_uuid.
* The server never trusts the client for what the client has no business knowing:
  cost price comes from the database, totals are recomputed from the lines, and the
  tenant always comes from the logged-in user.
* Cost prices are never put in the snapshot.
"""
import hashlib
import json
import logging
import uuid as uuidlib
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from inventory.models import StockLevel
from products.models import Category, ProductVariant, fmt_qty
from promotions.pricing import PriceBook
from tenants.models import get_settings
from users.mixins import MANAGEMENT_ROLES

from .cart import CartItem
from .models import CashRegisterSession, Order
from .utils import complete_pos_sale, resolve_user_branch

log = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
MAX_LINES = 200
MAX_QTY = 100_000
TWO_PLACES = Decimal("0.01")


def max_offline_hours(tenant=None):
    """How long a till may sell offline on one snapshot before it must reconnect (Settings -> Offline selling)."""
    if tenant is not None:
        return int(get_settings(tenant).offline_max_hours)
    return int(getattr(settings, "POS_OFFLINE_MAX_HOURS", 72))


def max_sale_age_days():
    """Client clocks older than this are not believed when stamping a synced sale."""
    return int(getattr(settings, "POS_OFFLINE_MAX_SALE_AGE_DAYS", 7))


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------
def build_snapshot(user, tenant):
    """
    Everything the offline terminal needs, as plain JSON-able data. Tenant scoping
    comes from the default managers (the active tenant is set by TenantMiddleware).
    """
    branch = resolve_user_branch(user)
    session = CashRegisterSession.get_active_session(branch) if branch else None

    variants = []
    # "price" is what the customer pays per unit TODAY (running discounts already taken off), so the
    # offline till needs no discount logic of its own. When the sale syncs, the server checks the
    # price against its own rules (see sales.utils.price_sale_line).
    book = PriceBook(tenant=tenant, branch=branch)
    rows = (ProductVariant.objects
            .filter(is_active=True, product__is_active=True)
            .select_related("product", "product__category")
            .order_by("product__name", "sku"))
    for v in rows:
        detail = " / ".join(x for x in (v.size, v.color) if x)
        line = book.price(v, 1)
        entry = {
            "id": v.id,
            "sku": v.sku,
            "name": v.product.name,
            "detail": detail,
            "pack": (f'{v.unit_name} \u00d7{fmt_qty(v.units_per_pack)}' if v.base_variant_id else ""),
            "price": str(line.net_price),
            "per_pack": str(v.units_per_pack),
            "base": v.base_variant_id or v.id,
            "cat": v.product.category_id,
            "pcat": v.product.category.parent_id,
        }
        if line.has_discount:
            entry["was"] = str(line.list_price)
            entry["promo"] = line.discount_name
        variants.append(entry)

    stock = {}
    if branch:
        stock = {str(vid): str(q) for vid, q in
                 StockLevel.objects.filter(branch=branch).values_list("variant_id", "quantity")}

    body = {
        "version": SNAPSHOT_VERSION,
        "tenant": {"id": tenant.pk, "name": tenant.name},
        "branch": {"id": branch.pk, "name": branch.name} if branch else None,
        "user": {
            "id": user.pk,
            "name": user.get_full_name().strip() or user.get_username().split("__")[-1],
            "role": user.role,
        },
        "session": ({"id": session.pk, "opened_at": session.opened_at.isoformat()}
                    if session else None),
        "max_offline_hours": max_offline_hours(tenant),
        "categories": list(Category.objects.values("id", "name", "parent_id")),
        "variants": variants,
        "stock": stock,
    }
    # The ETag covers the content only, so an unchanged catalogue costs a 304.
    etag = hashlib.sha1(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:24]
    body["etag"] = etag
    body["generated_at"] = timezone.now().isoformat()
    return body, etag


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------
class Rejected(Exception):
    """This sale can never be recorded as sent. Not retryable."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _dec(value, field, *, minimum=Decimal("0")):
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise Rejected("bad_number", f"{field} is not a valid number.")
    if not d.is_finite() or d < minimum:
        raise Rejected("bad_number", f"{field} is out of range.")
    return d.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _result(order, status):
    receipt = getattr(order, "receipt", None)
    return {
        "uuid": str(order.client_uuid),
        "status": status,
        "invoice": order.invoice_number,
        "order_id": order.pk,
        "receipt_id": receipt.pk if receipt else None,
        "receipt_number": receipt.receipt_number if receipt else None,
        "flags": order.offline_flags,
    }


def _parse_lines(raw_lines):
    if not isinstance(raw_lines, list) or not raw_lines:
        raise Rejected("no_lines", "The sale has no items.")
    if len(raw_lines) > MAX_LINES:
        raise Rejected("too_many_lines", "The sale has too many lines.")
    merged = {}
    for raw in raw_lines:
        try:
            vid = int(raw["variant_id"])
            qty = int(raw["quantity"])
        except (KeyError, TypeError, ValueError):
            raise Rejected("bad_line", "A line is missing its product or quantity.")
        if not 1 <= qty <= MAX_QTY:
            raise Rejected("bad_quantity", "A line has an invalid quantity.")
        price = _dec(raw.get("price"), "price")
        key = (vid, price)
        merged[key] = merged.get(key, 0) + qty
    return [(vid, price, qty) for (vid, price), qty in merged.items()]


def _sale_time(raw, flags):
    """The client clock is only believed inside a sane window."""
    now = timezone.now()
    when = parse_datetime(str(raw)) if raw else None
    if when is None:
        flags.append({"type": "CLOCK_UNKNOWN"})
        return now
    if timezone.is_naive(when):
        when = when.replace(tzinfo=dt_timezone.utc)
    if when > now + timedelta(minutes=5) or when < now - timedelta(days=max_sale_age_days()):
        flags.append({"type": "CLOCK_SKEW", "client_time": when.isoformat()})
        return now
    return when


def _resolve_session(user, raw_id, flags):
    """The register the cash went into; falls back to today's open register."""
    session = None
    if raw_id:
        try:
            session = CashRegisterSession.objects.select_related("branch").filter(pk=int(raw_id)).first()
        except (TypeError, ValueError):
            session = None
    if session is None:
        session = CashRegisterSession.get_active_session(resolve_user_branch(user))
        if session is not None and raw_id:
            flags.append({"type": "REGISTER_REASSIGNED"})
    if session is None:
        raise Rejected("no_register",
                       "There is no open cash register to record this sale against. "
                       "Open the register, then retry.")
    return session


def _resolve_cashier(request_user, tenant, raw_id):
    """
    A sale is attributed to the cashier who rang it up. A different logged-in person may
    deliver it only if they are an admin/manager (e.g. the shop's owner syncing a till).
    """
    try:
        cashier_id = int(raw_id) if raw_id is not None else request_user.pk
    except (TypeError, ValueError):
        raise Rejected("bad_cashier", "Unknown cashier.")
    if cashier_id == request_user.pk:
        return request_user
    if request_user.role not in MANAGEMENT_ROLES and not request_user.is_superuser:
        raise Rejected("wrong_cashier", "This sale belongs to another cashier. They must sign in to sync it.")
    cashier = get_user_model().objects.filter(pk=cashier_id, tenant=tenant, is_active=True).first()
    if cashier is None:
        raise Rejected("bad_cashier", "Unknown cashier.")
    return cashier


def process_offline_sale(request_user, tenant, raw):
    """
    Replay one offline sale. Returns a result dict with status one of:
      created    recorded now
      duplicate  already recorded earlier (safe retry) -- the client may forget it
      rejected   can never be recorded as sent; the client keeps it and shows why
      error      unexpected server problem; the client should simply retry later
    """
    try:
        sale_uuid = uuidlib.UUID(str(raw.get("uuid")))
    except (ValueError, AttributeError, TypeError):
        return {"uuid": str(raw.get("uuid") if isinstance(raw, dict) else ""),
                "status": "rejected", "code": "bad_uuid", "reason": "Missing or invalid sale id."}

    existing = Order.objects.filter(client_uuid=sale_uuid).select_related("receipt").first()
    if existing:
        return _result(existing, "duplicate")

    try:
        flags = []
        lines = _parse_lines(raw.get("lines"))
        cashier = _resolve_cashier(request_user, tenant, raw.get("cashier_id"))
        session = _resolve_session(request_user, raw.get("session_id"), flags)
        occurred_at = _sale_time(raw.get("occurred_at"), flags)

        found = {v.pk: v for v in ProductVariant.objects.select_related("base_variant")
                 .filter(pk__in={vid for vid, _, _ in lines})}
        missing = {vid for vid, _, _ in lines} - set(found)
        if missing:
            raise Rejected("unknown_product", "A product on this sale no longer exists.")

        cart = [CartItem(vid, found[vid].sku, found[vid].sku, price, qty) for vid, price, qty in lines]
        total = sum(price * qty for _, price, qty in lines)
        received = _dec(raw.get("amount_received"), "amount received")
        if received < total:
            raise Rejected("short_payment", "The amount received was less than the total.")

        reopened = session.status != "OPEN"
        try:
            with transaction.atomic():
                order = complete_pos_sale(
                    cart=cart, payment_method="CASH", cashier=cashier, cash_session=session,
                    amount_received=received, client_uuid=sale_uuid,
                    invoice_number=str(raw.get("invoice") or "")[:50] or None,
                    occurred_at=occurred_at, offline=True)
                extra = list(flags)
                if reopened:
                    # The register was closed before this sale reached us. The cash was in the
                    # drawer when it was counted, so the sale belongs to that session; re-save
                    # so expected closing balance and discrepancy include it.
                    session.save()
                    extra.append({"type": "LATE_SYNC_CLOSED_REGISTER", "session": session.pk})
                if extra:
                    order.offline_flags = list(order.offline_flags) + extra
                    order.save(update_fields=["offline_flags"])
        except IntegrityError:
            # Lost a race with another request carrying the same client_uuid.
            existing = Order.objects.filter(client_uuid=sale_uuid).select_related("receipt").first()
            if existing:
                return _result(existing, "duplicate")
            raise

        order = Order.objects.select_related("receipt").get(pk=order.pk)
        return _result(order, "created")

    except Rejected as exc:
        return {"uuid": str(sale_uuid), "status": "rejected", "code": exc.code, "reason": str(exc)}
    except ValueError as exc:  # complete_pos_sale's own validation
        return {"uuid": str(sale_uuid), "status": "rejected", "code": "invalid", "reason": str(exc)}
    except Exception:
        log.exception("offline sale %s failed to sync", sale_uuid)
        return {"uuid": str(sale_uuid), "status": "error", "reason": "Server error; will retry."}
