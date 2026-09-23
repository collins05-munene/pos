from django.shortcuts import render, redirect, get_object_or_404
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.db import transaction
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import ListView, CreateView, DetailView

from users.views import LoginRequiredMixin, AdminRequiredMixin

from .models import PurchaseOrder, PurchaseOrderItem, StockLevel, StockAdjustment
from .forms import PurchaseOrderForm, PurchaseOrderItemFormSet, StockAdjustmentForm

# Create your views here.
class StockLevelListView(LoginRequiredMixin, ListView):
    model = StockLevel
    template_name = 'inventory/stocklevel_list.html'
    context_object_name = 'stock_levels'
    paginate_by = 25
    queryset = StockLevel.objects.select_related('branch', 'variant', 'variant__product')

class PurchaseOrderListView(LoginRequiredMixin, ListView):
    model = PurchaseOrder
    template_name = 'inventory/purchase_order_list.html'
    context_object_name = 'purchase_orders'
    paginate_by = 20
    queryset = PurchaseOrder.objects.select_related('supplier', 'branch')

class PurchaseOrderCreateView(LoginRequiredMixin, CreateView):
    model = PurchaseOrder
    form_class = PurchaseOrderForm
    template_name = 'inventory/purchase_order_form.html'
    success_url = reverse_lazy('purchase-order-list')

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        if self.request.POST:
            data['items'] = PurchaseOrderItemFormSet(self.request.POST, instance=self.object)
        else:
            data['items'] = PurchaseOrderItemFormSet(instance=self.object)
        return data
    
    def form_valid(self, form):
        context = self.get_context_data()
        items = context['items']

        with transaction.atomic():
            if form.is_valid() and items.is_valid():
                form.instance.created_by = self.request.user
                self.object = form.save()
                items.instance = self.object
                items.save()
                return super().form_valid(form)
            else:
                return self.form_invalid(form)
            
class PurchaseOrderDetailView(LoginRequiredMixin, DetailView):
    model = PurchaseOrder
    template_name = 'inventory/purchase_order_detail.html'
    context_object_name = 'purchase_order'

    def get_queryset(self):
        return super().get_queryset().select_related('supplier', 'branch').prefetch_related('items__variant')
    

class ReceivePurchaseOrderView(LoginRequiredMixin, View):
    def get(self, request, pk):
        po = get_object_or_404(
            PurchaseOrder.objects.prefetch_related('items__variant'), pk=pk
        )
        if po.status in ('RECEIVED', 'CANCELLED'):
            messages.warming(request, f"Purchase Order {po.po_number} is already {po.get_status_display()}.")
            return redirect('purchase-order-detail', pk=po.pk)
        return render(request, 'inventory/purchase_order_receive.html', {'po': po})
    
    def post(self, request, pk):
        po = get_object_or_404(
            PurchaseOrder.objects.prefetch_related('items__variant'), pk=pk
        )
        if po.status in ('RECEIVED', 'CANCELLED'):
            messages.warning(request, f"Purchase Order {po.po_number} is already {po.get_status_display()}.")
            return redirect('purchase-order-detail', pk=po.pk)
    
        with transaction.atomic():
            items = list(
                PurchaseOrderItem.objects.select_for_update().filter(purchase_order=po).select_related('variant')
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
                        defaults={'quantity': 0}
                    )
                    stock_level.quantity += receive_qty
                    stock_level.save(update_fields=['quantity'])
                
                if item.quantity_received < item.quantity_ordered:
                    fully_received = False

            if not any_received:
                messages.warning(request, "No quantities entered - nothing was received.")
                return redirect('purchase-order-receive', pk=po.pk)
            
            po.status = 'RECEIVED' if fully_received else 'PARTIAL'
            po.save(update_fields=['status'])
        messages.success(request, f"Stock updated for Purchase Order {po.po_number}.")
        return redirect('purchase_order-detail', po=po.pk)
    

class StockAdjustmentCreateView(LoginRequiredMixin, CreateView):
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

            # 1. Normalize quantity for loss/waste adjustment types
            adj_type = getattr(adjustment, 'adjustment_type', getattr(adjustment, 'reason', None))
            
            if adj_type in self.SUBTRACTION_TYPES:
                adjustment.quantity_changed = -abs(adjustment.quantity_changed)

            # 2. Get or initialize stock level record
            stock_level, _ = StockLevel.objects.select_for_update().get_or_create(
                branch=adjustment.branch,
                variant=adjustment.variant,
                defaults={'quantity': 0}
            )

            # 3. Calculate target stock balance
            new_quantity = stock_level.quantity + adjustment.quantity_changed

            # 4. Prevent negative inventory balance
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