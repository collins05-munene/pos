"""
Taking money for a restaurant bill, and the cashier's shift reconciliation.

Payments use the existing money machinery:
  * cash comes in through the branch's open CashRegisterSession (the shared drawer);
  * when the bill is fully paid it becomes an ordinary sales.Order (+ items + Receipt), so the
    sales dashboard, cash-session totals, receipts and refunds work exactly as they do for the POS.
"""
import uuid
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from sales.models import CashRegisterSession, CashTransaction, Order, OrderItem, Refund
from sales.receipts import create_receipt

from ..models import (BillPayment, CashierShift, OrderLine, RestaurantOrder, ZERO, get_restaurant_settings, money)
from . import orders as order_svc
from . import stock
from .errors import RestaurantError

M = BillPayment.Method


def enabled_methods(rs=None):
    rs = rs or get_restaurant_settings()
    out = [M.CASH]
    if rs.accept_mpesa:
        out.append(M.MPESA)
    if rs.accept_card:
        out.append(M.CARD)
    return out


# ======================================================================================
# shifts
# ======================================================================================
def current_shift(user):
    return CashierShift.objects.filter(cashier=user, status=CashierShift.Status.OPEN).first()


@transaction.atomic
def open_shift(user, branch, opening_float=ZERO):
    if current_shift(user):
        raise RestaurantError("You already have a shift open.")
    return CashierShift.objects.create(cashier=user, branch=branch, opening_float=money(opening_float or 0))


def ensure_shift(user, branch):
    return current_shift(user) or open_shift(user, branch)


def shift_totals(shift, until=None):
    until = until or shift.closed_at or timezone.now()
    pays = shift.payments.filter(is_void=False)

    def total(method):
        return pays.filter(method=method).aggregate(t=Sum("amount"))["t"] or ZERO

    cash_in = CashTransaction.objects.filter(
        user=shift.cashier, transaction_type="CASH_IN", created_at__gte=shift.opened_at, created_at__lte=until
    ).aggregate(t=Sum("amount"))["t"] or ZERO
    cash_out = CashTransaction.objects.filter(
        user=shift.cashier, transaction_type="CASH_OUT", created_at__gte=shift.opened_at, created_at__lte=until
    ).aggregate(t=Sum("amount"))["t"] or ZERO
    refunds = Refund.objects.filter(
        processed_by=shift.cashier, created_at__gte=shift.opened_at, created_at__lte=until
    ).aggregate(t=Sum("total_amount"))["t"] or ZERO
    cash_sales = total(M.CASH)
    return {
        "cash_sales": cash_sales, "mpesa": total(M.MPESA), "card": total(M.CARD),
        "cash_in": cash_in, "cash_out": cash_out, "refunds": refunds,
        "expected_cash": money(shift.opening_float + cash_sales + cash_in - cash_out - refunds),
        "bills": pays.values("order").distinct().count(),
        "payments": pays.count(),
    }


@transaction.atomic
def close_shift(shift, user, *, counted_cash, counted_mpesa=None, counted_card=None, notes=""):
    shift = CashierShift.objects.select_for_update().get(pk=shift.pk)
    if shift.status != CashierShift.Status.OPEN:
        raise RestaurantError("This shift is already closed.")
    now = timezone.now()
    t = shift_totals(shift, until=now)
    counted_cash = money(counted_cash or 0)
    counted_mpesa = money(counted_mpesa) if counted_mpesa not in (None, "") else t["mpesa"]
    counted_card = money(counted_card) if counted_card not in (None, "") else t["card"]
    if min(counted_cash, counted_mpesa, counted_card) < 0:
        raise RestaurantError("Counted amounts can't be negative.")
    shift.expected_cash, shift.expected_mpesa, shift.expected_card = t["expected_cash"], t["mpesa"], t["card"]
    shift.counted_cash, shift.counted_mpesa, shift.counted_card = counted_cash, counted_mpesa, counted_card
    shift.variance = (counted_cash - t["expected_cash"]) + (counted_mpesa - t["mpesa"]) + (counted_card - t["card"])
    shift.notes, shift.status, shift.closed_at = (notes or "")[:300], CashierShift.Status.CLOSED, now
    shift.save()
    if shift.variance != 0:
        from notifications.models import Notification
        from notifications.utils import notify
        who = shift.cashier.get_full_name() or shift.cashier.username.split("__")[-1]
        kind = "short" if shift.variance < 0 else "over"
        notify(shift.tenant, f"Shift closed {kind}: {who}",
               f"{who}'s shift is KSH {abs(shift.variance)} {kind}. Expected cash KSH {t['expected_cash']}, counted KSH {counted_cash}.",
               level=Notification.Level.WARNING, dedupe_hours=0)
    return shift


# ======================================================================================
# paying
# ======================================================================================
@transaction.atomic
def pay(order, *, user, method, amount=None, tendered=None, reference="", payer=""):
    """
    Record one payment against a bill.  Call it several times to split a bill between guests or
    methods, or to take a part payment.  The bill closes (and becomes a sales Order + Receipt) when
    the balance reaches zero.

    CASH:  pass `tendered` (what the guest handed over). The bill is credited min(tendered, balance)
           and the rest is change.
    other: pass `amount`, no more than the balance; `reference` is the M-Pesa code / slip number.
    """
    order = RestaurantOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != RestaurantOrder.Status.OPEN:
        raise RestaurantError("This bill is already closed.")
    if order.needs_acceptance:
        raise RestaurantError("Accept this online order first.")
    if not order.active_lines:
        raise RestaurantError("There is nothing on this bill.")
    if order.draft_lines:
        raise RestaurantError("Some items haven't been sent to the kitchen. Send or remove them before payment.")
    order_svc.recalculate(order)
    if order.total <= 0:
        raise RestaurantError("This bill is KSH 0. Use 'Close as complimentary'.")
    balance = order.balance
    if balance <= 0:
        raise RestaurantError("This bill is already fully paid.")
    rs = get_restaurant_settings(order.tenant)
    if method not in enabled_methods(rs) and method != M.ONLINE:
        raise RestaurantError(f"{BillPayment.Method(method).label if method in M.values else method} payments are not switched on in Restaurant Settings.")
    if method == M.ONLINE and user is not None:
        raise RestaurantError("Online payments can only be recorded by the online ordering link.")

    change = ZERO
    session = None
    if method == M.CASH:
        tendered = money(tendered if tendered not in (None, "") else (amount or 0))
        if tendered <= 0:
            raise RestaurantError("Enter the cash received.")
        applied = min(tendered, balance)
        change = tendered - applied
        session = CashRegisterSession.get_active_session(order.branch)
        if session is None:
            raise RestaurantError("The cash register is closed. Open the register before taking cash.")
    else:
        applied = money(amount or 0)
        tendered = None
        if applied <= 0:
            raise RestaurantError("Enter the amount paid.")
        if applied > balance:
            raise RestaurantError(f"That is more than the balance (KSH {balance}).")
        if method in (M.MPESA, M.CARD) and not reference.strip():
            raise RestaurantError("Enter the M-Pesa code or card slip number.")

    shift = ensure_shift(user, order.branch) if (user is not None and method in (M.CASH, M.MPESA, M.CARD)) else None
    payment = BillPayment.objects.create(
        order=order, method=method, amount=applied, tendered=tendered, change_given=change,
        reference=reference.strip()[:60], payer=payer.strip()[:60], received_by=user, shift=shift)

    order = RestaurantOrder.objects.get(pk=order.pk)
    if order.balance <= 0:
        settle(order, user, session or CashRegisterSession.get_active_session(order.branch))
    return payment


@transaction.atomic
def void_payment(payment, user):
    """Undo a payment on a bill that is still open (cash goes back to the guest, tell the cashier)."""
    payment = BillPayment.objects.select_for_update().select_related("order").get(pk=payment.pk)
    if payment.order.status != RestaurantOrder.Status.OPEN:
        raise RestaurantError("The bill is closed. Use a refund instead.")
    if payment.is_void:
        return payment
    payment.is_void = True
    payment.save(update_fields=["is_void"])
    return payment


@transaction.atomic
def close_complimentary(order, user):
    """A bill whose total is zero (everything discounted / comped) is closed without payment."""
    order = RestaurantOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != RestaurantOrder.Status.OPEN:
        raise RestaurantError("This bill is already closed.")
    if order.draft_lines:
        raise RestaurantError("Send or remove the unsent items first.")
    order_svc.recalculate(order)
    if order.total > 0:
        raise RestaurantError("This bill has an amount to pay.")
    if not order.active_lines:
        raise RestaurantError("There is nothing on this bill.")
    return settle(order, user, CashRegisterSession.get_active_session(order.branch))


# ======================================================================================
# turning the bill into a sales Order
# ======================================================================================
def _allocate_discount(lines, discount):
    """Share a bill discount across lines in whole cents (largest remainder), never exceeding a line."""
    totals = [int((l.line_total * 100).to_integral_value()) for l in lines]
    grand = sum(totals)
    cents = int((discount * 100).to_integral_value())
    if cents == 0 or grand == 0:
        return [0] * len(lines)
    base = [cents * t // grand for t in totals]
    rem = [cents * t % grand for t in totals]
    left = cents - sum(base)
    for i in sorted(range(len(lines)), key=lambda k: rem[k], reverse=True)[:left]:
        base[i] += 1
    return base


def _method_summary(payments):
    methods = {p.method for p in payments}
    if len(methods) == 1:
        return next(iter(methods))
    return "SPLIT" if methods else "CASH"


@transaction.atomic
def settle(order, user, session):
    order = RestaurantOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != RestaurantOrder.Status.OPEN:
        raise RestaurantError("This bill is already closed.")
    order_svc.recalculate(order)
    lines = list(order.lines.exclude(status=OrderLine.Status.VOID)
                 .select_related("menu_item__variant__product").prefetch_related("modifiers"))
    payments = list(order.payments.filter(is_void=False))
    cashier = user or (payments[0].received_by if payments else order.created_by)
    discount_shares = _allocate_discount(lines, order.discount_total)
    discount_name = (order.discount_reason or "Bill discount") if order.discount_total > 0 else ""

    sale = Order.objects.create(
        invoice_number=f"INV-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
        branch=order.branch, cashier=cashier, cash_session=session,
        payment_method=_method_summary(payments), client_uuid=None)

    cogs = ZERO
    receipt_lines = []
    for line, share in zip(lines, discount_shares):
        v = line.menu_item.variant
        q = line.quantity
        d_low, r = divmod(share, q)
        parts = [(q - r, Decimal(d_low) / 100)]
        if r:
            parts.append((r, Decimal(d_low + 1) / 100))
        for qty, per_unit in parts:
            if qty <= 0:
                continue
            OrderItem(order=sale, variant=v, quantity=qty, retail_price=line.unit_price, cost_price=line.unit_cost,
                      discount=None, discount_name=discount_name if per_unit else "",
                      discount_per_unit=per_unit).save()
        cogs += line.unit_cost * q
        extras = [(("No " if m.kind == "REMOVE" else "+ ") + m.name) if m.kind != "CHOICE" else m.name
                  for m in line.modifiers.all()]
        receipt_lines.append({
            "name": line.name, "sku": v.sku, "quantity": q, "unit_price": str(money(line.unit_price)),
            "line_total": str(line.line_total), "extras": extras, "note": line.note})

    cash_kept = sum((p.amount for p in payments if p.method == M.CASH), ZERO)
    received = sum(((p.tendered if (p.method == M.CASH and p.tendered is not None) else p.amount) for p in payments), ZERO)
    sale.subtotal = order.subtotal
    sale.discount_total = order.discount_total
    sale.total_revenue = order.subtotal - order.discount_total
    sale.total_cogs = money(cogs)
    sale.total_profit = sale.total_revenue - sale.total_cogs
    sale.service_charge = order.service_charge
    sale.delivery_fee = order.delivery_fee
    sale.cash_amount = cash_kept
    sale.amount_received = received if payments else None
    sale.change_given = sum((p.change_given for p in payments), ZERO) if payments else None
    sale.save()

    paid_methods = []
    for p in payments:
        label = p.get_method_display()
        paid_methods.append({"method": label, "amount": str(p.amount), "reference": p.reference})
    create_receipt(sale, lines=receipt_lines, meta={
        "order_number": order.number, "table": order.table.label if order.table_id else "",
        "order_type": order.order_type, "order_type_label": order.get_order_type_display(),
        "waiter": (order.waiter.get_full_name() or order.waiter.username.split("__")[-1]) if order.waiter_id else "",
        "guests": order.guests, "payments": paid_methods})

    order.status, order.closed_at, order.sales_order = RestaurantOrder.Status.PAID, timezone.now(), sale
    order.save(update_fields=["status", "closed_at", "sales_order"])
    from accounting.hooks import notify          # ledger entry (after commit; never blocks the bill)
    notify("sale", sale.pk)
    stock.apply_policy(lines, "PAID")
    return sale
