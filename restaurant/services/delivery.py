"""Takeaway pick-ups and deliveries: riders, fees and the status trail."""
from django.db import transaction
from django.utils import timezone

from notifications.models import Notification
from notifications.utils import notify

from ..models import DeliveryRider, RestaurantOrder
from .errors import RestaurantError

H = RestaurantOrder.Handover
T = RestaurantOrder.Type

ALLOWED = {
    T.DELIVERY: {
        H.PENDING: {H.READY, H.ASSIGNED},
        H.READY: {H.ASSIGNED},
        H.ASSIGNED: {H.OUT, H.READY},
        H.OUT: {H.COMPLETED, H.FAILED},
        H.FAILED: {H.ASSIGNED, H.READY},
        H.COMPLETED: set(),
    },
    T.TAKEAWAY: {
        H.PENDING: {H.READY},
        H.READY: {H.COMPLETED},
        H.COMPLETED: set(),
    },
}


def _lock(order):
    return RestaurantOrder.objects.select_for_update().get(pk=order.pk)


@transaction.atomic
def assign_rider(order, rider):
    order = _lock(order)
    if order.order_type != T.DELIVERY:
        raise RestaurantError("Only delivery orders have a rider.")
    if order.status == RestaurantOrder.Status.VOID:
        raise RestaurantError("This order was cancelled.")
    if order.handover in (H.OUT, H.COMPLETED):
        raise RestaurantError("This delivery has already left.")
    if rider is not None and not rider.is_active:
        raise RestaurantError(f"{rider.name} is not active.")
    order.rider = rider
    if rider is None:
        order.handover = H.READY if order.handover == H.ASSIGNED else order.handover
    else:
        order.handover = H.ASSIGNED
    order.handover_at = timezone.now()
    order.save(update_fields=["rider", "handover", "handover_at"])
    if rider is not None and rider.user_id:
        notify(order.tenant, f"Delivery assigned: {order.number}",
               f"{order.customer_name or order.customer_phone}, {order.delivery_address}",
               level=Notification.Level.INFO, user=rider.user)
    return order


@transaction.atomic
def set_handover(order, new_status):
    order = _lock(order)
    if order.order_type == T.DINE_IN:
        raise RestaurantError("Dine-in orders don't have a hand-over status.")
    if order.status == RestaurantOrder.Status.VOID:
        raise RestaurantError("This order was cancelled.")
    if new_status not in H.values:
        raise RestaurantError("Unknown status.")
    if new_status == order.handover:
        return order
    if new_status not in ALLOWED[order.order_type].get(order.handover, set()):
        raise RestaurantError(f"An order that is '{order.get_handover_display().lower()}' can't go to "
                              f"'{H(new_status).label.lower()}'.")
    if new_status == H.OUT and order.rider_id is None:
        raise RestaurantError("Assign a rider before sending the order out.")
    order.handover, order.handover_at = new_status, timezone.now()
    order.save(update_fields=["handover", "handover_at"])
    if new_status == H.FAILED:
        notify(order.tenant, f"Delivery failed: {order.number}",
               f"{order.customer_name or order.customer_phone} at {order.delivery_address}",
               level=Notification.Level.WARNING)
    return order


def delivery_board(branch):
    """Open delivery + takeaway orders grouped by status for the board."""
    qs = (RestaurantOrder.objects.filter(branch=branch, order_type__in=(T.DELIVERY, T.TAKEAWAY),
                                         status=RestaurantOrder.Status.OPEN)
          .exclude(handover=H.COMPLETED).select_related("rider", "delivery_zone")
          .prefetch_related("lines", "payments").order_by("created_at"))
    return list(qs)
