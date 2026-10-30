"""
Platform admin -> Receipts: every subscription receipt, searchable and downloadable, plus a way to
generate the receipt for any paid invoice that doesn't have one (payments made before receipts
existed). Same pattern as the Payments page: one HTML shell plus JSON endpoints.
"""
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from billing import manual, receipts, services
from billing.models import Invoice, Receipt
from .views import SuperuserRequiredMixin

MAX_BULK = 500


def _receipt_row(r):
    return {
        "id": r.pk, "number": r.number, "invoice": r.invoice.number,
        "tenant": r.customer_name, "slug": r.tenant.slug, "amount": r.amount,
        "method": r.get_payment_method_display(), "reference": r.payment_reference,
        "paid_at": r.paid_at, "auto": r.generated_by_id is None,
        "is_void": r.is_void, "void_reason": r.void_reason,
        "verify_code": r.verify_code_display,
        "verify_url": reverse("billing:verify", args=[r.verify_code]),
        "download_url": reverse("platform_admin:receipt-download", args=[r.number]),
        "void_url": reverse("platform_admin:receipt-void", args=[r.number, "void"]),
        "restore_url": reverse("platform_admin:receipt-void", args=[r.number, "restore"]),
    }


def _missing_row(inv):
    return {"invoice": inv.number, "tenant": inv.tenant.name, "slug": inv.tenant.slug,
            "amount": inv.total, "paid_at": inv.paid_at}


def _missing_qs(q=""):
    qs = Invoice.objects.filter(status=Invoice.Status.PAID, receipt__isnull=True).select_related("tenant")
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(tenant__name__icontains=q) | Q(tenant__slug__icontains=q))
    return qs.order_by("-paid_at", "-id")


class ReceiptsPageView(SuperuserRequiredMixin, View):
    def get(self, request):
        return render(request, "platform_admin/receipts.html")


@method_decorator(never_cache, name="dispatch")
class ReceiptsListView(SuperuserRequiredMixin, View):
    def get(self, request):
        q = (request.GET.get("q") or "").strip()
        qs = Receipt.objects.select_related("tenant", "invoice")
        if q:
            qs = qs.filter(Q(number__icontains=q) | Q(customer_name__icontains=q)
                           | Q(payment_reference__icontains=q) | Q(invoice__number__icontains=q)
                           | Q(tenant__slug__icontains=q))
        page = Paginator(qs, 25).get_page(request.GET.get("page"))

        missing = _missing_qs(q)
        total = Receipt.objects.aggregate(n=Sum("amount"))["n"] or 0
        return JsonResponse({
            "summary": {"receipts": Receipt.objects.count(), "total": total,
                        "missing": _missing_qs().count()},
            "page": page.number, "pages": page.paginator.num_pages,
            "receipts": [_receipt_row(r) for r in page],
            "missing": [_missing_row(i) for i in missing[:50]],
        })


class ReceiptGenerateView(SuperuserRequiredMixin, View):
    """POST {"invoice": "INV-000012"} -> create that receipt. POST {"all": true} -> every missing one."""

    def post(self, request):
        try:
            data = manual.read_payload(request)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

        if str(data.get("all")).lower() in ("true", "1", "on"):
            made = 0
            for inv in _missing_qs()[:MAX_BULK]:
                made += self._generate(inv, request.user) is not None
            return JsonResponse({"ok": True, "generated": made, "remaining": _missing_qs().count()})

        invoice = get_object_or_404(Invoice.objects.select_related("tenant"), number=data.get("invoice") or "")
        if invoice.status != Invoice.Status.PAID:
            return JsonResponse({"error": "That invoice hasn't been paid, so it has no receipt."}, status=400)
        receipt = self._generate(invoice, request.user)
        return JsonResponse({"ok": True, "number": receipt.number,
                             "download_url": reverse("platform_admin:receipt-download", args=[receipt.number])},
                            status=201)

    @staticmethod
    def _generate(invoice, user):
        if Receipt.objects.filter(invoice=invoice).exists():
            return Receipt.objects.get(invoice=invoice)
        receipt = receipts.issue_receipt(invoice, generated_by=user)
        services.record_event("receipt.generated", invoice.tenant, by=user.username,
                              receipt=receipt.number, invoice=invoice.number)
        return receipt


class ReceiptDownloadView(SuperuserRequiredMixin, View):
    """GET /platform/receipts/<number>/download/ (add ?view=1 to open in the browser)."""

    def get(self, request, number):
        receipt = get_object_or_404(Receipt.objects.select_related("invoice", "tenant"), number=number)
        return receipts.pdf_response(receipt, inline=request.GET.get("view") == "1", request=request)


class InvoiceReceiptView(SuperuserRequiredMixin, View):
    """
    GET /platform/receipts/invoices/<invoice number>/ -> the PDF for a shop's paid invoice, creating
    the receipt first if it doesn't exist. Used by the "Receipt" link in a shop's drawer.
    """

    def get(self, request, number):
        invoice = get_object_or_404(Invoice.objects.select_related("tenant"), number=number)
        if invoice.status != Invoice.Status.PAID:
            raise Http404("This invoice has not been paid, so there is no receipt.")
        receipt = ReceiptGenerateView._generate(invoice, request.user)
        return receipts.pdf_response(receipt, inline=request.GET.get("view") == "1", request=request)


class ReceiptVoidView(SuperuserRequiredMixin, View):
    """
    POST /platform/receipts/<number>/void/    {"reason": "..."}  -> withdraw the receipt
    POST /platform/receipts/<number>/restore/                     -> undo that
    A voided receipt's public verify page says it is no longer valid, and its PDF is stamped VOID.
    The invoice and the subscription are not touched: this only withdraws the paper.
    """

    def post(self, request, number, action):
        if action not in ("void", "restore"):
            raise Http404
        try:
            data = manual.read_payload(request)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        receipt = get_object_or_404(Receipt, number=number)

        if action == "void":
            reason = (data.get("reason") or "").strip()[:255]
            if not reason:
                return JsonResponse({"error": "Give a reason, e.g. 'Payment reversed'."}, status=400)
            receipt.is_void, receipt.void_reason = True, reason
            receipt.voided_at, receipt.voided_by = timezone.now(), request.user
        else:
            receipt.is_void, receipt.void_reason = False, ""
            receipt.voided_at = receipt.voided_by = None
        receipt.save(update_fields=["is_void", "void_reason", "voided_at", "voided_by"])
        services.record_event(f"receipt.{action}ed", receipt.tenant, by=request.user.username,
                              receipt=receipt.number, reason=receipt.void_reason)
        return JsonResponse({"ok": True, "is_void": receipt.is_void})
