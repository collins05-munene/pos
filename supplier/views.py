from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, UpdateView, DeleteView, DetailView
from django.db import transaction

from users.views import AdminRequiredMixin
from .models import Supplier
from .forms import SupplierForm, SupplierProductFormset

# Create your views here.
class SupplierListView(AdminRequiredMixin, ListView):
    model = Supplier
    template_name = 'suppliers/supplier_list.html'
    context_object_name = 'suppliers'
    paginate_by = 10
    queryset = Supplier.objects.all()


class SupplierCreateView(AdminRequiredMixin, CreateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'supplier/supplier_form.html'
    success_url = reverse_lazy('supplier-list')
    
    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        if self.request.POST:
            data['products'] = SupplierProductFormset(self.request.POST, instance=self.object)
        else:
            data['products'] = SupplierProductFormset(instance=self.object)
        return data
    

    def form_valid(self, form):
        context = self.get_context_data()
        products = context['products']

        with transaction.atomic():
            if form.is_valid() and products.is_valid():
                self.object = form.save()
                products.instance = self.object
                products.save()
                return super().form_valid(form)
            else:
                return self.form_invalid(form)

class SupplierUpdateView(AdminRequiredMixin, UpdateView):
    model = Supplier
    form_class = SupplierForm
    template_name = 'supplier/supplier_form.html'
    success_url = reverse_lazy('supplier-list')

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)

        has_products = self.object.product_links.exists() if self.object else False

        if self.request.POST:
            data['products'] = SupplierProductFormset(self.request.POST, instance=self.object)
        else:
            product_formset = SupplierProductFormset(instance=self.object)
            if has_products:
                product_formset.extra = 0

            data['products'] = product_formset
        return data
    
    def form_valid(self, form):
        context = self.get_context_data()
        products = context['products']

        with transaction.atomic():
            if form.is_valid() and products.is_valid():
                self.object = form.save()
                products.instace = self.object
                products.save()
                return super().form_valid(form)
            else:
                return self.form_invalid(form)

class SupplierDeleteView(AdminRequiredMixin, DeleteView):
    model = Supplier
    template_name = 'supplier/supplier_confirm_delete.html'
    success_url = reverse_lazy('supplier-list')

class SupplierDetailView(AdminRequiredMixin, DetailView):
    model = Supplier
    template_name = 'supplier/supplier_detail.html'
    context_object_name = 'supplier'

    def get_queryset(self):
        return super().get_queryset().prefetch_related('product_links__product')