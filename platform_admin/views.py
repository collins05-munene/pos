from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views import View

from billing import services
from billing.models import BillingEvent, Invoice, MpesaPayment
from tenants.middleware import SUPPORT_SESSION_KEY
from tenants.models import Tenant
from .metrics import platform_overview


class SuperuserRequiredMixin(UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        u = self.request.user
        return u.is_authenticated and u.is_superuser


class OverviewView(SuperuserRequiredMixin, View):
    def get(self, request):
        return JsonResponse(platform_overview())


class TenantListView(SuperuserRequiredMixin, View):
    def get(self, request):
        qs = Tenant.objects.select_related("subscription").annotate(
            user_count=Count("users", distinct=True))
        if status := request.GET.get("status"):
            qs = qs.filter(subscription__status=status)
        if q := request.GET.get("q"):
            qs = qs.filter(Q(name__icontains=q) | Q(slug__icontains=q) | Q(phone__icontains=q)
                           | Q(email__icontains=q))
        page = Paginator(qs.order_by("-created_at"), 25).get_page(request.GET.get("page"))
        return JsonResponse({
            "page": page.number, "pages": page.paginator.num_pages,
            "results": [{
                "id": t.pk, "name": t.name, "slug": t.slug, "type": t.business_type,
                "is_active": t.is_active, "users": t.user_count, "created_at": t.created_at,
                "status": getattr(getattr(t, "subscription", None), "status", None),
                "period_end": getattr(getattr(t, "subscription", None), "current_period_end", None),
                "has_access": t.has_access,
            } for t in page],
        })


class TenantDetailView(SuperuserRequiredMixin, View):
    def get(self, request, pk):
        t = get_object_or_404(Tenant.objects.select_related("subscription"), pk=pk)
        sub = t.subscription
        return JsonResponse({
            "tenant": {"id": t.pk, "name": t.name, "slug": t.slug, "phone": t.phone,
                       "email": t.email, "is_active": t.is_active, "has_access": t.has_access},
            "subscription": {"status": sub.status, "term_months": sub.term_months,
                             "period_end": sub.current_period_end, "comp_until": sub.comp_until,
                             "custom_monthly_price": sub.custom_monthly_price,
                             "override_note": sub.override_note},
            "invoices": list(Invoice.objects.filter(tenant=t).values(
                "number", "status", "total", "term_months", "issued_at", "paid_at")[:20]),
            "payments": list(MpesaPayment.objects.filter(tenant=t).values(
                "mpesa_receipt", "amount", "status", "created_at")[:20]),
            "events": list(BillingEvent.objects.filter(tenant=t).values("type", "payload", "created_at")[:30]),
        })


class TenantActionView(SuperuserRequiredMixin, View):
    """POST /platform/tenants/<id>/<action>/ — every action lands in BillingEvent."""

    def post(self, request, pk, action):
        t = get_object_or_404(Tenant.objects.select_related("subscription"), pk=pk)
        note = request.POST.get("note", "")
        try:
            if action == "comp":
                services.grant_complimentary_access(t, days=int(request.POST["days"]), by=request.user, note=note)
            elif action == "set-price":
                services.set_custom_price(t, monthly_price=request.POST.get("monthly_price") or None,
                                          by=request.user, note=note)
            elif action == "suspend":
                services.suspend_now(t, by=request.user, note=note)
            elif action in ("disable", "enable"):
                t.is_active = action == "enable"
                t.save(update_fields=["is_active"])
                services.record_event(f"tenant.{action}d", t, by=request.user.username, note=note)
            elif action == "support-start":
                request.session[SUPPORT_SESSION_KEY] = t.pk
                services.record_event("support.session_started", t, by=request.user.username, note=note)
            elif action == "support-stop":
                request.session.pop(SUPPORT_SESSION_KEY, None)
                services.record_event("support.session_ended", t, by=request.user.username)
            else:
                raise Http404
        except (KeyError, ValueError, ArithmeticError) as exc:
            return JsonResponse({"error": f"invalid input: {exc}"}, status=400)
        t.refresh_from_db()
        return JsonResponse({"ok": True, "has_access": t.has_access})
