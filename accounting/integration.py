"""
Turns POS events into ledger entries. Every function is idempotent (keyed by the source record),
so it is safe to call from a hook, from sync_ledger(), or both.

Ledger starts on AccountingSettings.opening_balance_date (decision: no back-filling of history), so
anything dated earlier is ignored here.
"""
import datetime
import logging
from decimal import Decimal

from django.db.models import F, Sum
from django.utils import timezone

from inventory.models import (InventoryTransfer, InventoryTransferItem, PurchaseOrder, PurchaseOrderItem,
                              PurchasePayment, StockAdjustment, StockLevel)
from sales.models import CashTransaction, Order, OrderItem, Refund

from .models import METHOD_ACCOUNT_KEY, JournalEntry, JournalLine
from .services import ZERO, AccountingError, get_account, get_settings, money, post_journal

log = logging.getLogger("accounting")


def _cutover(tenant):
    return get_settings(tenant).opening_balance_date


def _day(dt):
    return timezone.localdate(dt) if isinstance(dt, datetime.datetime) and timezone.is_aware(dt) else \
        (dt.date() if isinstance(dt, datetime.datetime) else dt)


def _active_on(tenant, day):
    cut = _cutover(tenant)
    return bool(cut and day >= cut)


def _pay_key(method):
    return METHOD_ACCOUNT_KEY.get(method or "CASH", "CASH")


# --------------------------------------------------------------------------
# sales and refunds
# --------------------------------------------------------------------------
# Restaurant tills take card and online payments as well as cash and M-Pesa. Those settle to the bank,
# not the drawer. (Plain POS sales keep the long-standing mapping in models.METHOD_ACCOUNT_KEY.)
_RESTAURANT_PAY_KEY = {"CASH": "CASH", "MPESA": "MPESA", "CARD": "BANK", "ONLINE": "BANK"}


def _restaurant_receipt_split(order, total):
    """How a restaurant bill was actually paid, as {account_key: amount}; None means "use the plain rule".

    A bill can be split across cash, M-Pesa and card, so one debit to one account would misstate every
    account involved. Only used when the payments add up to exactly what the entry has to collect.
    """
    if not getattr(order.tenant, "is_hospitality", False):
        return None
    try:
        from restaurant.models import BillPayment
    except ImportError:                                  # restaurant module not installed
        return None
    paid = {}
    for p in BillPayment.all_objects.filter(order__sales_order=order, is_void=False):
        key = _RESTAURANT_PAY_KEY.get(p.method, "CASH")
        paid[key] = paid.get(key, ZERO) + p.amount
    paid = {k: money(v) for k, v in paid.items()}
    if paid and sum(paid.values(), ZERO) == total:
        return paid
    if paid:
        log.warning("Sale %s: restaurant payments %s do not add up to %s; posting to one account.",
                    order.pk, sum(paid.values(), ZERO), total)
    return None


def post_sale(order):
    tenant, day = order.tenant, _day(order.created_at)
    if not _active_on(tenant, day):
        return None
    items = list(OrderItem.all_objects.filter(order=order))
    gross = sum((i.retail_price * i.quantity for i in items), ZERO)
    discount = sum((i.discount_per_unit * i.quantity for i in items), ZERO)
    cogs = sum((i.cost_price * i.quantity for i in items), ZERO)
    gross, discount, cogs = money(gross), money(discount), money(cogs)
    net = gross - discount
    # Restaurant extras sit on top of the goods (they are not in total_revenue): the customer pays them,
    # so they must be collected in the entry or cash in the books falls short of the drawer.
    service = money(order.service_charge or ZERO)
    delivery = money(order.delivery_fee or ZERO)
    received = net + service + delivery
    if received <= 0 and cogs <= 0:
        return None
    br = order.branch
    split = _restaurant_receipt_split(order, received)
    if split:
        collected = [dict(account=key, debit=amt, branch=br, memo=order.invoice_number) for key, amt in split.items()]
    else:
        collected = [dict(account=_pay_key(order.payment_method), debit=received, branch=br, memo=order.invoice_number)]
    lines = collected + [
             dict(account="SALES_DISCOUNTS", debit=discount, branch=br),
             dict(account="SALES_REVENUE", credit=gross, branch=br, memo=order.invoice_number),
             dict(account="INCOME_SERVICE", credit=service, branch=br, memo=f"Service charge {order.invoice_number}"),
             dict(account="INCOME_OTHER", credit=delivery, branch=br, memo=f"Delivery fee {order.invoice_number}"),
             dict(account="COGS", debit=cogs, branch=br),
             dict(account="INVENTORY", credit=cogs, branch=br)]
    entry, _ = post_journal(tenant=tenant, entry_date=day, description=f"Sale {order.invoice_number}", lines=lines,
                            source_type="SALE", source_id=order.pk, source_key=f"sale:{order.pk}", branch=br,
                            user=None, audit_it=False)
    return entry


def post_refund(refund):
    tenant, day = refund.tenant, _day(refund.created_at)
    if not _active_on(tenant, day):
        return None
    amount, cogs = money(refund.total_amount), money(refund.cogs_reversed)
    br = refund.branch
    lines = [dict(account="SALES_RETURNS", debit=amount, branch=br, memo=refund.refund_number),
             dict(account="CASH", credit=amount, branch=br, memo=refund.refund_number),
             dict(account="INVENTORY", debit=cogs, branch=br),
             dict(account="COGS", credit=cogs, branch=br)]
    entry, _ = post_journal(tenant=tenant, entry_date=day, description=f"Refund {refund.refund_number}", lines=lines,
                            source_type="REFUND", source_id=refund.pk, source_key=f"refund:{refund.pk}", branch=br,
                            user=None, audit_it=False)
    return entry


# --------------------------------------------------------------------------
# purchases
# --------------------------------------------------------------------------
def post_po_receipt(item, delta_qty, when=None):
    """Stock received against a purchase: Dr Inventory, Cr Accounts Payable (at the PO price)."""
    po = item.purchase_order
    tenant = po.tenant
    day = _day(when or timezone.now())
    if not _active_on(tenant, day):
        return None
    value = money(Decimal(delta_qty) * item.unit_cost)
    if value <= 0:
        return None
    entry, _ = post_journal(
        tenant=tenant, entry_date=day, description=f"Stock received {po.po_number}",
        lines=[dict(account="INVENTORY", debit=value, branch=po.branch, memo=item.variant.sku),
               dict(account="AP", credit=value, branch=po.branch, memo=po.po_number)],
        source_type="PO_RECEIPT", source_id=item.pk, source_key=f"po-receipt:{item.pk}:{item.quantity_received}",
        branch=po.branch, user=None, audit_it=False)
    return entry


def post_po_payment(payment):
    po = payment.purchase_order
    tenant, day = po.tenant, _day(payment.created_at)
    if not _active_on(tenant, day):
        return None
    amount = money(payment.amount)
    entry, _ = post_journal(
        tenant=tenant, entry_date=day, description=f"Supplier payment {po.po_number}",
        lines=[dict(account="AP", debit=amount, branch=po.branch, memo=po.po_number),
               dict(account=_pay_key(payment.payment_method), credit=amount, branch=po.branch,
                    memo=payment.reference)],
        source_type="PO_PAYMENT", source_id=payment.pk, source_key=f"po-payment:{payment.pk}", branch=po.branch,
        user=payment.paid_by, audit_it=False)
    return entry


# --------------------------------------------------------------------------
# stock adjustments and transfers
# --------------------------------------------------------------------------
def post_stock_adjustment(adj):
    tenant, day = adj.tenant, _day(adj.created_at)
    if not _active_on(tenant, day):
        return None
    value = money(adj.quantity_changed * adj.variant.cost_price)
    if value == 0:
        return None
    gain, amount = value > 0, abs(value)
    other = "OPENING_EQUITY" if adj.adjustment_type == "OPENING" else "STOCK_LOSS"
    lines = ([dict(account="INVENTORY", debit=amount, branch=adj.branch), dict(account=other, credit=amount, branch=adj.branch)]
             if gain else
             [dict(account="STOCK_LOSS", debit=amount, branch=adj.branch), dict(account="INVENTORY", credit=amount, branch=adj.branch)])
    entry, _ = post_journal(tenant=tenant, entry_date=day, description=f"Stock {adj.get_adjustment_type_display()}: {adj.variant.sku}",
                            lines=lines, source_type="STOCK_ADJUSTMENT", source_id=adj.pk, source_key=f"stockadj:{adj.pk}",
                            branch=adj.branch, user=adj.user, audit_it=False)
    return entry


def _transfer_value(transfer, received=False):
    total = ZERO
    for it in InventoryTransferItem.all_objects.filter(transfer=transfer).select_related("variant"):
        qty = it.quantity_received if received else it.quantity
        total += Decimal(qty) * it.variant.cost_price
    return money(total)


def post_transfer_dispatch(transfer):
    tenant = transfer.tenant
    day = _day(transfer.dispatched_at or transfer.created_at)
    if not _active_on(tenant, day):
        return None
    value = _transfer_value(transfer)
    if value <= 0:
        return None
    # Goods on the road sit under 'no branch' so each branch's stock value stays true.
    entry, _ = post_journal(
        tenant=tenant, entry_date=day, description=f"Transfer out {transfer.transfer_number}",
        lines=[dict(account="INVENTORY", debit=value, branch=None, memo="In transit"),
               dict(account="INVENTORY", credit=value, branch=transfer.from_branch)],
        source_type="TRANSFER", source_id=transfer.pk, source_key=f"transfer-dispatch:{transfer.pk}",
        branch=transfer.from_branch, user=transfer.dispatched_by, audit_it=False)
    return entry


def _dispatch_value(transfer):
    e = JournalEntry.all_objects.filter(tenant=transfer.tenant, source_key=f"transfer-dispatch:{transfer.pk}").first()
    return e.total_debit if e else None


def post_transfer_receive(transfer):
    tenant = transfer.tenant
    day = _day(transfer.received_at or timezone.now())
    sent_value = _dispatch_value(transfer)
    if sent_value is None or not _active_on(tenant, day):
        return None
    full = _transfer_value(transfer)
    got = _transfer_value(transfer, received=True)
    arrived = money(sent_value * (got / full)) if full > 0 else ZERO
    short = sent_value - arrived
    lines = [dict(account="INVENTORY", credit=sent_value, branch=None, memo="In transit"),
             dict(account="INVENTORY", debit=arrived, branch=transfer.to_branch),
             dict(account="STOCK_LOSS", debit=short, branch=transfer.from_branch, memo="Short on arrival")]
    entry, _ = post_journal(tenant=tenant, entry_date=day, description=f"Transfer in {transfer.transfer_number}",
                            lines=lines, source_type="TRANSFER", source_id=transfer.pk,
                            source_key=f"transfer-receive:{transfer.pk}", branch=transfer.to_branch,
                            user=transfer.received_by, audit_it=False)
    return entry


def post_transfer_cancel(transfer):
    tenant = transfer.tenant
    sent_value = _dispatch_value(transfer)
    day = _day(getattr(transfer, "cancelled_at", None) or timezone.now())
    if sent_value is None or not _active_on(tenant, day):
        return None
    entry, _ = post_journal(
        tenant=tenant, entry_date=day, description=f"Transfer cancelled {transfer.transfer_number}",
        lines=[dict(account="INVENTORY", credit=sent_value, branch=None, memo="In transit"),
               dict(account="INVENTORY", debit=sent_value, branch=transfer.from_branch)],
        source_type="TRANSFER", source_id=transfer.pk, source_key=f"transfer-cancel:{transfer.pk}",
        branch=transfer.from_branch, user=None, audit_it=False)
    return entry


# --------------------------------------------------------------------------
# catch-up: post anything the hooks missed (offline sync, outage, hook error)
# --------------------------------------------------------------------------
def sync_ledger(tenant):
    """Idempotent. Returns a dict of how many entries were created per kind."""
    cut = _cutover(tenant)
    if not cut:
        return {}
    start = timezone.make_aware(datetime.datetime.combine(cut, datetime.time.min))
    have = set(JournalEntry.all_objects.filter(tenant=tenant).exclude(source_key__isnull=True)
               .values_list("source_key", flat=True))
    made = {}

    def run(kind, fn, objs, key):
        n = 0
        for o in objs:
            if key(o) in have:
                continue
            try:
                if fn(o):
                    n += 1
            except AccountingError as exc:
                log.warning("sync %s %s skipped: %s", kind, o.pk, exc)
        made[kind] = n

    run("sales", post_sale, Order.all_objects.filter(tenant=tenant, created_at__gte=start).select_related("branch"),
        lambda o: f"sale:{o.pk}")
    run("refunds", post_refund, Refund.all_objects.filter(tenant=tenant, created_at__gte=start).select_related("branch"),
        lambda o: f"refund:{o.pk}")
    run("supplier_payments", post_po_payment,
        PurchasePayment.all_objects.filter(tenant=tenant, created_at__gte=start).select_related("purchase_order__branch", "paid_by"),
        lambda o: f"po-payment:{o.pk}")
    run("stock_adjustments", post_stock_adjustment,
        StockAdjustment.all_objects.filter(tenant=tenant, created_at__gte=start).select_related("branch", "variant", "user"),
        lambda o: f"stockadj:{o.pk}")

    transfers = InventoryTransfer.all_objects.filter(tenant=tenant).exclude(status="PENDING") \
        .select_related("from_branch", "to_branch", "dispatched_by", "received_by")
    n = 0
    for t in transfers:
        if t.dispatched_at and t.dispatched_at >= start and f"transfer-dispatch:{t.pk}" not in have:
            n += bool(post_transfer_dispatch(t))
        if t.status == "COMPLETED" and f"transfer-receive:{t.pk}" not in have:
            n += bool(post_transfer_receive(t))
        if t.status == "CANCELLED" and f"transfer-dispatch:{t.pk}" in have and f"transfer-cancel:{t.pk}" not in have:
            n += bool(post_transfer_cancel(t))
    made["transfers"] = n

    # Receipts: only for purchases raised after the books started (older ones are in the opening balances).
    n = 0
    for item in PurchaseOrderItem.all_objects.filter(tenant=tenant, purchase_order__created_at__gte=start,
                                                     quantity_received__gt=0).select_related("purchase_order__branch", "variant"):
        posted = JournalLine.all_objects.filter(
            entry__tenant=tenant, entry__source_type="PO_RECEIPT", entry__source_id=str(item.pk),
            account__system_key="INVENTORY").aggregate(t=Sum("debit"))["t"] or ZERO
        missing = money(item.quantity_received * item.unit_cost) - posted
        if missing > 0 and item.unit_cost > 0:
            n += bool(post_po_receipt(item, missing / item.unit_cost, when=item.purchase_order.updated_at))
    made["purchase_receipts"] = n
    return made


# --------------------------------------------------------------------------
# moving-average costing (decision: moving average)
# --------------------------------------------------------------------------
def apply_receipt_cost(po, item, receive_qty):
    """
    Call BEFORE the received stock is added. New unit cost = weighted average of what is on hand
    (all branches) and what just arrived. Historical COGS is untouched because every sale line stores
    the cost it was sold at. Only runs for businesses that have started their books.
    """
    tenant = po.tenant
    if not _cutover(tenant):
        return None
    variant = item.variant
    base, per_pack = variant.stock_variant, Decimal(variant.units_per_pack)
    qty_in = Decimal(variant.to_base_units(receive_qty))
    if qty_in <= 0 or item.unit_cost <= 0:
        return None
    cost_in = item.unit_cost / per_pack
    on_hand = StockLevel.all_objects.filter(tenant=tenant, variant=base).aggregate(t=Sum("quantity"))["t"] or Decimal("0")
    old = base.cost_price or Decimal("0")
    new = cost_in if on_hand <= 0 else (on_hand * old + qty_in * cost_in) / (on_hand + qty_in)
    new = new.quantize(Decimal("0.01"))
    if new != base.cost_price:
        base.cost_price = new
        base.save(update_fields=["cost_price"])
    if variant.pk != base.pk:
        variant.cost_price = (new * per_pack).quantize(Decimal("0.01"))
        variant.save(update_fields=["cost_price"])
    return new


# --------------------------------------------------------------------------
# stock valuation vs ledger
# --------------------------------------------------------------------------
def stock_value(tenant, branch=None):
    qs = StockLevel.all_objects.filter(tenant=tenant)
    if branch is not None:
        qs = qs.filter(branch=branch)
    total = qs.aggregate(t=Sum(F("quantity") * F("variant__cost_price")))["t"] or ZERO
    return money(total)


def inventory_reconciliation(tenant):
    """Per branch: what the shelves are worth (stock x cost) against the Inventory account."""
    from inventory.models import Branch
    rows, inv = [], get_account(tenant, "INVENTORY")
    for b in Branch.all_objects.filter(tenant=tenant).order_by("name"):
        agg = JournalLine.all_objects.filter(tenant=tenant, account=inv, branch=b, entry__status__in=["POSTED", "REVERSED"]) \
            .aggregate(d=Sum("debit"), c=Sum("credit"))
        ledger = (agg["d"] or ZERO) - (agg["c"] or ZERO)
        physical = stock_value(tenant, b)
        rows.append(dict(branch=b, ledger=ledger, physical=physical, difference=physical - ledger))
    return rows


def post_inventory_revaluation(tenant, user, as_of=None):
    """Bring the Inventory account to the stock valuation, per branch. The difference goes to Stock Losses and Adjustments."""
    from .operations import require_active
    require_active(tenant)
    as_of = as_of or datetime.date.today()
    posted = 0
    for row in inventory_reconciliation(tenant):
        diff = money(row["difference"])
        if diff == 0:
            continue
        lines = ([dict(account="INVENTORY", debit=diff, branch=row["branch"]), dict(account="STOCK_LOSS", credit=diff, branch=row["branch"])]
                 if diff > 0 else
                 [dict(account="STOCK_LOSS", debit=-diff, branch=row["branch"]), dict(account="INVENTORY", credit=-diff, branch=row["branch"])])
        post_journal(tenant=tenant, entry_date=as_of, description="Inventory revaluation to stock count value", lines=lines,
                     source_type="INVENTORY_REVALUATION", source_id=row["branch"].pk,
                     source_key=f"revalue:{row['branch'].pk}:{as_of.isoformat()}:{diff}", branch=row["branch"], user=user)
        posted += 1
    return posted


def unclassified_cash_movements(tenant):
    """Till Cash In/Out entries (not supplier payments) that nobody has classified yet."""
    since = _cutover(tenant)
    if not since:
        return CashTransaction.all_objects.none()
    done = set(JournalEntry.all_objects.filter(tenant=tenant, source_type="CASH_MOVEMENT").values_list("source_id", flat=True))
    start = timezone.make_aware(datetime.datetime.combine(since, datetime.time.min))
    qs = CashTransaction.all_objects.filter(tenant=tenant, created_at__gte=start, purchase_payment__isnull=True) \
        .select_related("session__branch", "user").order_by("-created_at")
    return [t for t in qs if str(t.pk) not in done]


def housekeeping(tenant):
    """
    Cheap, idempotent upkeep run when finance pages open (at most once a minute per business):
    create due recurring expenses, post finished months of depreciation, catch up missed postings.
    """
    from django.core.cache import cache
    from .operations import generate_depreciation, generate_recurring_expenses
    if not _cutover(tenant) or cache.get(f"acct-hk:{tenant.pk}"):
        return
    cache.set(f"acct-hk:{tenant.pk}", 1, 60)
    try:
        generate_recurring_expenses(tenant)
        generate_depreciation(tenant, up_to=datetime.date.today().replace(day=1) - datetime.timedelta(days=1))
        sync_ledger(tenant)
    except Exception:
        log.exception("Accounting housekeeping failed for tenant %s", tenant.pk)
