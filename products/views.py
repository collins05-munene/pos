from decimal import Decimal

from django.shortcuts import render, redirect
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, DeleteView, UpdateView
from django.db import transaction
from django.utils.text import slugify
from django.contrib import messages

from users.views import AdminRequiredMixin, LoginRequiredMixin
from users.mixins import AuditLogMixin
from users.utils import log_action, AuditAction

from .models import Category, Brand, UnitOfMeasure, Product
from inventory.models import Branch, StockLevel
from .forms import CategoryForm, BrandForm, UnitOfMeasureForm, ProductForm, ProductVariantFormSet

# Create your views here.
class CategoryListView(LoginRequiredMixin, ListView):
    model = Category
    template_name = 'products/category_list.html'
    context_object_name = 'categories'
    paginate_by = 20

class CategoryCreateView(AuditLogMixin, AdminRequiredMixin, CreateView):
    model = Category
    form_class = CategoryForm
    template_name = 'products/category_form.html'
    success_url = reverse_lazy('category-list')

    def form_valid(self, form):
        category = form.save(commit=False)
        category.slug = slugify(category.name)
        category.save()
        
        self.object = category
        
   
        return super().form_valid(form)

   

class CategoryUpdateView(AuditLogMixin, AdminRequiredMixin, UpdateView):
    model = Category
    form_class = CategoryForm
    template_name = 'products/category_form.html'
    success_url = reverse_lazy('category-list')

class CategoryDeleteView(AuditLogMixin, AdminRequiredMixin, DeleteView):
    model = Category
    template_name = 'products/category_confirm_delete.html'
    success_url = reverse_lazy('category-list')

class BrandListView(LoginRequiredMixin, ListView):
    model = Brand
    template_name = 'products/brand_list.html'
    context_object_name = 'brands'
    paginate_by = 10

class BrandCreateView(AuditLogMixin, AdminRequiredMixin, CreateView):
    model = Brand
    template_name = 'products/brand_form.html'
    form_class = BrandForm
    success_url = reverse_lazy('brand-list')

class BrandUpdateView(AuditLogMixin, AdminRequiredMixin, UpdateView):
    model = Brand
    template_name = 'products/brand_form.html'
    form_class = BrandForm
    success_url = reverse_lazy('brand-list')

class BrandDeleteView(AuditLogMixin, AdminRequiredMixin, DeleteView):
    model = Brand
    template_name = 'products/brand_confirm_delete.html'
    success_url = reverse_lazy('brand-list')

class UoMListView(LoginRequiredMixin, ListView):
    model = UnitOfMeasure
    context_object_name = 'uoms'
    template_name = 'products/uom_list.html'
    paginate_by = 5

class UoMCreateView(AuditLogMixin, AdminRequiredMixin, CreateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'products/uom_form.html'
    success_url = reverse_lazy('uom-list')
    
class UoMUpdateView(AuditLogMixin, AdminRequiredMixin, UpdateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'products/uom_form.html'
    success_url = reverse_lazy('uom-list')

class UoMDeleteView(AuditLogMixin, AdminRequiredMixin, DeleteView):
    model = UnitOfMeasure
    template_name = 'products/uom_confirm_delete.html'
    success_url = reverse_lazy('uom-list')


class ProductListView(LoginRequiredMixin, ListView):
    model = Product
    template_name = 'products/product_list.html'
    context_object_name = 'products'
    paginate_by = 15
    queryset = Product.objects.select_related('category', 'brand', 'unit_of_measure', 'supplier')
    
class ProductCreateView(LoginRequiredMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = 'products/product_form.html'
    success_url = reverse_lazy('product-list')

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        if self.request.POST:
            data['variants'] = ProductVariantFormSet(self.request.POST, instance=self.object)
        else:
            data['variants'] = ProductVariantFormSet(instance=self.object)

        return data

    def post(self, request, *args, **kwargs):
        self.object = None
        form = self.get_form()
        context = self.get_context_data()
        variants = context['variants']

        if form.is_valid() and variants.is_valid():
            return self.form_valid(form, variants)
        return self.form_invalid(form, variants)

    def form_valid(self, form, variants):
        with transaction.atomic():
            # 1. Save main Product instance (force is_active = True)
            self.object = form.save(commit=False)
            self.object.is_active = True
            self.object.save()
            form.save_m2m()

            # 2. Save Variants with individual commit to capture returned instances
            variants.instance = self.object
            saved_variants = variants.save(commit=False)
            
            for variant_instance in saved_variants:
                variant_instance.is_active = True
                variant_instance.save()
            
            variants.save_m2m()

           

            # 4. Fetch or create default branch for initial stock allocation
            main_branch, _ = Branch.objects.get_or_create(
                name="Main Branch",
                defaults={"location": "Headquarters", "is_active": True}
            )

            has_variations = form.cleaned_data.get('has_variations', False)

            if not has_variations:
                global_initial_qty = form.cleaned_data.get('initial_stock') or 0
                for variant_instance in saved_variants:
                    StockLevel.objects.update_or_create(
                        branch=main_branch,
                        variant=variant_instance,
                        defaults={'quantity': global_initial_qty}
                    )
            else:
                for variant_form in variants.forms:
                    if variant_form.cleaned_data and not variant_form.cleaned_data.get('DELETE', False):
                        variant_instance = variant_form.instance
                        
                        if variant_instance.pk:
                            variant_qty = variant_form.cleaned_data.get('initial_stock') or 0
                            StockLevel.objects.update_or_create(
                                branch=main_branch,
                                variant=variant_instance,
                                defaults={'quantity': variant_qty}
                            )

            log_action(
                self.request.user,
                AuditAction.RECORD_CREATE,
                f"Product created: {self.object.name} ({len(saved_variants)} variant(s))",
                self.request
            )

        return redirect(self.get_success_url())

    def form_invalid(self, form, variants):
        return self.render_to_response(
            self.get_context_data(form=form, variants=variants)
        )

class ProductUpdateView(LoginRequiredMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = 'products/product_form.html'
    success_url = reverse_lazy('product-list')

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        
        ProductVariantFormSet.extra = 0

        if self.request.POST:
            data['variants'] = ProductVariantFormSet(self.request.POST, instance=self.object, prefix='variants')
        else:
            data['variants'] = ProductVariantFormSet(instance=self.object, prefix='variants')
            
        return data

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        context = self.get_context_data()
        variants = context['variants']
       

        if form.is_valid() and variants.is_valid():
            return self.form_valid(form, variants)
        return self.form_invalid(form, variants)

    def form_valid(self, form, variants):
        with transaction.atomic():
            self.object = form.save(commit=False)
            self.object.is_active = True
            self.object.save()
            form.save_m2m()

            variants.instance = self.object
            saved_variants = variants.save(commit=False)
            for variant in saved_variants:
                variant.is_active = True
                variant.save()
            variants.save_m2m()

            self.object.variants.all().update(is_active=True)


            log_action(
                self.request.user,
                AuditAction.RECORD_UPDATE,
                f"Product updated: {self.object.name}",
                self.request
            )

        return redirect(self.get_success_url())

    def form_invalid(self, form, variants):
        return self.render_to_response(
            self.get_context_data(form=form, variants=variants)
        )

        
class ProductDeleteView(LoginRequiredMixin,DeleteView):
    model = Product
    template_name = 'products/product_confirm_delete.html'
    success_url = reverse_lazy('product-list')


class ProductDetailView(AdminRequiredMixin, DeleteView):
    model = Product
    template_name = 'products/product_detail.html'
    context_object_name = 'product'

    def get_queryset(self):
        return super().get_queryset().prefetch_related('variants')