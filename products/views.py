from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse, reverse_lazy
from django.views.generic import ListView, CreateView, DeleteView, UpdateView, DetailView
from django.db import transaction
from django.views import View
from django.utils.text import slugify
from django.contrib import messages
from django.http import JsonResponse, Http404


from users.mixins import AdminRequiredMixin, AuditLogMixin, CashierRequiredMixin
from users.utils import log_action, AuditAction

from .models import Category, Brand, ProductVariant, UnitOfMeasure, Product
from .forms import (
    CategoryForm, BrandForm, UnitOfMeasureForm, ProductForm,
    ProductVariantFormSet, PackagingForm,
)


class _QuickCreateView(CashierRequiredMixin, View):
    model = None
    label = ""

    def build(self, name):
        return self.model.objects.create(name=name)

    def post(self, request):
        name = " ".join(request.POST.get("name", "").split())
        if len(name) < 2:
            return JsonResponse({"error": "Name is too short."}, status=400)
        if len(name) > 100:
            return JsonResponse({"error": "Name is too long (100 characters max)."}, status=400)

        existing = self.model.objects.filter(name__iexact=name).first()
        if existing:
            return JsonResponse({"id": existing.pk, "name": existing.name})

        obj = self.build(name)
        log_action(request.user, AuditAction.RECORD_CREATE,
                   f"{self.label} created inline: {obj.name}", request)
        return JsonResponse({"id": obj.pk, "name": obj.name}, status=201)


class CategoryListView(CashierRequiredMixin, ListView):
    model = Category
    template_name = 'products/category_list.html'
    context_object_name = 'categories'
    paginate_by = 20


class CategoryQuickCreateView(_QuickCreateView):
    model = Category
    label = "Category"

    def build(self, name):
        base = slugify(name) or "category"
        slug, i = base, 2
        while Category.objects.filter(slug=slug).exists():
            slug = f"{base}-{i}"
            i += 1
        return Category.objects.create(name=name, slug=slug)


class CategoryUpdateView(AuditLogMixin, CashierRequiredMixin, UpdateView):
    model = Category
    form_class = CategoryForm
    template_name = 'products/category_form.html'
    success_url = reverse_lazy('category-list')


class CategoryDeleteView(AuditLogMixin, AdminRequiredMixin, DeleteView):
    model = Category
    template_name = 'products/category_confirm_delete.html'
    success_url = reverse_lazy('category-list')


class CategoryCreateView(AuditLogMixin, CashierRequiredMixin, CreateView):
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


class BrandCreateView(AuditLogMixin, CashierRequiredMixin, CreateView):
    model = Brand
    template_name = 'products/brand_form.html'
    form_class = BrandForm
    success_url = reverse_lazy('brand-list')


class BrandListView(CashierRequiredMixin, ListView):
    model = Brand
    template_name = 'products/brand_list.html'
    context_object_name = 'brands'
    paginate_by = 10


class BrandQuickCreateView(_QuickCreateView):
    model = Brand
    label = "Brand"


class BrandUpdateView(AuditLogMixin, CashierRequiredMixin, UpdateView):
    model = Brand
    template_name = 'products/brand_form.html'
    form_class = BrandForm
    success_url = reverse_lazy('brand-list')


class BrandDeleteView(AuditLogMixin, AdminRequiredMixin, DeleteView):
    model = Brand
    template_name = 'products/brand_confirm_delete.html'
    success_url = reverse_lazy('brand-list')


class UoMListView(CashierRequiredMixin, ListView):
    model = UnitOfMeasure
    context_object_name = 'uoms'
    template_name = 'products/uom_list.html'
    paginate_by = 5


class UoMCreateView(AuditLogMixin, CashierRequiredMixin, CreateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'products/uom_form.html'
    success_url = reverse_lazy('uom-list')


class UoMUpdateView(AuditLogMixin, CashierRequiredMixin, UpdateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'products/uom_form.html'
    success_url = reverse_lazy('uom-list')


class UoMDeleteView(AuditLogMixin, AdminRequiredMixin, DeleteView):
    model = UnitOfMeasure
    template_name = 'products/uom_confirm_delete.html'
    success_url = reverse_lazy('uom-list')


class ProductListView(CashierRequiredMixin, ListView):
    model = Product
    template_name = 'products/product_list.html'
    context_object_name = 'products'
    paginate_by = 15
    queryset = Product.objects.select_related('category', 'brand', 'unit_of_measure', 'supplier')


# --------------------------------------------------------------------------
# Product create / update (the mixin MUST come before the views that use it)
# --------------------------------------------------------------------------
class VariantRulesMixin:
    def variants_ok(self, form, variants):
        live = [f for f in variants.forms if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        if not form.cleaned_data.get('has_variations') and len(live) > 1:
            form.add_error('has_variations',
                "More than one variant row was submitted. Tick 'multiple variants' or remove the extra rows.")
            return False
        return True

    def save_variants(self, variants):
        saved = variants.save(commit=False)
        for v in saved:
            v.save()
        # Removed rows are deactivated, not deleted: purchases, stock and sales reference them.
        for v in variants.deleted_objects:
            v.is_active = False
            v.save(update_fields=['is_active'])
            v.packagings.update(is_active=False)
        return saved


class ProductCreateView(VariantRulesMixin, CashierRequiredMixin, CreateView):
    """
    Creates the catalog entry (product + variants) only. Stock enters via a purchase
    or opening stock. Single-variant products go straight to "record a purchase".
    """
    model = Product
    form_class = ProductForm
    template_name = 'products/product_form.html'
    success_url = reverse_lazy('product-list')

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        if self.request.POST:
            data['variants'] = ProductVariantFormSet(self.request.POST, instance=self.object, prefix='variants')
        else:
            data['variants'] = ProductVariantFormSet(instance=self.object, prefix='variants')
        return data

    def post(self, request, *args, **kwargs):
        self.object = None
        form = self.get_form()
        variants = self.get_context_data()['variants']

        form_ok, variants_ok = form.is_valid(), variants.is_valid()
        if form_ok and variants_ok and self.variants_ok(form, variants):
            return self.form_valid(form, variants)
        return self.form_invalid(form, variants)

    def form_valid(self, form, variants):
        with transaction.atomic():
            self.object = form.save(commit=False)
            self.object.is_active = True
            self.object.save()
            form.save_m2m()

            variants.instance = self.object
            saved_variants = self.save_variants(variants)

            log_action(self.request.user, AuditAction.RECORD_CREATE,
                       f"Product created: {self.object.name} ({len(saved_variants)} variant(s))",
                       self.request)

        messages.success(
            self.request,
            f"{self.object.name} created. It has no stock yet — record a purchase to bring it into inventory."
        )

        if len(saved_variants) == 1:
            return redirect(f"{reverse('purchase-order-create')}?variant={saved_variants[0].pk}")
        return redirect(self.get_success_url())

    def form_invalid(self, form, variants):
        return self.render_to_response(self.get_context_data(form=form, variants=variants))


class ProductUpdateView(VariantRulesMixin, CashierRequiredMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = 'products/product_form.html'
    success_url = reverse_lazy('product-list')

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        # Only live base variants are edited here; packs are managed on the product page.
        qs = ProductVariant.objects.filter(is_active=True, base_variant__isnull=True)
        if self.request.POST:
            data['variants'] = ProductVariantFormSet(self.request.POST, instance=self.object,
                                                     prefix='variants', queryset=qs)
        else:
            data['variants'] = ProductVariantFormSet(instance=self.object, prefix='variants', queryset=qs)
        return data

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        variants = self.get_context_data()['variants']

        form_ok, variants_ok = form.is_valid(), variants.is_valid()
        if form_ok and variants_ok and self.variants_ok(form, variants):
            return self.form_valid(form, variants)
        return self.form_invalid(form, variants)

    def form_valid(self, form, variants):
        with transaction.atomic():
            self.object = form.save(commit=False)
            self.object.is_active = True
            self.object.save()
            form.save_m2m()

            variants.instance = self.object
            self.save_variants(variants)

            log_action(self.request.user, AuditAction.RECORD_UPDATE,
                       f"Product updated: {self.object.name}", self.request)
        return redirect(self.get_success_url())

    def form_invalid(self, form, variants):
        return self.render_to_response(self.get_context_data(form=form, variants=variants))


class ProductDeleteView(AdminRequiredMixin, DeleteView):
    model = Product
    template_name = 'products/product_confirm_delete.html'
    success_url = reverse_lazy('product-list')


class ProductDetailView(CashierRequiredMixin, DetailView):
    model = Product
    template_name = 'products/product_detail.html'
    context_object_name = 'product'

    def get_queryset(self):
        return super().get_queryset().prefetch_related('variants__packagings')


# --------------------------------------------------------------------------
# Selling units (packet / box ...) of a base variant
# --------------------------------------------------------------------------
class PackagingSaveView(CashierRequiredMixin, View):
    """Add (pk = base variant) or edit (pk = existing pack) a selling unit."""
    creating = False
    template_name = 'products/packaging_form.html'

    def _load(self, pk):
        obj = get_object_or_404(ProductVariant.objects.select_related('product', 'base_variant'), pk=pk)
        if self.creating:
            if obj.base_variant_id:
                raise Http404
            return obj, None
        if not obj.base_variant_id:
            raise Http404
        return obj.base_variant, obj

    def get(self, request, pk):
        base, inst = self._load(pk)
        return render(request, self.template_name,
                      {'form': PackagingForm(instance=inst, base_variant=base), 'base': base})

    def post(self, request, pk):
        base, inst = self._load(pk)
        form = PackagingForm(request.POST, instance=inst, base_variant=base)
        if form.is_valid():
            pack = form.save(commit=False)
            pack.product, pack.base_variant, pack.is_active = base.product, base, True
            pack.save()
            log_action(request.user,
                       AuditAction.RECORD_UPDATE if inst else AuditAction.RECORD_CREATE,
                       f"Selling unit {pack.unit_name} x{pack.units_per_pack} for {base.sku}", request)
            messages.success(request, f"{pack.unit_name} saved.")
            return redirect('product-detail', pk=base.product_id)
        return render(request, self.template_name, {'form': form, 'base': base})


class PackagingDeactivateView(CashierRequiredMixin, View):
    def post(self, request, pk):
        pack = get_object_or_404(ProductVariant, pk=pk, base_variant__isnull=False)
        pack.is_active = False
        pack.save(update_fields=['is_active'])
        messages.success(request, f"{pack.unit_name} removed from sale.")
        return redirect('product-detail', pk=pack.product_id)