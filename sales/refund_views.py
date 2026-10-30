"""
sales/refund_views.py

    GET        /pos/refunds/                      refunds list + "find a sale to refund"
    GET/POST   /pos/refunds/new/<receipt_id>/     choose items, reason, restock or write off
    GET        /pos/refunds/<id>/                 printable refund (credit note)

Management only (ADMIN / MANAGER): refunds move cash out of the drawer, so cashiers cannot make them.
"""
import uuid
from datetime import datetime, time
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import DecimalField, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views import View

from notifications.models import Notification
from notifications.utils import MANAGEMENT, notify, who
from users.mixins import AdminRequiredMixin
from users.utils import log_action

from .models import Receipt, Refund
from .refunds import RefundError, process_refund, refund_totals, refund_window_days, refundable_lines

# Reasons where the goods usually cannot go back on the shelf: the form pre-ticks "write off".
NO_RESTOCK_REASONS = ('DEFECTIVE', 'EXPIRED')


class RefundListView(AdminRequiredMixin, View):
    page_size = 20

    def get(self, request):
        qs = Refund.objects.select_related('order', 'processed_by')

        q = request.GET.get('q', '').strip()
        if q:
            qs = qs.filter(Q(refund_number__icontains=q) | Q(order__invoice_number__icontains=q)
                           | Q(order__receipt__receipt_number__icontains=q))
        tz = timezone.get_current_timezone()
        date_from = parse_date(request.GET.get('from', '') or '')
        date_to = parse_date(request.GET.get('to', '') or '')
        if date_from:
            qs = qs.filter(created_at__gte=timezone.make_aware(datetime.combine(date_from, time.min), tz))
        if date_to:
            qs = qs.filter(created_at__lte=timezone.make_aware(datetime.combine(date_to, time.max), tz))

        total = qs.aggregate(t=Sum('total_amount'))['t'] or 0
        page_obj = Paginator(qs, self.page_size).get_page(request.GET.get('page'))
        params = request.GET.copy()
        params.pop('page', None)
        params.pop('find', None)

        find = request.GET.get('find', '').strip()
        found = []
        if find:
            found = list(
                Receipt.objects.select_related('order')
                .filter(Q(receipt_number__icontains=find) | Q(order__invoice_number__icontains=find))
                .annotate(refunded=Coalesce(Sum('order__refunds__total_amount'), Decimal('0.00'), output_field=DecimalField(max_digits=12, decimal_places=2)))
                .order_by('-issued_at')[:10])

        return render(request, 'sales/refund_list.html', {
            'page_obj': page_obj, 'q': q, 'find': find, 'found': found, 'total_refunded': total,
            'date_from': request.GET.get('from', ''), 'date_to': request.GET.get('to', ''),
            'qs_without_page': params.urlencode(), 'window_days': refund_window_days(request.tenant),
        })


class RefundCreateView(AdminRequiredMixin, View):
    template_name = 'sales/refund_form.html'

    def _context(self, receipt, posted=None):
        order = receipt.order
        lines = refundable_lines(order)
        for line in lines:
            pk = line['item'].pk
            line['qty_value'] = (posted or {}).get(f'qty_{pk}', '')
            line['restock_value'] = (f'restock_{pk}' in posted) if posted else True
        return {
            'receipt': receipt, 'order': order, 'lines': lines,
            'reasons': Refund.REASONS, 'no_restock_reasons': ','.join(NO_RESTOCK_REASONS),
            'checkout_uuid': (posted or {}).get('client_uuid') or uuid.uuid4(),
            'reason_value': (posted or {}).get('reason', ''),
            'notes_value': (posted or {}).get('notes', ''),
            'nothing_left': all(l['refundable'] == 0 for l in lines),
            'refunds': order.refunds.select_related('processed_by'),
            'window_days': refund_window_days(order.tenant),
        }

    def get(self, request, receipt_pk):
        receipt = get_object_or_404(Receipt.objects.select_related('order'), pk=receipt_pk)
        return render(request, self.template_name, self._context(receipt))

    def post(self, request, receipt_pk):
        receipt = get_object_or_404(Receipt.objects.select_related('order'), pk=receipt_pk)
        order = receipt.order
        try:
            client_uuid = uuid.UUID(request.POST.get('client_uuid', ''))
        except ValueError:
            client_uuid = None

        lines = [{
            'item_id': l['item'].pk,
            'quantity': request.POST.get(f"qty_{l['item'].pk}") or 0,
            'restock': f"restock_{l['item'].pk}" in request.POST,
        } for l in refundable_lines(order)]

        try:
            refund, created = process_refund(
                order, request.user, lines, request.POST.get('reason', ''),
                request.POST.get('notes', ''), client_uuid)
        except RefundError as exc:
            messages.error(request, str(exc))
            return render(request, self.template_name, self._context(receipt, request.POST), status=400)

        if created:
            log_action(request.user, 'REFUND_PROCESSED',
                       f"{refund.refund_number}: KSH {refund.total_amount} refunded on {order.invoice_number} "
                       f"({refund.get_reason_display()})", request)
            notify(request.tenant, 'Refund processed',
                   f"{who(request.user)} refunded KSH {refund.total_amount} on {order.invoice_number} "
                   f"({refund.get_reason_display()}).",
                   level=Notification.Level.INFO, roles=MANAGEMENT, exclude_user=request.user)
            messages.success(request, f"Refund {refund.refund_number} recorded. Give the customer KSH {refund.total_amount}.")
        else:
            messages.info(request, "This refund was already recorded.")
        return redirect(f"{reverse('refund-detail', args=[refund.pk])}?new=1")


class RefundDetailView(AdminRequiredMixin, View):
    def get(self, request, pk):
        refund = get_object_or_404(
            Refund.objects.select_related('order', 'order__receipt', 'processed_by'), pk=pk)
        return render(request, 'sales/refund_detail.html', {
            'refund': refund, 'items': refund.items.all(), 'just_made': request.GET.get('new') == '1',
        })
