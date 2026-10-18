from django.db.models import Q
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView, DetailView

from products.models import Product
from .models import Supplier
from .forms import SupplierForm
from django.urls import reverse, reverse_lazy
from users.mixins import AdminRequiredMixin, AuditLogMixin, CashierRequiredMixin, LiveSearchMixin, action_link


class SupplierListView(LiveSearchMixin, CashierRequiredMixin, ListView):
    model = Supplier
    template_name = 'suppliers/supplier_list.html'
    context_object_name = 'suppliers'
    paginate_by = 15
    search_fields = ('name', 'contact_person', 'phone', 'email')
    empty_noun = 'suppliers'
    create_url_name = 'supplier-create'

    def serialize_row(self, s):
        pk = [s.pk]
        return {'cells': [s.name, s.contact_person, s.phone, s.email],
                'actions': [action_link('Update', reverse('supplier-update', args=pk)),
                            action_link('View', reverse('supplier-detail', args=pk)),
                            action_link('Delete', reverse('supplier-delete', args=pk), 'delete-link')]}



class SupplierCreateView(AuditLogMixin, CashierRequiredMixin, CreateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'supplier/supplier_form.html'
    success_url = reverse_lazy('supplier-list')


class SupplierUpdateView(AuditLogMixin, CashierRequiredMixin, UpdateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'supplier/supplier_form.html'
    success_url = reverse_lazy('supplier-list')


class SupplierDeleteView(AuditLogMixin, AdminRequiredMixin, DeleteView):
    model = Supplier
    template_name = 'supplier/supplier_confirm_delete.html'
    success_url = reverse_lazy('supplier-list')


class SupplierDetailView(CashierRequiredMixin, DetailView):
    model = Supplier
    template_name = 'supplier/supplier_detail.html'
    context_object_name = 'supplier'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['products'] = (
            Product.objects.filter(is_active=True)
            .filter(Q(supplier=self.object) | Q(variants__po_items__purchase_order__supplier=self.object))
            .select_related('category', 'brand')
            .prefetch_related('variants')
            .distinct()
        )
        return context