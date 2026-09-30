import hmac
import json

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django_ratelimit.decorators import ratelimit

from tenants.mixins import OwnerRequiredMixin
from tenants.rendering import html_or_json

from . import services
from .models import Invoice, MpesaPayment
from .mpesa import MpesaError
from .pricing import quote_table


class BillingOverviewView(OwnerRequiredMixin, View):
    def get(self, request):
        tenant = request.tenant
        sub = tenant.subscription
        open_invoice = Invoice.objects.filter(tenant=tenant, status=Invoice.Status.OPEN).first()
        payload = {
            "status": sub.status,
            "has_access": tenant.has_access,
            "term_months": sub.term_months,
            "current_period_end": sub.current_period_end,
            "cancel_at_period_end": sub.cancel_at_period_end,
            "open_invoice": open_invoice and {"number": open_invoice.number,
                                              "total": open_invoice.total,
                                              "term_months": open_invoice.term_months},
            "plans": quote_table(
                include_install_fee=not Invoice.objects.filter(
                    tenant=tenant, status=Invoice.Status.PAID, install_fee_amount__gt=0).exists(),
                monthly_price=sub.custom_monthly_price),
            "invoices": list(Invoice.objects.filter(tenant=tenant).values(
                "number", "status", "total", "term_months", "issued_at", "paid_at", "period_end")[:20]),
        }
        return html_or_json(request, "billing/overview.html", payload)


@method_decorator(ratelimit(key="user", rate="6/m", method="POST", block=True), name="post")
class CheckoutView(OwnerRequiredMixin, View):
    """POST term_months + phone -> M-Pesa prompt on the owner's phone."""

    def post(self, request):
        tenant = request.tenant
        try:
            term = int(request.POST.get("term_months") or tenant.subscription.term_months)
            invoice = services.issue_invoice(tenant, term)
            payment = services.initiate_stk_payment(invoice, request.POST.get("phone", ""))
        except (ValueError, services.BillingError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except MpesaError as exc:
            return JsonResponse({"error": str(exc)}, status=502)
        return JsonResponse({"payment_id": payment.pk, "invoice": invoice.number,
                             "amount": invoice.total, "status": payment.status}, status=201)


class PaymentStatusView(OwnerRequiredMixin, View):
    """Front-end polls this every ~3s after the STK prompt."""

    def get(self, request, pk):
        payment = get_object_or_404(MpesaPayment, pk=pk, tenant=request.tenant)
        return JsonResponse({"status": payment.status, "result_desc": payment.result_desc,
                             "has_access": request.tenant.has_access})


@method_decorator(csrf_exempt, name="dispatch")
class MpesaCallbackView(View):
    """
    Daraja's STK callbacks are unsigned, so we defend in layers: secret path
    segment, exact CheckoutRequestID match against a payment WE initiated,
    amount check, idempotency. (Optionally also allow-list Safaricom IPs at the proxy.)
    """

    def post(self, request, secret):
        if not hmac.compare_digest(secret, settings.MPESA_BILLING_CALLBACK_SECRET):
            raise Http404
        try:
            payload = json.loads(request.body)
        except ValueError:
            payload = {}
        services.handle_stk_callback(payload)
        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})  # always ack


def locked_view(request):
    is_owner = request.user.is_authenticated and getattr(request.user, "is_owner", False)
    resp = render(request, "billing/locked.html", {"is_owner": is_owner})
    resp.status_code = 402
    return resp