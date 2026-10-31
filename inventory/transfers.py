"""
Branch-to-branch stock transfers. Every stock movement a transfer causes happens here,
through stock.change_stock, inside one database transaction.

Lifecycle (see InventoryTransfer):

    create ──► PENDING ──dispatch──► IN_TRANSIT ──receive──► COMPLETED
                  │                       │
                  └────────cancel─────────┴──► CANCELLED   (in-transit stock goes back to the sender)

Stock leaves the sending branch at DISPATCH and joins the receiving branch at RECEIVE.
While IN_TRANSIT it is counted at neither branch, so a stock count never double-counts it
and nobody can sell what is on the road. If less arrives than was sent, the difference is
simply recorded on the line (`shortfall`): it already left the sender and never reached
the receiver, so the stock books stay correct without a second adjustment.

Quantities are in each variant's own unit (3 boxes, 40 tablets ...), like purchases.
"""
import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from products.models import fmt_qty

from .models import InventoryTransfer, InventoryTransferItem
from .stock import InsufficientStock, available_units, change_stock


class TransferError(Exception):
    """A rule of the transfer workflow was broken; the message is safe to show to the user."""


def new_transfer_number():
    return f"TR-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


def _lock(transfer):
    # Re-read under a row lock so two people (or a double click) can't dispatch,
    # receive or cancel the same transfer twice.
    return (InventoryTransfer.objects.select_for_update()
            .select_related('from_branch', 'to_branch').get(pk=transfer.pk))


def _items_in_lock_order(transfer):
    """Items ordered by the stock row they touch, so concurrent transfers lock rows in the same order."""
    items = list(transfer.items.select_related('variant', 'variant__product', 'variant__base_variant'))
    items.sort(key=lambda i: i.variant.stock_variant.pk)
    return items


def _require_active(branch, role):
    if not branch.is_active:
        raise TransferError(f"The {role} branch '{branch.name}' is not active.")


@transaction.atomic
def create_transfer(user, from_branch, to_branch, lines, remarks='', dispatch_now=False):
    """
    lines: iterable of (variant, quantity). Returns the new transfer, PENDING, or IN_TRANSIT
    when dispatch_now is set (in which case stock is checked and deducted straight away).
    """
    if from_branch.pk == to_branch.pk:
        raise TransferError("Choose two different branches - stock can't be transferred to the same branch.")
    _require_active(from_branch, 'sending')
    _require_active(to_branch, 'receiving')

    lines = list(lines)
    if not lines:
        raise TransferError("Add at least one item to transfer.")
    seen = set()
    for variant, qty in lines:
        if Decimal(qty) <= 0:
            raise TransferError(f"Quantity for {variant.product.name} must be more than zero.")
        if variant.pk in seen:
            raise TransferError(f"{variant.product.name} ({variant.sku}) is listed more than once - combine the quantities.")
        seen.add(variant.pk)

    transfer = InventoryTransfer.objects.create(
        transfer_number=new_transfer_number(), from_branch=from_branch, to_branch=to_branch,
        remarks=remarks, created_by=user)
    for variant, qty in lines:
        # create() (not bulk_create) so the tenant-owned model's save() stamps the tenant.
        InventoryTransferItem.objects.create(transfer=transfer, variant=variant, quantity=Decimal(qty))

    if dispatch_now:
        dispatch_transfer(transfer, user)
        transfer.refresh_from_db()
    return transfer


@transaction.atomic
def dispatch_transfer(transfer, user):
    """PENDING -> IN_TRANSIT. Takes the stock out of the sending branch."""
    t = _lock(transfer)
    if t.status != 'PENDING':
        raise TransferError(f"{t.transfer_number} is already {t.get_status_display().lower()}.")
    _require_active(t.from_branch, 'sending')
    _require_active(t.to_branch, 'receiving')

    for item in _items_in_lock_order(t):
        try:
            change_stock(t.from_branch, item.variant, -item.quantity)
        except InsufficientStock:
            have = available_units(t.from_branch, item.variant)
            raise TransferError(
                f"Not enough {item.variant.product.name} ({item.variant.sku}) at {t.from_branch.name}: "
                f"sending {fmt_qty(item.quantity)} {item.variant.unit_label} but only "
                f"{fmt_qty(have)} available.")

    t.status = 'IN_TRANSIT'
    t.dispatched_by, t.dispatched_at = user, timezone.now()
    t.save(update_fields=['status', 'dispatched_by', 'dispatched_at', 'updated_at'])
    return t


@transaction.atomic
def receive_transfer(transfer, user, received=None):
    """
    IN_TRANSIT -> COMPLETED. Adds the stock to the receiving branch.

    received: optional {item_id: quantity actually arrived}. Anything not listed counts as
    arriving in full. A line can arrive short but never over.
    """
    t = _lock(transfer)
    if t.status != 'IN_TRANSIT':
        raise TransferError(f"{t.transfer_number} is {t.get_status_display().lower()}, so it can't be received.")
    _require_active(t.to_branch, 'receiving')

    received = received or {}
    for item in _items_in_lock_order(t):
        qty = Decimal(received.get(item.pk, item.quantity))
        if qty < 0:
            raise TransferError(f"Received quantity for {item.variant.product.name} can't be negative.")
        if qty > item.quantity:
            raise TransferError(
                f"Received quantity for {item.variant.product.name} ({fmt_qty(qty)}) is more than "
                f"was sent ({fmt_qty(item.quantity)}).")
        if qty > 0:
            change_stock(t.to_branch, item.variant, qty)
        item.quantity_received = qty
        item.save(update_fields=['quantity_received'])

    t.status = 'COMPLETED'
    t.received_by, t.received_at = user, timezone.now()
    t.save(update_fields=['status', 'received_by', 'received_at', 'updated_at'])
    return t


@transaction.atomic
def cancel_transfer(transfer, user):
    """PENDING or IN_TRANSIT -> CANCELLED. Stock already dispatched goes back to the sender."""
    t = _lock(transfer)
    if t.status not in ('PENDING', 'IN_TRANSIT'):
        raise TransferError(f"{t.transfer_number} is {t.get_status_display().lower()} and can't be cancelled.")

    if t.status == 'IN_TRANSIT':
        for item in _items_in_lock_order(t):
            change_stock(t.from_branch, item.variant, item.quantity)

    t.status = 'CANCELLED'
    t.cancelled_by, t.cancelled_at = user, timezone.now()
    t.save(update_fields=['status', 'cancelled_by', 'cancelled_at', 'updated_at'])
    return t
