"""
Platform admin -> Payments: list the M-Pesa payments you've received (pasted from the
confirmation SMS) and settle shops' "I paid, here's my code" requests against them.
Same pattern as the Overview page: one HTML shell plus JSON endpoints.
"""
from decimal import Decimal

from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from billing import manual
from billing.models import ManualPaymentRequest, MpesaCode
from billing.services import BillingError
from .views import SuperuserRequiredMixin


def _code_row(c):
    return {
        "id": c.pk, "code": c.code, "amount": c.amount, "state": c.state,
        "sender_name": c.sender_name, "sender_phone": c.sender_phone,
        "transaction_at": c.transaction_at, "created_at": c.created_at, "used_at": c.used_at,
        "used_by": c.used_by_tenant.name if c.used_by_tenant else None,
        "invoice": c.used_invoice.number if c.used_invoice else None,
    }


def _request_row(r, listed_codes):
    return {
        "id": r.pk, "code": r.code, "tenant": r.tenant.name, "slug": r.tenant.slug,
        "phone": r.tenant.phone, "invoice": r.invoice.number, "amount_due": r.invoice.total,
        "status": r.status, "reason": r.reason, "created_at": r.created_at,
        "code_listed": r.code in listed_codes,
    }


class PaymentsPageView(SuperuserRequiredMixin, View):
    def get(self, request):
        return render(request, "platform_admin/payments.html")


@method_decorator(never_cache, name="dispatch")
class PaymentsListView(SuperuserRequiredMixin, View):
    def get(self, request):
        qs = MpesaCode.objects.select_related("used_by_tenant", "used_invoice")
        state = request.GET.get("state", "UNUSED")
        if state == "UNUSED":
            qs = qs.filter(used_at__isnull=True, is_void=False)
        elif state == "USED":
            qs = qs.filter(used_at__isnull=False)
        elif state == "VOID":
            qs = qs.filter(is_void=True)
        if q := (request.GET.get("q") or "").strip():
            qs = qs.filter(Q(code__icontains=q) | Q(sender_phone__icontains=q)
                           | Q(sender_name__icontains=q) | Q(used_by_tenant__name__icontains=q))
        page = Paginator(qs, 25).get_page(request.GET.get("page"))

        pending = list(ManualPaymentRequest.objects
                       .filter(status=ManualPaymentRequest.Status.PENDING)
                       .select_related("tenant", "invoice").order_by("created_at")[:100])
        listed = set(MpesaCode.objects.filter(code__in=[r.code for r in pending]).values_list("code", flat=True))

        unused = MpesaCode.objects.filter(used_at__isnull=True, is_void=False)
        agg = unused.aggregate(n=Count("id"), total=Sum("amount"))
        return JsonResponse({
            "summary": {"unused_count": agg["n"], "unused_total": agg["total"] or Decimal("0"),
                        "pending_count": len(pending)},
            "pending": [_request_row(r, listed) for r in pending],
            "page": page.number, "pages": page.paginator.num_pages,
            "codes": [_code_row(c) for c in page],
        })


class PaymentParseView(SuperuserRequiredMixin, View):
    """Paste the SMS -> prefilled fields. Nothing is saved."""

    def post(self, request):
        try:
            data = manual.read_payload(request)
            parsed = manual.parse_mpesa_sms(data.get("message", ""))
        except (ValueError, BillingError) as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        parsed["exists"] = MpesaCode.objects.filter(code=parsed["code"]).exists()
        return JsonResponse(parsed)


class PaymentAddView(SuperuserRequiredMixin, View):
    def post(self, request):
        try:
            data = manual.read_payload(request)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        message = (data.get("message") or "").strip()
        fields = {"code": data.get("code") or "", "amount": data.get("amount") or "",
                  "phone": data.get("phone") or "", "name": data.get("name") or "",
                  "transaction_at": None}
        if message:
            try:
                parsed = manual.parse_mpesa_sms(message)
            except BillingError:
                parsed = {}
            for key, value in parsed.items():
                if value and not fields.get(key):
                    fields[key] = value

        errors = {}
        if not fields["code"]:
            errors["code"] = "Enter the M-Pesa code, or paste the full message."
        if not fields["amount"]:
            errors["amount"] = "Enter the amount received."
        if errors:
            return JsonResponse({"errors": errors}, status=400)

        try:
            mcode, approved = manual.register_mpesa_code(
                by=request.user, code=fields["code"], amount=fields["amount"],
                phone=fields["phone"], name=fields["name"],
                transaction_at=fields["transaction_at"], raw_message=message)
        except BillingError as exc:
            field = "amount" if "amount" in str(exc).lower() else "code"
            return JsonResponse({"errors": {field: str(exc)}}, status=400)

        return JsonResponse({
            "code": _code_row(mcode),
            "approved": [{"tenant": r.tenant.name, "invoice": r.invoice.number} for r in approved],
        }, status=201)


class PaymentVoidView(SuperuserRequiredMixin, View):
    def post(self, request, pk):
        mcode = get_object_or_404(MpesaCode, pk=pk)
        try:
            manual.void_code(mcode, by=request.user)
        except BillingError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"ok": True})


class RequestActionView(SuperuserRequiredMixin, View):
    def post(self, request, pk, action):
        req = get_object_or_404(ManualPaymentRequest, pk=pk)
        try:
            if action == "approve":
                req = manual.approve_request(req, by=request.user)
            elif action == "reject":
                data = manual.read_payload(request)
                req = manual.reject_request(req, by=request.user, reason=(data.get("reason") or "")[:255])
            else:
                raise Http404
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except BillingError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        return JsonResponse({"ok": True, "status": req.status, "reason": req.reason})
