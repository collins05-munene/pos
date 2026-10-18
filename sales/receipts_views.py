"""
sales/receipt_views.py  (import these in sales/urls.py)
"""
from datetime import datetime, time

from django.core.paginator import Paginator
from django.db.models import F, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View
from django.views.decorators.http import require_POST

from users.mixins import CashierRequiredMixin

from .models import Receipt


def _visible_receipts(user):
    """Admins/managers see every receipt in the shop; cashiers only the ones they issued."""
    qs = Receipt.objects.select_related('order', 'branch')
    if user.is_admin or user.is_manager:
        return qs
    return qs.filter(cashier=user)


class ReceiptListView(CashierRequiredMixin, View):
    page_size = 20

    def get(self, request):
        qs = _visible_receipts(request.user)

        q = request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(receipt_number__icontains=q) | Q(order__invoice_number__icontains=q))

        date_from = parse_date(request.GET.get('from', '') or '')
        date_to = parse_date(request.GET.get('to', '') or '')
        tz = timezone.get_current_timezone()
        if date_from:
            qs = qs.filter(issued_at__gte=timezone.make_aware(datetime.combine(date_from, time.min), tz))
        if date_to:
            qs = qs.filter(issued_at__lte=timezone.make_aware(datetime.combine(date_to, time.max), tz))

        page_obj = Paginator(qs, self.page_size).get_page(request.GET.get('page'))

        params = request.GET.copy()
        params.pop('page', None)

        return render(request, 'sales/receipt_list.html', {
            'page_obj': page_obj,
            'q': q,
            'date_from': request.GET.get('from', ''),
            'date_to': request.GET.get('to', ''),
            'qs_without_page': params.urlencode(),
        })


class ReceiptDetailView(CashierRequiredMixin, View):
    def get(self, request, pk):
        receipt = get_object_or_404(_visible_receipts(request.user), pk=pk)
        return render(request, 'sales/receipt_detail.html', {
            'receipt': receipt,
            'is_reprint': receipt.print_count > 0,
            'just_sold': request.GET.get('new') == '1',
        })


@require_POST
def receipt_printed(request, pk):
    """Called by the receipt page after the browser print dialog closes."""
    if not request.user.is_authenticated:
        return JsonResponse({'ok': False}, status=401)
    receipt = get_object_or_404(_visible_receipts(request.user), pk=pk)
    Receipt.objects.filter(pk=receipt.pk).update(
        print_count=F('print_count') + 1, last_printed_at=timezone.now())
    return JsonResponse({'ok': True})