"""
Public JSON API for online ordering (website / app / aggregator).  Authenticated with the
restaurant's API key:   Authorization: Bearer us_<id>_<secret>

GET  /restaurant/api/v1/menu/      the live menu, options and delivery zones
POST /restaurant/api/v1/orders/    place an order (idempotent on external_ref)
GET  /restaurant/api/v1/orders/<external_ref>/   status of an order
"""
import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django_ratelimit.decorators import ratelimit

from inventory.models import Branch
from tenants.context import tenant_context

from .models import RestaurantOrder
from .services import online
from .services.errors import RestaurantError


def _auth(request):
    tenant = online.authenticate(request.headers.get("Authorization", ""))
    if tenant is None:
        return None, JsonResponse({"error": "Invalid or missing API key."}, status=401)
    if not tenant.has_access:
        return None, JsonResponse({"error": "This restaurant's subscription is not active."}, status=402)
    return tenant, None


def _branch(tenant, request):
    qs = Branch.all_objects.filter(tenant=tenant, is_active=True)
    code = request.GET.get("branch") or ""
    b = qs.filter(code=code).first() if code else None
    return b or qs.filter(is_main=True).first() or qs.first()


@csrf_exempt
@ratelimit(key="ip", rate="120/m", block=True)
def menu(request):
    if request.method != "GET":
        return JsonResponse({"error": "GET only."}, status=405)
    tenant, err = _auth(request)
    if err:
        return err
    with tenant_context(tenant):
        return JsonResponse(online.public_menu())


@csrf_exempt
@ratelimit(key="ip", rate="60/m", block=True)
def orders(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only."}, status=405)
    tenant, err = _auth(request)
    if err:
        return err
    try:
        payload = json.loads(request.body or b"{}")
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        return JsonResponse({"error": "Send a JSON object."}, status=400)
    with tenant_context(tenant):
        branch = _branch(tenant, request)
        if branch is None:
            return JsonResponse({"error": "No branch configured."}, status=409)
        try:
            order, created = online.create_order_from_payload(tenant, branch, payload)
        except RestaurantError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"order_number": order.number, "external_ref": order.external_ref,
                             "total": str(order.total), "status": "awaiting_acceptance" if order.needs_acceptance else "accepted",
                             "created": created}, status=201 if created else 200)


@csrf_exempt
@ratelimit(key="ip", rate="120/m", block=True)
def order_status(request, ref):
    tenant, err = _auth(request)
    if err:
        return err
    with tenant_context(tenant):
        o = RestaurantOrder.objects.filter(external_ref=ref).first()
        if o is None:
            return JsonResponse({"error": "Not found."}, status=404)
        return JsonResponse({"order_number": o.number, "external_ref": o.external_ref, "status": o.status,
                             "awaiting_acceptance": o.needs_acceptance, "handover": o.handover,
                             "rider": o.rider.name if o.rider_id else None,
                             "rider_phone": o.rider.phone if o.rider_id else None, "total": str(o.total)})
