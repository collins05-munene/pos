"""
Thin, failure-proof bridge from the POS code to the ledger.

notify() runs after the surrounding database transaction commits. If posting fails for any
reason the sale/refund/purchase still stands, the error is logged, and sync_ledger() picks it up
later. A ledger problem must never stop a shop from selling.
"""
import logging
from decimal import Decimal

from django.db import transaction

log = logging.getLogger("accounting")


def notify(kind, *args):
    transaction.on_commit(lambda: _run(kind, *args))


def _run(kind, *args):
    try:
        from inventory.models import (InventoryTransfer, PurchaseOrderItem, PurchasePayment, StockAdjustment)
        from sales.models import Order, Refund

        from . import integration as ig
        if kind == "sale":
            ig.post_sale(Order.all_objects.select_related("branch", "tenant").get(pk=args[0]))
        elif kind == "refund":
            ig.post_refund(Refund.all_objects.select_related("branch", "tenant").get(pk=args[0]))
        elif kind == "po_payment":
            ig.post_po_payment(PurchasePayment.all_objects.select_related("purchase_order__branch", "paid_by").get(pk=args[0]))
        elif kind == "po_receipt":
            item = PurchaseOrderItem.all_objects.select_related("purchase_order__branch", "variant").get(pk=args[0])
            ig.post_po_receipt(item, Decimal(args[1]))
        elif kind == "stock_adjustment":
            ig.post_stock_adjustment(StockAdjustment.all_objects.select_related("branch", "variant", "user").get(pk=args[0]))
        elif kind in ("transfer_dispatch", "transfer_receive", "transfer_cancel"):
            t = InventoryTransfer.all_objects.select_related("from_branch", "to_branch", "dispatched_by", "received_by").get(pk=args[0])
            {"transfer_dispatch": ig.post_transfer_dispatch, "transfer_receive": ig.post_transfer_receive,
             "transfer_cancel": ig.post_transfer_cancel}[kind](t)
    except Exception:       # never let the ledger break a sale
        log.exception("Ledger posting failed for %s %s; sync_ledger() will retry.", kind, args)
