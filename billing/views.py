import json
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django_ratelimit.decorators import ratelimit

from tenants.mixins import OwnerRequiredMixin
from tenants.models import Tenant
from tenants.rendering import html_or_json

from datetime import timedelta

from django.utils import timezone

from . import conf, paystack, receipts, services
from .models import BillingCredit, Invoice, PaystackPayment, Receipt
from .pricing import quote_table
from .utils import read_payload

logger = logging.getLogger(__name__)


def _overview_url(**params):
    base = reverse("billing:billing-overview")
    return f"{base}?{urlencode(params)}" if params else base


def _callback_url(request):
    """Where Paystack sends the owner back to. BILLING_CALLBACK_BASE_URL overrides the host (ngrok in dev)."""
    path = reverse("billing:paystack-callback")
    base = (getattr(settings, "BILLING_CALLBACK_BASE_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else request.build_absolute_uri(path)


@method_decorator(never_cache, name="dispatch")
class BillingOverviewView(OwnerRequiredMixin, View):
    def get(self, request):
        tenant = request.tenant
        sub = tenant.subscription
        open_invoice = Invoice.objects.filter(tenant=tenant, status=Invoice.Status.OPEN).first()
        balance = services.credit_balance(tenant)
        due = services.amount_due(open_invoice) if open_invoice else None
        payload = {
            "status": sub.status,
            "has_access": tenant.has_access,
            "term_months": sub.term_months,
            "current_period_end": sub.current_period_end,
            "cancel_at_period_end": sub.cancel_at_period_end,
            "open_invoice": open_invoice and {"number": open_invoice.number,
                                              "total": open_invoice.total,
                                              "amount_due": due,
                                              "credit_applied": open_invoice.total - due,
                                              "term_months": open_invoice.term_months},
            "access_deadline": sub.access_deadline(),
            "plans": quote_table(
                include_install_fee=not Invoice.objects.filter(
                    tenant=tenant, status=Invoice.Status.PAID, install_fee_amount__gt=0).exists(),
                monthly_price=sub.custom_monthly_price,
                extra_branches=services.billable_branches(tenant) - 1),
            "branch_slots": sub.branch_slots,
            "invoices": list(Invoice.objects.filter(tenant=tenant).values(
                "number", "status", "total", "term_months", "issued_at", "paid_at", "period_end")[:20]),
            "credit_balance": balance,
            "credit_entries": list(BillingCredit.objects.filter(tenant=tenant).values(
                "id", "kind", "amount", "note", "created_at", "invoice__number",
                "paystack_payment__reference", "mpesa_code__code")[:8]),
            "paystack_enabled": paystack.is_configured(),
            "pending_payment": bool(PaystackPayment.objects.filter(
                tenant=tenant, status=PaystackPayment.Status.INITIATED,
                created_at__gte=timezone.now() - timedelta(hours=2)).exists()),
        }
        return html_or_json(request, "billing/overview.html", payload)


@method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True), name="post")
class CheckoutView(OwnerRequiredMixin, View):
    """
    POST term_months -> a Paystack checkout for what the shop owes. Reply: {authorization_url}
    to send the browser to. If the shop's credit already covers the term, the invoice is paid
    straight away and the reply says so (no Paystack involved).
    """

    def post(self, request):
        tenant = request.tenant
        try:
            data = read_payload(request)
            term = data.get("term_months")
            if term in (None, ""):
                open_invoice = Invoice.objects.filter(tenant=tenant, status=Invoice.Status.OPEN).first()
                term = open_invoice.term_months if open_invoice else tenant.subscription.term_months
            invoice = services.issue_invoice(tenant, int(term))

            if invoice.status == Invoice.Status.PAID:           # credit alone covered it
                return JsonResponse({
                    "status": "PAID", "invoice": invoice.number,
                    "receipt_url": reverse("billing:invoice-receipt", args=[invoice.number])})

            if not paystack.is_configured():
                return JsonResponse({"error": "Online payment isn't set up yet. Please contact support.",
                                     "code": "paystack_unconfigured"}, status=503)
            email = (tenant.email or getattr(request.user, "email", "") or "").strip()
            payment = services.initiate_paystack_payment(
                invoice, email=email, callback_url=_callback_url(request))
        except (ValueError, services.BillingError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({
            "status": "REDIRECT", "authorization_url": payment.authorization_url,
            "reference": payment.reference, "invoice": invoice.number, "amount": payment.amount,
        }, status=201)


def _outcome(payment):
    if payment.status == PaystackPayment.Status.SUCCESS:
        return "success"
    if payment.status in (PaystackPayment.Status.FAILED, PaystackPayment.Status.CANCELLED):
        return "failed"
    return "pending"


class PaystackCallbackView(OwnerRequiredMixin, View):
    """
    Paystack sends the owner's browser here after checkout (?reference=...). The query string is
    never trusted: we ask Paystack for the real result, then bounce back to the billing page.
    """

    def get(self, request):
        reference = (request.GET.get("reference") or request.GET.get("trxref") or "").strip()
        payment = PaystackPayment.objects.filter(reference=reference, tenant=request.tenant).first() \
            if reference else None
        if payment is None:
            return HttpResponseRedirect(_overview_url(paystack="unknown"))
        try:
            payment = services.verify_paystack_payment(payment)
        except paystack.PaystackError:
            return HttpResponseRedirect(_overview_url(paystack="pending", ref=payment.reference))
        return HttpResponseRedirect(_overview_url(paystack=_outcome(payment), ref=payment.reference))


@method_decorator(ratelimit(key="user", rate="30/m", method="GET", block=True), name="get")
class PaymentStatusView(OwnerRequiredMixin, View):
    """The billing page polls this for a few seconds after returning from Paystack."""

    def get(self, request, reference):
        payment = get_object_or_404(PaystackPayment.objects.select_related("invoice"),
                                    reference=reference, tenant=request.tenant)
        if payment.status == PaystackPayment.Status.INITIATED:
            try:
                payment = services.verify_paystack_payment(payment)
            except paystack.PaystackError:
                pass                                          # still waiting; the page asks again
        receipt_url = None
        if payment.status == PaystackPayment.Status.SUCCESS:
            invoice = Invoice.objects.filter(pk=payment.invoice_id, status=Invoice.Status.PAID).first()
            if invoice:
                receipt_url = reverse("billing:invoice-receipt", args=[invoice.number])
        fresh = Tenant.objects.select_related("subscription").get(pk=request.tenant.pk)   # settling may have just activated it
        return JsonResponse({"status": payment.status, "outcome": _outcome(payment),
                             "reason": payment.gateway_response,
                             "has_access": fresh.has_access, "receipt_url": receipt_url})


class InvoiceReceiptView(OwnerRequiredMixin, View):
    """
    GET /billing/invoices/<number>/receipt/ -> the receipt PDF as a download
    (add ?view=1 to open it in the browser instead). Only the shop's own paid invoices.
    Receipts for invoices paid before this feature existed are created on first request.
    """

    def get(self, request, number):
        invoice = get_object_or_404(Invoice.objects.select_related("tenant"),
                                    number=number, tenant=request.tenant)
        if invoice.status != Invoice.Status.PAID:
            raise Http404("This invoice has not been paid, so there is no receipt.")
        receipt = receipts.issue_receipt(invoice)
        return receipts.pdf_response(receipt, inline=request.GET.get("view") == "1", request=request)


@method_decorator(csrf_exempt, name="dispatch")
class PaystackWebhookView(View):
    """
    Paystack's server-to-server notification (set this URL in the Paystack dashboard).
    Authenticated by the x-paystack-signature header: HMAC-SHA512 of the raw body with our
    secret key. Anything unsigned or wrongly signed is refused before it is even parsed.
    """

    def post(self, request):
        if not paystack.valid_signature(request.body, request.headers.get("x-paystack-signature", "")):
            return HttpResponse(status=401)
        try:
            event = json.loads(request.body)
        except ValueError:
            return HttpResponse(status=400)
        if isinstance(event, dict) and event.get("event") == "charge.success":
            # An error here returns 500 on purpose: Paystack retries, so a payment is never lost.
            services.settle_paystack_transaction(event.get("data") or {}, source="webhook")
        return HttpResponse(status=200)


def locked_view(request):
    is_owner = request.user.is_authenticated and getattr(request.user, "is_owner", False)
    resp = render(request, "billing/locked.html", {"is_owner": is_owner})
    resp.status_code = 402
    return resp

@method_decorator(never_cache, name="dispatch")
@method_decorator(ratelimit(key="ip", rate="30/m", method="GET", block=True), name="get")
class VerifyReceiptView(View):
    """
    PUBLIC (no login): where the QR code / verification code on a receipt leads. Reads the live
    record, so a forged or altered PDF can't pass: the page shows what is really on file.
    Deliberately shows little: no phone, e-mail or M-Pesa code, and the shop name is partly masked.
    """

    def get(self, request, code):
        clean = "".join(ch for ch in (code or "").upper() if ch.isalnum())
        receipt = (Receipt.objects.select_related("invoice").filter(verify_code=clean).first()
                   if len(clean) == 12 else None)
        ctx = {"code": code, "found": receipt is not None}
        if receipt:
            ctx.update(
                receipt=receipt, shop=receipts.mask_name(receipt.customer_name),
                term_months=receipt.invoice.term_months, currency=conf.get("CURRENCY"),
                company=conf.get("COMPANY_NAME"))
        else:
            ctx["company"] = conf.get("COMPANY_NAME")
        return render(request, "billing/verify.html", ctx, status=200 if receipt else 404)
