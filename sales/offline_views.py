"""
sales/offline_views.py

    GET  /pos/offline/            the offline terminal shell (public, no user data)
    GET  /pos/offline/snapshot/   JSON catalogue + stock + register for this till (ETag)
    POST /pos/offline/sync/       JSON batch of sales rung up offline (idempotent)

The shell carries no data of its own: everything it shows comes from IndexedDB, filled
from the snapshot while the till was online. That is what makes it safe for the service
worker to cache it for everybody, including a browser that has not logged in yet.

The two JSON endpoints answer in JSON even when the caller is not allowed in (401/403),
never with a redirect to a login page, because the caller is a background script.
"""
import json
import logging

from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import TemplateView

from notifications.models import Notification
from notifications.utils import MANAGEMENT, notify, who
from users.mixins import MANAGEMENT_ROLES
from users.utils import log_action

from . import offline

log = logging.getLogger(__name__)

MAX_BATCH = 25
MAX_BODY_BYTES = 512 * 1024


class OfflineTerminalView(TemplateView):
    template_name = "sales/offline_terminal.html"

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        response["Cache-Control"] = "no-cache"
        return response


def _api_guard(request):
    """Returns (user, tenant, None) or (None, None, JsonResponse)."""
    user = request.user
    if not user.is_authenticated:
        return None, None, JsonResponse({"error": "auth_required"}, status=401)
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        return None, None, JsonResponse({"error": "no_tenant"}, status=403)
    if user.role not in ("CASHIER",) + MANAGEMENT_ROLES and not user.is_superuser:
        return None, None, JsonResponse({"error": "not_a_cashier"}, status=403)
    return user, tenant, None


@require_GET
@never_cache
@ensure_csrf_cookie
def snapshot(request):
    user, tenant, denied = _api_guard(request)
    if denied:
        return denied
    body, etag = offline.build_snapshot(user, tenant)
    quoted = f'"{etag}"'
    if request.headers.get("If-None-Match") == quoted:
        response = HttpResponse(status=304)
    else:
        response = JsonResponse(body)
    response["ETag"] = quoted
    return response


@require_POST
@never_cache
def sync(request):
    user, tenant, denied = _api_guard(request)
    if denied:
        return denied
    if len(request.body) > MAX_BODY_BYTES:
        return JsonResponse({"error": "too_large"}, status=413)
    try:
        sales = json.loads(request.body or b"{}").get("sales")
    except (ValueError, AttributeError):
        return JsonResponse({"error": "bad_json"}, status=400)
    if not isinstance(sales, list) or len(sales) > MAX_BATCH:
        return JsonResponse({"error": "bad_batch"}, status=400)

    results = []
    for raw in sales:
        if not isinstance(raw, dict):
            results.append({"uuid": "", "status": "rejected", "code": "bad_sale", "reason": "Malformed sale."})
            continue
        result = offline.process_offline_sale(user, tenant, raw)
        results.append(result)
        if result["status"] == "error":
            break  # keep order: stop here, the client retries this one and everything after it
        if result["status"] in ("created", "duplicate") and raw.get("from_online_cart"):
            _drop_matching_session_cart(request, raw.get("lines"))

    _record_batch(request, user, tenant, results)
    return JsonResponse({"results": results})


def _drop_matching_session_cart(request, lines):
    """
    A checkout that began online, then lost its connection mid-payment, leaves the server-side
    session cart behind. Once the sale is safely recorded, clear that cart -- but only if it is
    exactly the cart that was sold, so a new sale being built in the meantime is never wiped.
    """
    try:
        sold = {}
        for line in lines or []:
            sold[int(line["variant_id"])] = sold.get(int(line["variant_id"]), 0) + int(line["quantity"])
        cart = request.session.get("pos_cart") or {}
        held = {int(v["variant_id"]): int(v["quantity"]) for v in cart.values()}
        if held and held == sold:
            request.session["pos_cart"] = {}
            request.session.modified = True
    except (KeyError, TypeError, ValueError):
        pass


def _record_batch(request, user, tenant, results):
    created = [r for r in results if r["status"] == "created"]
    if not created:
        return
    flagged = [r for r in created if r.get("flags")]
    log_action(user, "OFFLINE_SALES_SYNCED",
               f"{len(created)} offline sale(s) synced: " + ", ".join(r["invoice"] for r in created[:10]),
               request)
    if flagged:
        notes = []
        for r in flagged[:5]:
            kinds = sorted({f["type"].replace("_", " ").lower() for f in r["flags"]})
            notes.append(f'{r["invoice"]} ({", ".join(kinds)})')
        notify(tenant, "Offline sales need a look",
               f"{who(user)} synced {len(created)} offline sale(s); {len(flagged)} flagged: "
               + "; ".join(notes) + ("; ..." if len(flagged) > 5 else ""),
               level=Notification.Level.WARNING, roles=MANAGEMENT)
