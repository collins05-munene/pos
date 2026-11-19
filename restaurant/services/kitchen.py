"""
Kitchen Display System: sending orders to stations, moving tickets through
Pending -> Preparing -> Ready -> Served, preparation-time tracking, and the waiter alert.
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from notifications.models import Notification
from notifications.utils import notify

from ..models import KitchenTicket, OrderLine, RestaurantOrder, get_restaurant_settings
from . import stock
from .errors import RestaurantError
from .setup import default_station

TS = KitchenTicket.Status
LS = OrderLine.Status
FLOOR_ROLES = ("ADMIN", "MANAGER", "CASHIER")


def _who(user):
    return (user.get_full_name() or user.username.split("__")[-1]) if user else ""


# ======================================================================================
# sending
# ======================================================================================
@transaction.atomic
def send_to_kitchen(order, user):
    """Fire every not-yet-sent item. One ticket per station, so grill and bar each get their own slip."""
    order = RestaurantOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != RestaurantOrder.Status.OPEN:
        raise RestaurantError("This order is closed.")
    if order.needs_acceptance:
        raise RestaurantError("Accept this online order first.")
    draft = list(order.lines.filter(status=LS.DRAFT).select_related("menu_item__station"))
    if not draft:
        raise RestaurantError("There is nothing new to send.")

    by_station = {}
    fallback = None
    for line in draft:
        st = line.menu_item.station
        if st is None or not st.is_active:
            fallback = fallback or default_station(order.tenant)
            st = fallback
        by_station.setdefault(st, []).append(line)

    round_no = (order.tickets.order_by("-round").values_list("round", flat=True).first() or 0) + 1
    now = timezone.now()
    tickets = []
    for st, lines in by_station.items():
        ticket = KitchenTicket.objects.create(
            order=order, station=st, branch=order.branch, round=round_no, fired_at=now, fired_by=user)
        for line in lines:
            line.status, line.sent_at, line.station, line.ticket = LS.SENT, now, st, ticket
            line.save(update_fields=["status", "sent_at", "station", "ticket"])
        tickets.append(ticket)
    stock.apply_policy(draft, "SEND")
    return tickets


@transaction.atomic
def accept_online_order(order, user):
    order = RestaurantOrder.objects.select_for_update().get(pk=order.pk)
    if not order.needs_acceptance:
        raise RestaurantError("This order was already accepted.")
    order.needs_acceptance = False
    order.save(update_fields=["needs_acceptance"])
    return send_to_kitchen(order, user)


# ======================================================================================
# ticket movement
# ======================================================================================
def _lock_ticket(ticket):
    return KitchenTicket.objects.select_for_update().select_related("order", "station").get(pk=ticket.pk)


def _live_lines(ticket):
    return list(ticket.lines.exclude(status=LS.VOID))


@transaction.atomic
def start_ticket(ticket, user):
    ticket = _lock_ticket(ticket)
    if ticket.status != TS.PENDING:
        raise RestaurantError("This ticket was already started.")
    ticket.status, ticket.started_at, ticket.worked_by = TS.PREPARING, timezone.now(), user
    ticket.save(update_fields=["status", "started_at", "worked_by"])
    ticket.lines.filter(status=LS.SENT).update(status=LS.PREPARING)
    return ticket


@transaction.atomic
def ready_ticket(ticket, user):
    ticket = _lock_ticket(ticket)
    if ticket.status not in (TS.PENDING, TS.PREPARING):
        raise RestaurantError("This ticket is not being prepared.")
    now = timezone.now()
    ticket.status, ticket.ready_at = TS.READY, now
    if ticket.started_at is None:
        ticket.started_at = now
    ticket.worked_by = ticket.worked_by or user
    ticket.save(update_fields=["status", "ready_at", "started_at", "worked_by"])
    lines = _live_lines(ticket)
    for line in lines:
        line.status, line.ready_at = LS.READY, now
        line.save(update_fields=["status", "ready_at"])
    stock.apply_policy(lines, "READY")
    _alert_ready(ticket)
    return ticket


@transaction.atomic
def serve_ticket(ticket, user):
    ticket = _lock_ticket(ticket)
    if ticket.status != TS.READY:
        raise RestaurantError("Only food that is ready can be marked served.")
    now = timezone.now()
    ticket.status, ticket.served_at = TS.SERVED, now
    ticket.save(update_fields=["status", "served_at"])
    lines = _live_lines(ticket)
    for line in lines:
        line.status, line.served_at = LS.SERVED, now
        line.save(update_fields=["status", "served_at"])
    stock.apply_policy(lines, "SERVED")
    return ticket


@transaction.atomic
def recall_ticket(ticket, user):
    """Undo a premature 'ready' (the plate went back to the pass)."""
    ticket = _lock_ticket(ticket)
    if ticket.status != TS.READY:
        raise RestaurantError("Only a ready ticket can be recalled.")
    ticket.status, ticket.ready_at = TS.PREPARING, None
    ticket.save(update_fields=["status", "ready_at"])
    ticket.lines.filter(status=LS.READY).update(status=LS.PREPARING, ready_at=None)
    return ticket


def refresh_ticket(ticket):
    """After a line was cancelled: a ticket with nothing left to make is cancelled."""
    ticket = KitchenTicket.objects.get(pk=ticket.pk)
    if ticket.status in (TS.CANCELLED, TS.SERVED):
        return ticket
    if not ticket.lines.exclude(status=LS.VOID).exists():
        ticket.status, ticket.cancelled_at = TS.CANCELLED, timezone.now()
        ticket.save(update_fields=["status", "cancelled_at"])
    return ticket


# ======================================================================================
# waiter alert + takeaway/delivery readiness
# ======================================================================================
def _alert_ready(ticket):
    order = ticket.order
    items = ", ".join(f"{l.quantity} x {l.name}" for l in ticket.lines.exclude(status=LS.VOID))[:180]
    where = order.table.label if order.table_id else order.get_order_type_display()
    title = f"Food ready: {where}"
    msg = f"{ticket.station.name} has finished {order.number} ({where}): {items}"
    if order.order_type == RestaurantOrder.Type.DINE_IN:
        if order.waiter_id:
            notify(order.tenant, title, msg, level=Notification.Level.SUCCESS, user=order.waiter)
        else:
            notify(order.tenant, title, msg, level=Notification.Level.SUCCESS, roles=("ADMIN", "MANAGER", "CASHIER", "WAITER"))
        return
    # takeaway / delivery: the order is ready once every live ticket is ready
    live = order.tickets.exclude(status__in=(TS.CANCELLED, TS.SERVED))
    if not live.exclude(status=TS.READY).exists() and order.handover == RestaurantOrder.Handover.PENDING:
        order.handover, order.handover_at = RestaurantOrder.Handover.READY, timezone.now()
        order.save(update_fields=["handover", "handover_at"])
        notify(order.tenant, f"{order.get_order_type_display()} ready: {order.number}",
               f"{order.customer_name or order.customer_phone}: all items are ready.",
               level=Notification.Level.SUCCESS, roles=FLOOR_ROLES)


# ======================================================================================
# boards (JSON for the KDS and the waiter screen)
# ======================================================================================
def _ticket_payload(t, now, late_seconds):
    order = t.order
    elapsed = int(((t.ready_at or now) - t.fired_at).total_seconds())
    where = order.table.label if order.table_id else order.get_order_type_display()
    return {
        "id": t.pk, "order_id": order.pk, "order": order.number, "where": where, "type": order.order_type,
        "type_label": order.get_order_type_display(), "round": t.round, "status": t.status,
        "station": t.station.name, "station_id": t.station_id, "color": t.station.color,
        "waiter": _who(order.waiter) if order.waiter_id else "",
        "customer": order.customer_name or order.customer_phone,
        "notes": order.notes, "fired_at": t.fired_at.isoformat(), "elapsed": elapsed,
        "late": t.status in (TS.PENDING, TS.PREPARING) and elapsed > late_seconds,
        "prep_seconds": t.prep_seconds,
        "lines": [{
            "id": l.pk, "qty": l.quantity, "name": l.name, "note": l.note, "void": l.status == LS.VOID,
            "mods": [(("NO " if m.kind == "REMOVE" else "+ ") + m.name) if m.kind != "CHOICE" else m.name
                     for m in l.modifiers.all()],
        } for l in t.lines.all()],
    }


def kds_board(branch, station_id=None):
    rs = get_restaurant_settings()
    now = timezone.now()
    recent = now - timedelta(minutes=5)
    qs = (KitchenTicket.objects.filter(branch=branch)
          .filter(Q(status__in=(TS.PENDING, TS.PREPARING, TS.READY)) |
                  Q(status=TS.CANCELLED, cancelled_at__gte=recent))
          .select_related("order__table", "order__waiter", "station")
          .prefetch_related("lines__modifiers", "order__table__merged_tables"))
    if station_id:
        qs = qs.filter(station_id=station_id)
    return [_ticket_payload(t, now, rs.kds_late_minutes * 60) for t in qs.order_by("fired_at", "id")]


def ready_for_waiter(branch, user=None, all_orders=False):
    """Tickets that are ready and not yet served: the waiter's 'collect from the pass' list."""
    rs = get_restaurant_settings()
    now = timezone.now()
    qs = (KitchenTicket.objects.filter(branch=branch, status=TS.READY,
                                       order__order_type=RestaurantOrder.Type.DINE_IN,
                                       order__status=RestaurantOrder.Status.OPEN)
          .select_related("order__table", "order__waiter", "station").prefetch_related("lines__modifiers"))
    if user is not None and not all_orders:
        qs = qs.filter(Q(order__waiter=user) | Q(order__waiter__isnull=True))
    return [_ticket_payload(t, now, rs.kds_late_minutes * 60) for t in qs.order_by("ready_at")]


def prep_time_report(branch, since):
    """Average / slowest preparation time per station since a date."""
    from django.db.models import Avg, Count, ExpressionWrapper, F, DurationField, Max
    qs = (KitchenTicket.objects.filter(branch=branch, ready_at__isnull=False, fired_at__gte=since)
          .annotate(prep=ExpressionWrapper(F("ready_at") - F("fired_at"), output_field=DurationField()))
          .values("station__name").annotate(avg=Avg("prep"), slowest=Max("prep"), tickets=Count("id"))
          .order_by("station__name"))
    return [{"station": r["station__name"], "tickets": r["tickets"],
             "avg_minutes": round(r["avg"].total_seconds() / 60, 1) if r["avg"] else 0,
             "slowest_minutes": round(r["slowest"].total_seconds() / 60, 1) if r["slowest"] else 0} for r in qs]
