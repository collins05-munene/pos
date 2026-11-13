"""
Platform admin -> Payments: every Paystack payment shops have started, with a one-click
"re-check with Paystack" for the ones that look stuck (e.g. a webhook that never arrived).
Same pattern as the Overview page: one HTML shell plus JSON endpoints.
"""
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from billing import paystack, services
from billing.models import PaystackPayment
from .views import SuperuserRequiredMixin


def _row(p):
    return {
        "id": p.pk, "reference": p.reference, "tenant": p.tenant.name, "slug": p.tenant.slug,
        "invoice": p.invoice.number, "amount": p.amount, "paid_amount": p.paid_amount,
        "channel": p.channel, "status": p.status, "reason": p.gateway_response,
        "created_at": p.created_at, "completed_at": p.completed_at,
    }


class PaymentsPageView(SuperuserRequiredMixin, View):
    def get(self, request):
        return render(request, "platform_admin/payments.html")


@method_decorator(never_cache, name="dispatch")
class PaymentsListView(SuperuserRequiredMixin, View):
    def get(self, request):
        qs = PaystackPayment.objects.select_related("tenant", "invoice")
        status = request.GET.get("status", "SUCCESS")
        if status in PaystackPayment.Status.values:
            qs = qs.filter(status=status)
        if q := (request.GET.get("q") or "").strip():
            qs = qs.filter(Q(reference__icontains=q) | Q(tenant__name__icontains=q)
                           | Q(payer_email__icontains=q) | Q(invoice__number__icontains=q))
        page = Paginator(qs, 25).get_page(request.GET.get("page"))

        month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        ok = PaystackPayment.objects.filter(status=PaystackPayment.Status.SUCCESS)
        month = ok.filter(completed_at__gte=month_start).aggregate(n=Count("id"), total=Sum("paid_amount"))
        everything = ok.aggregate(total=Sum("paid_amount"))
        waiting = PaystackPayment.objects.filter(status=PaystackPayment.Status.INITIATED).count()
        return JsonResponse({
            "configured": paystack.is_configured(),
            "summary": {"month_count": month["n"], "month_total": month["total"] or Decimal("0"),
                        "all_time_total": everything["total"] or Decimal("0"),
                        "waiting_count": waiting},
            "page": page.number, "pages": page.paginator.num_pages,
            "payments": [_row(p) for p in page],
        })


class PaymentRecheckView(SuperuserRequiredMixin, View):
    """Ask Paystack what really happened to one payment and settle it if it succeeded."""

    def post(self, request, pk):
        payment = get_object_or_404(PaystackPayment, pk=pk)
        try:
            payment = services.verify_paystack_payment(payment)
        except paystack.PaystackError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        return JsonResponse({"ok": True, "status": payment.status, "reason": payment.gateway_response})
