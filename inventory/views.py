import uuid
from decimal import Decimal, InvalidOperation

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.urls import reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import ListView, CreateView, DetailView

from users.mixins import AdminRequiredMixin, AuditLogMixin, CashierRequiredMixin
from sales.models import CashRegisterSession, CashTransaction

from .models import (
    PurchaseOrder, PurchaseOrderItem, PurchasePayment, StockLevel, StockAdjustment,
)
from .forms import (
    PurchaseOrderForm, PurchaseOrderItemFormSet, StockAdjustmentForm, PurchasePaymentForm,
)


def _cash_pool_check(branch, amount):
    """
    Cash payments come out of the branch's shared cash pool, so they need
    an open register and enough cash in it. Returns (session, error).
    """
    session = CashRegisterSession.get_active_session(branch)
    if not session:
        return None, "Cannot pay cash: this branch's cash register isn't open."
    available = session.get_current_expected_cash()
    if amount > available:
        return None, f"Cannot pay KSH {amount} in cash - only KSH {available} is available in the branch cash pool."
    return session, None


def _record_payment(po, user, amount, method, reference, session):
    """
    Creates the PurchasePayment and, for CASH, the linked CASH_OUT in the
    branch cash pool. Call inside transaction.atomic().
    """
    cash_txn = None
    if method == 'CASH':
        supplier_note = f" ({po.supplier.name})" if po.supplier else ""
        cash_txn = CashTransaction.objects.create(
            session=session,
            user=user,
            transaction_type='CASH_OUT',
            amount=amount,
            reason=f"Stock purchase - PO {po.po_number}{supplier_note}",
        )
    return PurchasePayment.objects.create(
        purchase_order=po,
        amount=amount,
        payment_method=method,
        reference=reference,
        paid_by=user,
        cash_transaction=cash_txn,
    )


class StockLevelListView(CashierRequiredMixin, ListView):
    model = StockLevel
    template_name = 'inventory/stocklevel_list.html'
    context_object_name = 'stock_levels'
    paginate_by = 25
    queryset = StockLevel.objects.select_related('branch', 'variant', 'variant__product')


class PurchaseOrderListView(CashierRequiredMixin, ListView):
    model = PurchaseOrder
    template_name = 'inventory/purchase_order_list.html'
    context_object_name = 'purchase_orders'
    paginate_by = 20
    queryset = PurchaseOrder.objects.select_related('supplier', 'branch').prefetch_related('items', 'payments')


class PurchaseOrderCreateView(CashierRequiredMixin, CreateView):
    """
    Records a stock purchase as one transaction: items + costs, optional
    supplier, payment (none / partial / full) and, by default, immediate
    receipt into inventory. Cash payments are withdrawn from the branch
    cash pool and linked to the payment record.
    """
    model = PurchaseOrder
    form_class = PurchaseOrderForm
    template_name = 'inventory/purchase_order_form.html'
    success_url = reverse_lazy('purchase-order-list')

    def get_initial(self):
        # Supports the "record a purchase" link a new product now redirects
        # into (see products/views.py ProductCreateView) — ?branch=<id>
        # preselects the branch dropdown the same way StockAdjustmentCreateView
        # already does for adjustments.
        initial = super().get_initial()
        branch_id = self.request.GET.get('branch')
        if branch_id:
            initial['branch'] = branch_id
        return initial

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        if self.request.POST:
            data['items'] = PurchaseOrderItemFormSet(self.request.POST, instance=self.object)
        else:
            formset = PurchaseOrderItemFormSet(instance=self.object)
            # ?variant=<id> preselects that variant on the first (empty)
            # item row, so a brand-new product's own "record a purchase"
            # link doesn't leave the cashier hunting for it in the dropdown.
            variant_id = self.request.GET.get('variant')
            if variant_id and formset.forms:
                formset.forms[0].initial['variant'] = variant_id
            data['items'] = formset
        return data

    def form_valid(self, form):
        items = self.get_context_data()['items']
        if not items.is_valid():
            return self.form_invalid(form)

        receive_now = form.cleaned_data.get('receive_immediately')
        method = form.cleaned_data.get('payment_method') or ''
        amount_paid = form.cleaned_data.get('amount_paid') or Decimal('0.00')
        reference = (form.cleaned_data.get('payment_reference') or '').strip()
        branch = form.cleaned_data['branch']

        # Validate everything BEFORE writing anything.
        projected_total = Decimal('0.00')
        for item_form in items.forms:
            cd = item_form.cleaned_data
            if not cd or cd.get('DELETE'):
                continue
            projected_total += (cd.get('quantity_ordered') or Decimal('0')) * (cd.get('unit_cost') or Decimal('0'))

        if amount_paid > projected_total:
            form.add_error('amount_paid', f"Amount paid (KSH {amount_paid}) can't exceed the total purchase cost (KSH {projected_total}).")
            return self.form_invalid(form)

        session = None
        if method == 'CASH' and amount_paid > 0:
            session, error = _cash_pool_check(branch, amount_paid)
            if error:
                form.add_error('amount_paid', error)
                return self.form_invalid(form)

        with transaction.atomic():
            form.instance.created_by = self.request.user
            form.instance.po_number = f"PO-{timezone.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
            form.instance.status = 'RECEIVED' if receive_now else 'ORDERED'
            self.object = form.save()
            items.instance = self.object
            items.save()

            if receive_now:
                for item in self.object.items.select_related('variant'):
                    item.quantity_received = item.quantity_ordered
                    item.save(update_fields=['quantity_received'])

                    stock_level, _ = StockLevel.objects.select_for_update().get_or_create(
                        branch=self.object.branch,
                        variant=item.variant,
                        defaults={'quantity': 0},
                    )
                    stock_level.quantity += item.quantity_ordered
                    stock_level.save(update_fields=['quantity'])

            if amount_paid > 0:
                _record_payment(self.object, self.request.user, amount_paid, method, reference, session)

        messages.success(
            self.request,
            f"Purchase {self.object.po_number} recorded - {self.object.payment_status_display}"
            + (f", KSH {self.object.balance_payable} still payable." if self.object.balance_payable > 0 else ".")
        )
        return redirect(self.get_success_url())


class PurchaseOrderDetailView(AdminRequiredMixin, DetailView):
    model = PurchaseOrder
    template_name = 'inventory/purchase_order_detail.html'
    context_object_name = 'purchase_order'

    def get_queryset(self):
        return (
            super().get_queryset()
            .select_related('supplier', 'branch', 'created_by')
            .prefetch_related('items__variant', 'payments__paid_by', 'payments__cash_transaction')
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        po = self.object
        context['total_cost'] = po.total_cost
        context['total_paid'] = po.total_paid
        context['balance_payable'] = po.balance_payable
        context['payment_form'] = PurchasePaymentForm()
        return context


class ReceivePurchaseOrderView(AdminRequiredMixin, View):
    """Receive an advance order (fully or in parts), optionally paying at the same time."""

    def get(self, request, pk):
        po = get_object_or_404(PurchaseOrder.objects.prefetch_related('items__variant'), pk=pk)
        if po.status in ('RECEIVED', 'CANCELLED'):
            messages.warning(request, f"Purchase Order {po.po_number} is already {po.get_status_display()}.")
            return redirect('purchase-order-detail', pk=po.pk)
        return render(request, 'inventory/purchase_order_receive.html', {
            'po': po,
            'balance_payable': po.balance_payable,
        })

    def post(self, request, pk):
        po = get_object_or_404(PurchaseOrder.objects.select_related('supplier', 'branch'), pk=pk)
        if po.status in ('RECEIVED', 'CANCELLED'):
            messages.warning(request, f"Purchase Order {po.po_number} is already {po.get_status_display()}.")
            return redirect('purchase-order-detail', pk=po.pk)

        method = request.POST.get('payment_method', '').strip()
        reference = request.POST.get('payment_reference', '').strip()
        try:
            amount_paid = Decimal(request.POST.get('amount_paid', '').strip() or '0')
        except InvalidOperation:
            amount_paid = Decimal('0.00')

        if amount_paid < 0 or (amount_paid > 0 and method not in dict(PurchasePayment.PAYMENT_METHODS)):
            messages.error(request, "Choose a valid payment method and a positive amount.")
            return redirect('purchase-order-receive', pk=po.pk)

        if amount_paid > po.balance_payable:
            messages.error(request, f"Amount paid (KSH {amount_paid}) exceeds the outstanding balance (KSH {po.balance_payable}).")
            return redirect('purchase-order-receive', pk=po.pk)

        session = None
        if method == 'CASH' and amount_paid > 0:
            session, error = _cash_pool_check(po.branch, amount_paid)
            if error:
                messages.error(request, error)
                return redirect('purchase-order-receive', pk=po.pk)

        with transaction.atomic():
            items = list(
                PurchaseOrderItem.objects.select_for_update()
                .filter(purchase_order=po).select_related('variant')
            )
            any_received = False
            fully_received = True

            for item in items:
                raw_value = request.POST.get(f'Receive_{item.id}', '').strip()
                try:
                    receive_qty = Decimal(raw_value) if raw_value else Decimal('0')
                except InvalidOperation:
                    receive_qty = Decimal('0')

                remaining = item.quantity_ordered - item.quantity_received
                receive_qty = max(Decimal('0'), min(receive_qty, remaining))

                if receive_qty > 0:
                    any_received = True
                    item.quantity_received += receive_qty
                    item.save(update_fields=['quantity_received'])

                    stock_level, _ = StockLevel.objects.select_for_update().get_or_create(
                        branch=po.branch,
                        variant=item.variant,
                        defaults={'quantity': 0},
                    )
                    stock_level.quantity += receive_qty
                    stock_level.save(update_fields=['quantity'])

                if item.quantity_received < item.quantity_ordered:
                    fully_received = False

            if not any_received and amount_paid <= 0:
                messages.warning(request, "No quantities entered and no payment recorded - nothing was done.")
                return redirect('purchase-order-receive', pk=po.pk)

            if any_received:
                po.status = 'RECEIVED' if fully_received else 'PARTIAL'
                po.save(update_fields=['status'])

            if amount_paid > 0:
                _record_payment(po, request.user, amount_paid, method, reference, session)

        messages.success(request, f"Purchase Order {po.po_number} updated.")
        return redirect('purchase-order-detail', pk=po.pk)


class PurchasePaymentCreateView(AdminRequiredMixin, View):
    """Record a further payment against an existing purchase (settle credit / partial balance)."""

    def post(self, request, pk):
        po = get_object_or_404(PurchaseOrder.objects.select_related('supplier', 'branch'), pk=pk)

        form = PurchasePaymentForm(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect('purchase-order-detail', pk=po.pk)

        amount = form.cleaned_data['amount']
        method = form.cleaned_data['payment_method']
        reference = (form.cleaned_data.get('reference') or '').strip()

        if amount > po.balance_payable:
            messages.error(request, f"Amount (KSH {amount}) exceeds the outstanding balance (KSH {po.balance_payable}).")
            return redirect('purchase-order-detail', pk=po.pk)

        session = None
        if method == 'CASH':
            session, error = _cash_pool_check(po.branch, amount)
            if error:
                messages.error(request, error)
                return redirect('purchase-order-detail', pk=po.pk)

        with transaction.atomic():
            _record_payment(po, request.user, amount, method, reference, session)

        messages.success(request, f"Payment of KSH {amount} recorded against {po.po_number}.")
        return redirect('purchase-order-detail', pk=po.pk)


class StockAdjustmentCreateView(CashierRequiredMixin, CreateView):
    model = StockAdjustment
    form_class = StockAdjustmentForm
    template_name = 'inventory/adjustment_form.html'
    success_url = reverse_lazy('stock-level-list')

    SUBTRACTION_TYPES = {'DAMAGE', 'THEFT', 'EXPIRY'}

    def get_initial(self):
        initial = super().get_initial()
        variant_id = self.request.GET.get('variant')
        branch_id = self.request.GET.get('branch')

        if variant_id:
            initial['variant'] = variant_id
        if branch_id:
            initial['branch'] = branch_id

        return initial

    def form_valid(self, form):
        with transaction.atomic():
            adjustment = form.save(commit=False)
            adjustment.user = self.request.user

            adj_type = getattr(adjustment, 'adjustment_type', getattr(adjustment, 'reason', None))

            if adj_type in self.SUBTRACTION_TYPES:
                adjustment.quantity_changed = -abs(adjustment.quantity_changed)

            stock_level, _ = StockLevel.objects.select_for_update().get_or_create(
                branch=adjustment.branch,
                variant=adjustment.variant,
                defaults={'quantity': 0}
            )

            new_quantity = stock_level.quantity + adjustment.quantity_changed

            if new_quantity < 0:
                form.add_error(
                    'quantity_changed',
                    f'This adjustment would reduce stock below zero (current level: {stock_level.quantity}).'
                )
                return self.form_invalid(form)

            stock_level.quantity = new_quantity
            stock_level.save(update_fields=['quantity'])
            adjustment.save()

        return super().form_valid(form)