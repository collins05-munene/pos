import hmac
import json

from django.conf import settings
from django.db.models import Q
from django.http import Http404, JsonResponse
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

from . import conf, manual, receipts, services
from .models import BillingCredit, Invoice, ManualPaymentRequest, MpesaPayment, Receipt
from .mpesa import MpesaError
from .pricing import quote_table


class BillingOverviewView(OwnerRequiredMixin, View):
    def get(self, request):
        tenant = request.tenant
        sub = tenant.subscription
        open_invoice = Invoice.objects.filter(tenant=tenant, status=Invoice.Status.OPEN).first()
        balance = services.credit_balance(tenant)
        due = services.amount_due(open_invoice) if open_invoice else None
        # Only attempts that still need the shop's attention: anything waiting for us, plus
        # recent rejections. Part payments live in the open invoice's amount_due, and approved
        # ones need no follow-up, so neither is listed (their old messages went stale).
        recent = timezone.now() - timedelta(hours=24)
        attempts = ManualPaymentRequest.objects.filter(tenant=tenant).filter(
            Q(status=ManualPaymentRequest.Status.PENDING)
            | Q(status=ManualPaymentRequest.Status.REJECTED, created_at__gte=recent))
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
                "mpesa_code__code")[:8]),
            "stk_enabled": conf.get("STK_PUSH_ENABLED"),
            "stk_message": conf.get("STK_PUSH_MESSAGE"),
            "manual_pay": {
                "enabled": conf.get("MANUAL_PAYMENTS_ENABLED"),
                "label": conf.get("MANUAL_PAY_LABEL"),
                "number": conf.get("MANUAL_PAY_NUMBER"),
                "name": conf.get("MANUAL_PAY_NAME"),
            },
            "manual_requests": list(attempts.values(
                "id", "code", "status", "reason", "created_at", "invoice__number")[:5]),
        }
        return html_or_json(request, "billing/overview.html", payload)


@method_decorator(ratelimit(key="user", rate="6/m", method="POST", block=True), name="post")
class CheckoutView(OwnerRequiredMixin, View):
    """POST term_months + phone -> M-Pesa prompt on the owner's phone."""

    def post(self, request):
        tenant = request.tenant
        if not conf.get("STK_PUSH_ENABLED"):
            # Checked first so a blocked attempt doesn't issue or void any invoices.
            return JsonResponse({"error": conf.get("STK_PUSH_MESSAGE"), "code": "stk_unavailable"},
                                status=400)
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


@method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True), name="post")
class ManualPaymentView(OwnerRequiredMixin, View):
    """POST code (+ optional term_months) -> approve against the codes listed in platform admin."""

    def post(self, request):
        tenant = request.tenant
        try:
            data = manual.read_payload(request)
            term = data.get("term_months")
            if term in (None, ""):
                open_invoice = Invoice.objects.filter(tenant=tenant, status=Invoice.Status.OPEN).first()
                term = open_invoice.term_months if open_invoice else tenant.subscription.term_months
            invoice = services.issue_invoice(tenant, int(term))
            if invoice.status == Invoice.Status.PAID:          # credit alone covered it
                req = None
            else:
                req = manual.submit_manual_payment(tenant, user=request.user, invoice=invoice,
                                                   raw_code=data.get("code", ""))
        except (ValueError, services.BillingError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        fresh = Tenant.objects.select_related("subscription").get(pk=tenant.pk)
        invoice.refresh_from_db()
        status = req.status if req else ManualPaymentRequest.Status.APPROVED
        reason = req.reason if req else "Paid from your credit."

        receipt_url = None
        if status == ManualPaymentRequest.Status.APPROVED:
            paid = req.invoice if req else invoice        # settling may have swapped in the current invoice
            paid.refresh_from_db()
            if paid.status == Invoice.Status.PAID:
                receipt_url = reverse("billing:invoice-receipt", args=[paid.number])
        return JsonResponse({
            "status": status, "reason": reason, "invoice": invoice.number,
            "has_access": fresh.has_access, "period_end": fresh.subscription.current_period_end,
            "credit_balance": services.credit_balance(tenant),
            "amount_due": services.amount_due(invoice),
            "receipt_url": receipt_url,
        }, status=422 if status == ManualPaymentRequest.Status.REJECTED else 200)


class PaymentStatusView(OwnerRequiredMixin, View):
    """Front-end polls this every ~3s after the STK prompt."""

    def get(self, request, pk):
        payment = get_object_or_404(MpesaPayment.objects.select_related("invoice"),
                                    pk=pk, tenant=request.tenant)
        receipt_url = None
        if payment.status == MpesaPayment.Status.SUCCESS and payment.invoice.status == Invoice.Status.PAID:
            receipt_url = reverse("billing:invoice-receipt", args=[payment.invoice.number])
        return JsonResponse({"status": payment.status, "result_desc": payment.result_desc,
                             "has_access": request.tenant.has_access, "receipt_url": receipt_url})


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
class MpesaCallbackView(View):
    """
    Daraja's STK callbacks are unsigned, so we defend in layers: secret query token,
    exact CheckoutRequestID match against a payment WE initiated, amount check,
    and idempotency.
    """

    def post(self, request):
        token = request.GET.get("token", "")
        if not hmac.compare_digest(token, settings.MPESA_BILLING_CALLBACK_SECRET):
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
