import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from inventory.models import StockLevel
from sales.models import CashRegisterSession, Order
from sales.utils import resolve_user_branch

from . import forms as F
from .models import (BillPayment, CashierShift, DeliveryRider, DeliveryZone, FloorZone, Ingredient, KitchenTicket,
                     MenuItem, Modifier, OrderLine, Recipe, RecipeLine, RestaurantOrder, Station, Table, WasteRecord,
                     get_restaurant_settings, money)
from .services import billing, costing, delivery, kitchen, online
from .services import orders as osvc
from .services import stock as stock_svc
from .services.errors import RestaurantError
from .services.setup import create_ingredient, create_menu_item, ensure_restaurant_setup, update_ingredient, update_menu_item

User = get_user_model()
MGMT = ("ADMIN", "MANAGER")
FLOOR = ("ADMIN", "MANAGER", "CASHIER", "WAITER")
TILL = ("ADMIN", "MANAGER", "CASHIER")
KITCHEN = ("ADMIN", "MANAGER", "KITCHEN")
ANY = ("ADMIN", "MANAGER", "CASHIER", "WAITER", "KITCHEN")


def rview(roles=ANY, api=False):
    """Login + restaurant/hotel only (404 for every other business type) + role check + friendly errors."""
    def deco(fn):
        @wraps(fn)
        def inner(request, *a, **kw):
            u = request.user
            if not u.is_authenticated:
                if api:
                    return JsonResponse({"ok": False, "error": "Please sign in again."}, status=401)
                return redirect_to_login(request.get_full_path())
            tenant = getattr(request, "tenant", None)
            if tenant is None or not tenant.is_hospitality:
                raise Http404()
            if u.role not in roles:
                if api:
                    return JsonResponse({"ok": False, "error": "You don't have permission to do that."}, status=403)
                raise PermissionDenied
            try:
                return fn(request, *a, **kw)
            except RestaurantError as exc:
                if api:
                    return JsonResponse({"ok": False, "error": str(exc)}, status=400)
                messages.error(request, str(exc))
                return redirect(request.META.get("HTTP_REFERER") or reverse("restaurant:dashboard"))
        return inner
    return deco


def _body(request):
    if request.content_type == "application/json":
        try:
            return json.loads(request.body or b"{}")
        except ValueError:
            raise RestaurantError("Bad request.")
    return request.POST.dict()


def _dec(v, name="amount"):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        raise RestaurantError(f"Enter a valid {name}.")


def _branch(request):
    b = resolve_user_branch(request.user)
    if b is None:
        raise RestaurantError("No branch is set up yet.")
    return b


def _name(u):
    return (u.get_full_name() or u.username.split("__")[-1]) if u else ""


def _order(request, pk):
    return get_object_or_404(RestaurantOrder, pk=pk, branch=_branch(request))


# ======================================================================================
# dashboard
# ======================================================================================
@rview(MGMT + ("CASHIER",))
def dashboard(request):
    ensure_restaurant_setup(request.tenant)
    branch = _branch(request)
    today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    open_orders = RestaurantOrder.objects.filter(branch=branch, status="OPEN")
    paid = RestaurantOrder.objects.filter(branch=branch, status="PAID", closed_at__gte=today)
    tables = Table.objects.filter(branch=branch, is_active=True, merged_into__isnull=True)
    occupied = open_orders.filter(order_type="DINE_IN").values("table").distinct().count()
    low = [sl for sl in StockLevel.objects.filter(branch=branch, variant__ingredient__is_active=True)
           .select_related("variant__product", "variant__ingredient") if sl.is_low_stock][:8]
    ctx = {
        "branch": branch, "open_count": open_orders.count(), "tables_total": tables.count(), "tables_occupied": occupied,
        "paid_today": paid.count(), "sales_today": paid.aggregate(t=Sum("total"))["t"] or 0,
        "pending_online": open_orders.filter(needs_acceptance=True).count(),
        "kitchen_queue": KitchenTicket.objects.filter(branch=branch, status__in=("PENDING", "PREPARING")).count(),
        "ready_now": KitchenTicket.objects.filter(branch=branch, status="READY").count(),
        "low_stock": low, "prep": kitchen.prep_time_report(branch, today),
        "register": CashRegisterSession.get_active_session(branch),
        "waste_today": WasteRecord.objects.filter(branch=branch, created_at__gte=today).aggregate(t=Sum("cost"))["t"] or 0,
    }
    return render(request, "restaurant/dashboard.html", ctx)


# ======================================================================================
# floor plan
# ======================================================================================
@rview(FLOOR)
def floor(request):
    ensure_restaurant_setup(request.tenant)
    branch = _branch(request)
    return render(request, "restaurant/floor.html", {
        "branch": branch, "can_manage": request.user.role in MGMT,
        "zones": FloorZone.objects.filter(branch=branch),
        "waiters": User.objects.filter(tenant=request.tenant, is_active=True, role__in=("WAITER", "MANAGER", "ADMIN", "CASHIER")),
        "types": RestaurantOrder.Type.choices, "delivery_zones": DeliveryZone.objects.filter(is_active=True),
        "default_fee": get_restaurant_settings().default_delivery_fee,
    })


@rview(FLOOR, api=True)
def floor_data(request):
    branch = _branch(request)
    return JsonResponse({"ok": True, "tables": osvc.floor_snapshot(branch),
                         "ready": len(kitchen.ready_for_waiter(branch, request.user, all_orders=request.user.role != "WAITER"))})


@rview(MGMT)
def floor_editor(request):
    branch = _branch(request)
    return render(request, "restaurant/floor_editor.html", {
        "branch": branch, "zones": FloorZone.objects.filter(branch=branch), "form": F.TableForm()})


@rview(MGMT, api=True)
@require_POST
def layout_save(request):
    data = _body(request)
    branch = _branch(request)
    n = 0
    for row in data.get("tables", []):
        t = Table.objects.filter(pk=row.get("id"), branch=branch).first()
        if t:
            t.x, t.y = max(0, min(95, float(row["x"]))), max(0, min(95, float(row["y"])))
            t.w, t.h = max(3, min(40, float(row.get("w", t.w)))), max(3, min(40, float(row.get("h", t.h))))
            t.save(update_fields=["x", "y", "w", "h"])
            n += 1
    return JsonResponse({"ok": True, "saved": n})


@rview(MGMT, api=True)
@require_POST
def table_save(request):
    d = _body(request)
    branch = _branch(request)
    name = (d.get("name") or "").strip()
    if not name:
        raise RestaurantError("Give the table a name or number.")
    zone = FloorZone.objects.filter(pk=d.get("zone_id"), branch=branch).first() if d.get("zone_id") else None
    seats = max(1, min(60, int(d.get("seats") or 4)))
    shape = d.get("shape") if d.get("shape") in Table.Shape.values else "SQUARE"
    if d.get("id"):
        t = get_object_or_404(Table, pk=d["id"], branch=branch)
    else:
        t = Table(branch=branch)
    if Table.objects.filter(branch=branch, name__iexact=name).exclude(pk=t.pk).exists():
        raise RestaurantError(f"There is already a table called {name}.")
    t.name, t.seats, t.shape, t.zone = name, seats, shape, zone
    if shape == "RECT" and not t.pk:
        t.w = 14
    t.save()
    return JsonResponse({"ok": True, "id": t.pk})


@rview(MGMT, api=True)
@require_POST
def table_delete(request, pk):
    t = get_object_or_404(Table, pk=pk, branch=_branch(request))
    if osvc.table_open_orders(t).exists():
        raise RestaurantError("This table has an open order.")
    t.merged_tables.update(merged_into=None)
    t.is_active = False
    t.save(update_fields=["is_active"])
    return JsonResponse({"ok": True})


@rview(MGMT, api=True)
@require_POST
def zone_save(request):
    name = (_body(request).get("name") or "").strip()
    if not name:
        raise RestaurantError("Give the area a name.")
    z, created = FloorZone.objects.get_or_create(branch=_branch(request), name=name, defaults={"tenant": request.tenant})
    return JsonResponse({"ok": True, "id": z.pk, "created": created})


@rview(FLOOR, api=True)
@require_POST
def table_action(request, pk, action):
    t = get_object_or_404(Table, pk=pk, branch=_branch(request))
    d = _body(request)
    if action == "assign_waiter":
        w = User.objects.filter(pk=d.get("waiter_id"), tenant=request.tenant, is_active=True).first() if d.get("waiter_id") else None
        osvc.assign_waiter(t, w)
    elif action == "hold":
        if d.get("hold") not in Table.Hold.values:
            raise RestaurantError("Unknown table status.")
        osvc.set_hold(t, d["hold"], d.get("reserved_for", ""))
    elif action == "merge":
        if request.user.role not in MGMT + ("CASHIER",):
            raise PermissionDenied
        others = list(Table.objects.filter(pk__in=d.get("table_ids") or [], branch=t.branch))
        if not others:
            raise RestaurantError("Choose the tables to join.")
        osvc.merge_tables(t, others)
    elif action == "split":
        osvc.split_tables(t)
    else:
        raise Http404()
    return JsonResponse({"ok": True})


# ======================================================================================
# orders
# ======================================================================================
def order_payload(o):
    o = RestaurantOrder.objects.prefetch_related("lines__modifiers", "payments", "tickets__station").select_related(
        "table", "waiter", "rider", "delivery_zone", "sales_order__receipt").get(pk=o.pk)
    lines = []
    for l in o.lines.all():
        lines.append({
            "id": l.pk, "name": l.name, "qty": l.quantity, "unit_price": str(l.unit_price), "total": str(l.line_total),
            "note": l.note, "status": l.status, "status_label": l.get_status_display(),
            "mods": [(("No " if m.kind == "REMOVE" else "+ ") + m.name) if m.kind != "CHOICE" else m.name for m in l.modifiers.all()],
            "station": l.station.name if l.station_id else "", "ticket": l.ticket_id,
        })
    receipt = None
    so = o.sales_order
    if so is not None and hasattr(so, "receipt"):
        receipt = reverse("receipt-detail", args=[so.receipt.pk])
    return {
        "id": o.pk, "number": o.number, "type": o.order_type, "type_label": o.get_order_type_display(),
        "status": o.status, "table_id": o.table_id, "table": o.table.label if o.table_id else "",
        "waiter": _name(o.waiter), "guests": o.guests, "notes": o.notes, "needs_acceptance": o.needs_acceptance,
        "customer_name": o.customer_name, "customer_phone": o.customer_phone, "address": o.delivery_address,
        "delivery_notes": o.delivery_notes, "rider": o.rider.name if o.rider_id else "",
        "handover": o.handover, "handover_label": o.get_handover_display(),
        "lines": lines, "subtotal": str(o.subtotal), "discount_total": str(o.discount_total),
        "discount_type": o.discount_type, "discount_value": str(o.discount_value), "discount_reason": o.discount_reason,
        "service_pct": str(o.service_charge_percent), "service_charge": str(o.service_charge),
        "delivery_fee": str(o.delivery_fee), "total": str(o.total), "paid": str(o.amount_paid), "balance": str(o.balance),
        "payments": [{"id": p.pk, "method": p.get_method_display(), "amount": str(p.amount), "reference": p.reference,
                      "payer": p.payer, "void": p.is_void} for p in o.payments.all()],
        "draft_count": len(o.draft_lines), "receipt_url": receipt,
        "split_from": o.split_from.number if o.split_from_id else "",
    }


@rview(FLOOR, api=True)
@require_POST
def order_create(request):
    d = _body(request)
    branch = _branch(request)
    otype = d.get("type", "DINE_IN")
    if otype not in RestaurantOrder.Type.values:
        raise RestaurantError("Choose dine-in, takeaway or delivery.")
    table = None
    if otype == "DINE_IN":
        table = get_object_or_404(Table, pk=d.get("table_id"), branch=branch)
    zone = DeliveryZone.objects.filter(pk=d.get("delivery_zone_id"), is_active=True).first() if d.get("delivery_zone_id") else None
    sched = parse_datetime(d["scheduled_for"]) if d.get("scheduled_for") else None
    order = osvc.create_order(
        user=request.user, branch=branch, order_type=otype, table=table, guests=d.get("guests") or 1,
        customer_name=d.get("customer_name", ""), customer_phone=d.get("customer_phone", ""),
        delivery_address=d.get("address", ""), delivery_notes=d.get("delivery_notes", ""), delivery_zone=zone,
        notes=d.get("notes", ""), source="PHONE" if d.get("phone_order") else "POS", scheduled_for=sched)
    return JsonResponse({"ok": True, "id": order.pk, "url": reverse("restaurant:order", args=[order.pk])})


@rview(FLOOR)
def order_page(request, pk):
    order = _order(request, pk)
    rs = get_restaurant_settings()
    return render(request, "restaurant/order.html", {
        "order": order, "can_pay": request.user.role in TILL, "is_mgmt": request.user.role in MGMT,
        "methods": [{"value": m.value, "label": m.label} for m in billing.enabled_methods(rs)],
        "tables": Table.objects.filter(branch=order.branch, is_active=True, merged_into__isnull=True),
        "open_orders": RestaurantOrder.objects.filter(branch=order.branch, status="OPEN").exclude(pk=order.pk),
        "riders": DeliveryRider.objects.filter(is_active=True), "zones": DeliveryZone.objects.filter(is_active=True),
    })


@rview(FLOOR, api=True)
def order_data(request, pk):
    return JsonResponse({"ok": True, "order": order_payload(_order(request, pk))})


@rview(FLOOR, api=True)
def menu_data(request):
    items = (MenuItem.objects.filter(is_active=True, variant__is_active=True)
             .select_related("variant__product__category", "station").order_by("variant__product__category__name", "sort_order"))
    mods = list(Modifier.objects.filter(is_active=True).prefetch_related("menu_items"))
    cats, out = {}, []
    for it in items:
        cat = it.category
        cats.setdefault(cat.pk, {"id": cat.pk, "name": cat.name})
        out.append({
            "id": it.pk, "name": it.name, "price": str(it.price), "cat": cat.pk, "kind": it.kind,
            "available": it.is_available, "station": it.station.name if it.station_id else "",
            "options": [{"id": m.pk, "name": m.name, "kind": m.kind, "price": str(m.price)}
                        for m in mods if not m.menu_items.all() or it in m.menu_items.all()],
        })
    return JsonResponse({"ok": True, "categories": list(cats.values()), "items": out})


@rview(FLOOR, api=True)
@require_POST
def order_action(request, pk, action):
    order = _order(request, pk)
    d = _body(request)
    role, user = request.user.role, request.user
    extra = {}

    def need(roles):
        if role not in roles:
            raise RestaurantError("Ask a manager to do that.")

    if action == "add_line":
        item = get_object_or_404(MenuItem, pk=d.get("item_id"))
        osvc.add_line(order, item, d.get("qty") or 1, d.get("option_ids") or [], d.get("note", ""))
    elif action == "qty":
        line = get_object_or_404(OrderLine, pk=d.get("line_id"), order=order)
        osvc.set_line_quantity(line, int(d.get("qty", 0)))
    elif action == "void_line":
        line = get_object_or_404(OrderLine, pk=d.get("line_id"), order=order)
        if line.status != OrderLine.Status.DRAFT:
            need(MGMT)
        osvc.void_line(line, user, d.get("reason", ""))
    elif action == "send":
        kitchen.send_to_kitchen(order, user)
    elif action == "accept":
        need(TILL)
        kitchen.accept_online_order(order, user)
    elif action == "details":
        zone = DeliveryZone.objects.filter(pk=d.get("delivery_zone_id")).first() if d.get("delivery_zone_id") else None
        fields = {k: d[k] for k in ("guests", "notes", "customer_name", "customer_phone", "delivery_address", "delivery_notes") if k in d}
        if "address" in d:
            fields["delivery_address"] = d["address"]
        if zone:
            fields["delivery_zone"] = zone
        osvc.update_details(order, **fields)
    elif action == "delivery_fee":
        need(TILL)
        osvc.set_delivery_fee(order, _dec(d.get("fee"), "fee"))
    elif action == "discount":
        need(MGMT)
        osvc.set_discount(order, user, d.get("type", "NONE"), _dec(d.get("value") or 0, "discount"), d.get("reason", ""))
    elif action == "transfer":
        osvc.transfer_order(order, get_object_or_404(Table, pk=d.get("table_id"), branch=order.branch))
    elif action == "merge":
        osvc.merge_orders(order, _order(request, d.get("target_id")))
        extra["redirect"] = reverse("restaurant:order", args=[d.get("target_id")])
    elif action == "split":
        child = osvc.split_order(order, d.get("line_ids") or [], user)
        extra["redirect"] = reverse("restaurant:order", args=[child.pk])
    elif action == "shares":
        extra["shares"] = [str(s) for s in osvc.equal_shares(order, d.get("parts") or 2)]
    elif action == "pay":
        need(TILL)
        billing.pay(order, user=user, method=d.get("method", "CASH"),
                    amount=d.get("amount"), tendered=d.get("tendered"),
                    reference=d.get("reference", ""), payer=d.get("payer", ""))
    elif action == "void_payment":
        need(MGMT)
        billing.void_payment(get_object_or_404(BillPayment, pk=d.get("payment_id"), order=order), user)
    elif action == "complimentary":
        need(MGMT)
        billing.close_complimentary(order, user)
    elif action == "cancel":
        need(MGMT)
        osvc.cancel_order(order, user, d.get("reason", ""))
    elif action == "rider":
        need(TILL)
        rider = DeliveryRider.objects.filter(pk=d.get("rider_id")).first() if d.get("rider_id") else None
        delivery.assign_rider(order, rider)
    elif action == "handover":
        need(TILL)
        delivery.set_handover(order, d.get("status", ""))
    else:
        raise Http404()
    return JsonResponse({"ok": True, "order": order_payload(order), **extra})


@rview(FLOOR)
def orders_list(request):
    branch = _branch(request)
    qs = RestaurantOrder.objects.filter(branch=branch).select_related("table", "waiter").prefetch_related("lines")
    status = request.GET.get("status", "OPEN")
    kind = request.GET.get("type", "")
    if status in RestaurantOrder.Status.values:
        qs = qs.filter(status=status)
    if kind in RestaurantOrder.Type.values:
        qs = qs.filter(order_type=kind)
    if request.user.role == "WAITER":
        qs = qs.filter(waiter=request.user)
    return render(request, "restaurant/orders.html", {
        "orders": qs[:200], "status": status, "kind": kind, "statuses": RestaurantOrder.Status.choices,
        "types": RestaurantOrder.Type.choices})


# ======================================================================================
# kitchen display + waiter
# ======================================================================================
@rview(KITCHEN)
def kds(request):
    branch = _branch(request)
    return render(request, "restaurant/kds.html", {
        "stations": Station.objects.filter(is_active=True), "late": get_restaurant_settings().kds_late_minutes})


@rview(KITCHEN, api=True)
def kds_data(request):
    sid = request.GET.get("station")
    return JsonResponse({"ok": True, "tickets": kitchen.kds_board(_branch(request), int(sid) if sid and sid.isdigit() else None)})


@rview(FLOOR + ("KITCHEN",), api=True)
@require_POST
def ticket_action(request, pk, action):
    ticket = get_object_or_404(KitchenTicket, pk=pk, branch=_branch(request))
    role = request.user.role
    if action in ("start", "ready", "recall") and role not in KITCHEN:
        raise RestaurantError("Only the kitchen can do that.")
    if action == "serve" and role == "KITCHEN":
        pass  # the kitchen may bump a plate as served at the pass
    fn = {"start": kitchen.start_ticket, "ready": kitchen.ready_ticket, "serve": kitchen.serve_ticket,
          "recall": kitchen.recall_ticket}.get(action)
    if fn is None:
        raise Http404()
    fn(ticket, request.user)
    return JsonResponse({"ok": True})


@rview(FLOOR, api=True)
def waiter_data(request):
    branch = _branch(request)
    mine = request.user.role == "WAITER"
    tickets = kitchen.ready_for_waiter(branch, request.user, all_orders=not mine)
    tables = [t for t in osvc.floor_snapshot(branch) if not mine or t["waiter_id"] in (None, request.user.pk)]
    return JsonResponse({"ok": True, "ready": tickets, "tables": tables})


@rview(FLOOR)
def waiter(request):
    return render(request, "restaurant/waiter.html", {"branch": _branch(request)})


# ======================================================================================
# delivery / takeaway
# ======================================================================================
@rview(TILL)
def delivery_board(request):
    branch = _branch(request)
    orders = delivery.delivery_board(branch)
    pending = [o for o in orders if o.needs_acceptance]
    groups = [("Being prepared", [o for o in orders if o.handover == "PENDING" and not o.needs_acceptance]),
              ("Ready", [o for o in orders if o.handover == "READY"]),
              ("Rider assigned", [o for o in orders if o.handover == "ASSIGNED"]),
              ("Out for delivery", [o for o in orders if o.handover == "OUT"]),
              ("Failed", [o for o in orders if o.handover == "FAILED"])]
    return render(request, "restaurant/delivery.html", {
        "pending": pending, "groups": groups, "riders": DeliveryRider.objects.filter(is_active=True),
        "zones": DeliveryZone.objects.filter(is_active=True), "default_fee": get_restaurant_settings().default_delivery_fee})


# ======================================================================================
# menu, recipes, ingredients, waste
# ======================================================================================
@rview(MGMT)
def menu_list(request):
    items = MenuItem.objects.select_related("variant__product__category", "station").prefetch_related("recipe__lines")
    rows = []
    for it in items:
        cost = costing.recipe_cost(it)
        price = Decimal(it.price)
        rows.append({"item": it, "cost": cost, "margin": money(price - cost),
                     "fc": money(cost / price * 100) if price > 0 else None,
                     "has_recipe": it.fulfilment != "RECIPE" or (hasattr(it, "recipe") and it.recipe.lines.exists())})
    return render(request, "restaurant/menu_list.html", {"rows": rows})


@rview(MGMT)
def menu_edit(request, pk=None):
    item = get_object_or_404(MenuItem, pk=pk) if pk else None
    if request.method == "POST":
        form = F.MenuItemForm(request.POST)
        if form.is_valid():
            c = form.cleaned_data
            args = dict(name=c["name"], category=c["category"], price=c["price"], kind=c["kind"],
                        fulfilment=c["fulfilment"], station=c["station"], prep_minutes=c["prep_minutes"],
                        description=c["description"])
            if item:
                update_menu_item(item, **args, is_available=c["is_available"], is_active=c["is_active"])
                if c["fulfilment"] != "RECIPE" and c["cost"] is not None:
                    item.variant.cost_price = c["cost"]
                    item.variant.save(update_fields=["cost_price"])
                costing.refresh_menu_cost(item)
            else:
                item = create_menu_item(**args, cost=c["cost"] or Decimal("0"))
            messages.success(request, f"{item.name} saved.")
            if item.fulfilment == "RECIPE":
                return redirect("restaurant:recipe", item.pk)
            return redirect("restaurant:menu")
    else:
        form = F.MenuItemForm.for_item(item) if item else F.MenuItemForm()
    return render(request, "restaurant/form.html", {"form": form, "title": f"Edit {item.name}" if item else "New menu item",
                                                     "back": reverse("restaurant:menu")})


@rview(MGMT)
def recipe_edit(request, pk):
    item = get_object_or_404(MenuItem, pk=pk)
    recipe, _ = Recipe.objects.get_or_create(menu_item=item, defaults={"tenant": request.tenant})
    form = F.RecipeLineForm()
    if request.method == "POST":
        act = request.POST.get("action")
        if act == "add":
            form = F.RecipeLineForm(request.POST)
            if form.is_valid():
                c = form.cleaned_data
                RecipeLine.objects.update_or_create(
                    recipe=recipe, ingredient=c["ingredient"],
                    defaults={"quantity": c["quantity"], "waste_percent": c["waste_percent"], "tenant": request.tenant})
                costing.refresh_menu_cost(item)
                return redirect("restaurant:recipe", pk)
        elif act == "remove":
            RecipeLine.objects.filter(recipe=recipe, pk=request.POST.get("line_id")).delete()
            costing.refresh_menu_cost(item)
            return redirect("restaurant:recipe", pk)
        elif act == "yield":
            try:
                y = Decimal(request.POST.get("yield_portions") or 1)
                if y <= 0:
                    raise InvalidOperation
                recipe.yield_portions, recipe.notes = y, request.POST.get("notes", "")[:2000]
                recipe.save()
                costing.refresh_menu_cost(item)
            except InvalidOperation:
                messages.error(request, "Portions must be above zero.")
            return redirect("restaurant:recipe", pk)
    return render(request, "restaurant/recipe.html", {
        "item": item, "recipe": recipe, "form": form, "bd": costing.cost_breakdown(item)})


@rview(MGMT)
@require_POST
def recost(request):
    n = costing.refresh_all_costs()
    messages.success(request, f"Recipe costs refreshed ({n} dish{'es' if n != 1 else ''} changed).")
    return redirect("restaurant:menu")


@rview(MGMT)
def ingredient_list(request):
    branch = _branch(request)
    levels = {sl.variant_id: sl for sl in StockLevel.objects.filter(branch=branch, variant__ingredient__isnull=False)}
    rows = []
    for ing in Ingredient.objects.select_related("variant__product__unit_of_measure"):
        sl = levels.get(ing.variant_id)
        rows.append({"ing": ing, "qty": sl.quantity if sl else 0, "low": bool(sl and sl.is_low_stock) or sl is None})
    return render(request, "restaurant/ingredients.html", {"rows": rows, "only_low": request.GET.get("low") == "1"})


@rview(MGMT)
def ingredient_edit(request, pk=None):
    ing = get_object_or_404(Ingredient, pk=pk) if pk else None
    if request.method == "POST":
        form = F.IngredientForm(request.POST)
        if form.is_valid():
            c = form.cleaned_data
            if ing:
                update_ingredient(ing, name=c["name"], cost_per_unit=c["cost_per_unit"], low_stock_threshold=c["low_stock_threshold"],
                                  recipe_unit=c["recipe_unit"], units_per_base=c["units_per_base"], is_active=c["is_active"])
                costing.refresh_all_costs()
            else:
                create_ingredient(name=c["name"], unit=c["unit"], cost_per_unit=c["cost_per_unit"],
                                  low_stock_threshold=c["low_stock_threshold"], recipe_unit=c["recipe_unit"],
                                  units_per_base=c["units_per_base"])
            messages.success(request, "Ingredient saved. Receive stock for it with a Purchase Order.")
            return redirect("restaurant:ingredients")
    else:
        form = F.IngredientForm.for_ingredient(ing) if ing else F.IngredientForm()
        if ing:
            form.fields["unit"].disabled = True
    return render(request, "restaurant/form.html", {"form": form, "title": f"Edit {ing.name}" if ing else "New ingredient",
                                                     "back": reverse("restaurant:ingredients")})


@rview(KITCHEN)
def waste(request):
    branch = _branch(request)
    form = F.WasteForm()
    if request.method == "POST":
        form = F.WasteForm(request.POST)
        if form.is_valid():
            c = form.cleaned_data
            kind, pk = c["subject"][0], int(c["subject"][1:])
            try:
                if kind == "i":
                    stock_svc.record_waste(branch=branch, user=request.user, reason=c["reason"], notes=c["notes"],
                                           ingredient=get_object_or_404(Ingredient, pk=pk), quantity=c["quantity"])
                else:
                    stock_svc.record_waste(branch=branch, user=request.user, reason=c["reason"], notes=c["notes"],
                                           menu_item=get_object_or_404(MenuItem, pk=pk), portions=c["quantity"])
                messages.success(request, "Waste recorded and taken out of stock.")
                return redirect("restaurant:waste")
            except RestaurantError as exc:
                form.add_error(None, str(exc))
    since = timezone.now() - timedelta(days=30)
    recs = WasteRecord.objects.filter(branch=branch, created_at__gte=since).select_related("ingredient__variant__product", "menu_item__variant__product", "recorded_by")
    by_reason = recs.values("reason").annotate(total=Sum("cost"), n=Count("id")).order_by("-total")
    labels = dict(WasteRecord.Reason.choices)
    return render(request, "restaurant/waste.html", {
        "form": form, "records": recs[:100], "total": recs.aggregate(t=Sum("cost"))["t"] or 0,
        "by_reason": [{"reason": labels[r["reason"]], "total": r["total"], "n": r["n"]} for r in by_reason]})


# ---------------------------------------------------------------------- generic small lists
CRUD = {
    "stations": dict(model=Station, form=F.StationForm, title="Kitchen stations", cols=["name", "color", "sort_order", "is_active"]),
    "modifiers": dict(model=Modifier, form=F.ModifierForm, title="Customizations & extras", cols=["name", "kind", "price", "ingredient", "is_active"]),
    "riders": dict(model=DeliveryRider, form=F.RiderForm, title="Delivery riders", cols=["name", "phone", "is_active"]),
    "zones": dict(model=DeliveryZone, form=F.ZoneForm, title="Delivery zones & fees", cols=["name", "fee", "is_active"]),
}


@rview(MGMT)
def crud_list(request, key):
    cfg = CRUD.get(key) or (_ for _ in ()).throw(Http404())
    objs = cfg["model"].objects.all()
    rows = [[getattr(o, c) if not hasattr(o, f"get_{c}_display") else getattr(o, f"get_{c}_display")() for c in cfg["cols"]] for o in objs]
    return render(request, "restaurant/crud_list.html", {
        "key": key, "title": cfg["title"], "cols": [c.replace("_", " ").title() for c in cfg["cols"]],
        "rows": list(zip(objs, rows))})


@rview(MGMT)
def crud_edit(request, key, pk=None):
    cfg = CRUD.get(key) or (_ for _ in ()).throw(Http404())
    obj = get_object_or_404(cfg["model"], pk=pk) if pk else None
    form = cfg["form"](request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        o = form.save(commit=False)
        if not o.pk:
            o.tenant = request.tenant
        o.save()
        form.save_m2m()
        messages.success(request, "Saved.")
        return redirect("restaurant:crud", key)
    return render(request, "restaurant/form.html", {"form": form, "title": ("Edit " if obj else "Add ") + cfg["title"].lower(),
                                                     "back": reverse("restaurant:crud", args=[key])})


# ======================================================================================
# shifts, settings, reports
# ======================================================================================
@rview(TILL)
def shift_page(request):
    branch = _branch(request)
    shift = billing.current_shift(request.user)
    if request.method == "POST":
        act = request.POST.get("action")
        try:
            if act == "open":
                billing.open_shift(request.user, branch, _dec(request.POST.get("opening_float") or 0, "float"))
            elif act == "close" and shift:
                def opt(k):
                    v = request.POST.get(k, "").strip()
                    return _dec(v, k) if v else None
                billing.close_shift(shift, request.user, counted_cash=_dec(request.POST.get("counted_cash") or 0, "cash"),
                                    counted_mpesa=opt("counted_mpesa"), counted_card=opt("counted_card"),
                                    notes=request.POST.get("notes", ""))
                messages.success(request, "Shift closed.")
                return redirect("restaurant:shifts")
        except RestaurantError as exc:
            messages.error(request, str(exc))
        return redirect("restaurant:shift")
    ctx = {"shift": shift, "rs": get_restaurant_settings(), "register": CashRegisterSession.get_active_session(branch)}
    if shift:
        ctx["t"] = billing.shift_totals(shift)
        ctx["payments"] = shift.payments.select_related("order").filter(is_void=False)
    return render(request, "restaurant/shift.html", ctx)


@rview(TILL)
def shifts_history(request):
    qs = CashierShift.objects.filter(branch=_branch(request), status="CLOSED").select_related("cashier")
    if request.user.role == "CASHIER":
        qs = qs.filter(cashier=request.user)
    return render(request, "restaurant/shifts.html", {"shifts": qs[:100]})


@rview(MGMT)
def settings_page(request):
    rs = get_restaurant_settings()
    form = F.SettingsForm(request.POST or None, instance=rs)
    new_key = None
    if request.method == "POST":
        act = request.POST.get("action")
        if act == "key_new":
            new_key = online.generate_api_key(request.tenant)
        elif act == "key_revoke":
            online.revoke_api_key(request.tenant)
            messages.success(request, "API key revoked.")
            return redirect("restaurant:settings")
        elif form.is_valid():
            form.save()
            messages.success(request, "Settings saved.")
            return redirect("restaurant:settings")
    return render(request, "restaurant/settings.html", {
        "form": form, "rs": get_restaurant_settings(), "new_key": new_key,
        "api_base": request.build_absolute_uri("/restaurant/api/v1/")})


@rview(MGMT)
def reports(request):
    branch = _branch(request)
    days = int(request.GET.get("days", 7)) if request.GET.get("days", "7").isdigit() else 7
    since = timezone.now() - timedelta(days=days)
    lines = (OrderLine.objects.filter(order__branch=branch, order__status="PAID", order__closed_at__gte=since)
             .exclude(status="VOID").values("menu_item__variant__product__name")
             .annotate(qty=Sum("quantity")).order_by("-qty")[:15])
    sales = Order.objects.filter(branch=branch, restaurant_order__isnull=False, created_at__gte=since)
    agg = sales.aggregate(rev=Sum("total_revenue"), cogs=Sum("total_cogs"), profit=Sum("total_profit"),
                          svc=Sum("service_charge"), fee=Sum("delivery_fee"), n=Count("id"))
    by_type = RestaurantOrder.objects.filter(branch=branch, status="PAID", closed_at__gte=since).values("order_type").annotate(
        n=Count("id"), total=Sum("total"))
    return render(request, "restaurant/reports.html", {
        "days": days, "top": lines, "agg": agg, "by_type": by_type,
        "prep": kitchen.prep_time_report(branch, since),
        "waste": WasteRecord.objects.filter(branch=branch, created_at__gte=since).aggregate(t=Sum("cost"))["t"] or 0})
