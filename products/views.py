from decimal import Decimal

from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.views.generic import ListView, CreateView, DeleteView, UpdateView, DetailView
from django.db import transaction
from django.views import View
from django.utils.text import slugify
from django.contrib import messages
from django.http import JsonResponse

from users.mixins import AdminRequiredMixin, AuditLogMixin, CashierRequiredMixin
from users.utils import log_action, AuditAction

from .models import Category, Brand, ProductVariant, UnitOfMeasure, Product, fmt_qty
from .forms import (
    CategoryForm, BrandForm, UnitOfMeasureForm, ProductForm,
    ProductVariantFormSet, SellingUnitFormSet,
)
from notifications.models import Notification
from notifications.utils import notify, MANAGEMENT

_PRICE_FIELDS = (("retail_price", "retail"), ("cost_price", "cost"))

def _price_changes(variants, packs):
    """Compare submitted prices to what the forms were initialised with."""
    lines = []
    for f in variants.initial_forms:
        if not getattr(f, "cleaned_data", None) or f.cleaned_data.get("DELETE"):
            continue
        for field, label in _PRICE_FIELDS:
            if field in f.changed_data:
                lines.append(f"{f.initial.get('sku', 'variant')}: {label} "
                             f"{f.initial.get(field)} → {f.cleaned_data.get(field)}")
    for f in packs.forms:
        cd = getattr(f, "cleaned_data", None)
        if not cd or not f.initial or cd.get("DELETE"):
            continue
        for field, label in _PRICE_FIELDS:
            if field in f.changed_data:
                lines.append(f"{f.initial.get('sku', 'pack')}: {label} "
                             f"{f.initial.get(field)} → {cd.get(field)}")
    return lines


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


class ProductEditorMixin:
    template_name = 'products/product_form.html'
    form_class = ProductForm

    def base_variants_qs(self):
        if self.object is None:
            return ProductVariant.objects.none()
        return (ProductVariant.objects
                .filter(product=self.object, is_active=True, base_variant__isnull=True)
                .order_by('pk'))

    def build_variants(self, data=None):
        return ProductVariantFormSet(data, instance=self.object, prefix='variants',
                                     queryset=self.base_variants_qs())

    def pack_initial(self):
        """Existing selling units as rows. variant_index = position of the base variant in the variants formset."""
        if self.object is None:
            return []
        position = {v.pk: i for i, v in enumerate(self.base_variants_qs())}
        packs = (ProductVariant.objects
                 .filter(product=self.object, is_active=True, base_variant_id__in=list(position))
                 .order_by('base_variant_id', 'units_per_pack'))
        return [{
            'id': p.pk,
            'variant_index': position[p.base_variant_id],
            'unit_name': p.unit_name,
            'units_per_pack': fmt_qty(p.units_per_pack),
            'sku': p.sku,
            'retail_price': p.retail_price,
            'cost_price': p.cost_price,
        } for p in packs]

    def build_packs(self, data=None):
        return SellingUnitFormSet(data, prefix='packs', initial=self.pack_initial())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        data = self.request.POST if self.request.method == 'POST' else None
        if ctx.get('variants') is None:
            ctx['variants'] = self.build_variants(data)
        if ctx.get('packs') is None:
            ctx['packs'] = self.build_packs(data)
        return ctx

    def get_editing_object(self):
        raise NotImplementedError

    def post(self, request, *args, **kwargs):
        self.object = self.get_editing_object()
        form = self.get_form()
        variants = self.build_variants(request.POST)
        packs = self.build_packs(request.POST)

        form_ok = form.is_valid()
        variants_ok = variants.is_valid()
        packs_ok = packs.is_valid()
        if (form_ok and variants_ok and packs_ok
                and self.variants_ok(form, variants)
                and self.packs_ok(variants, packs)):
            return self.form_valid(form, variants, packs)
        return self.form_invalid(form, variants, packs)

    def form_invalid(self, form, variants, packs):
        return self.render_to_response(self.get_context_data(form=form, variants=variants, packs=packs))

    @staticmethod
    def live_variant_forms(variants):
        return {i: f for i, f in enumerate(variants.forms)
                if getattr(f, 'cleaned_data', None) and not f.cleaned_data.get('DELETE')}

    def variants_ok(self, form, variants):
        live = self.live_variant_forms(variants)
        if not form.cleaned_data.get('has_variations') and len(live) > 1:
            form.add_error('has_variations',
                           "More than one variant row was submitted. Tick 'multiple variants' or remove the extra rows.")
            return False
        return True

    def packs_ok(self, variants, packs):
        ok = True
        live_variants = self.live_variant_forms(variants)
        variant_skus = {f.cleaned_data['sku'].lower() for f in live_variants.values()}
        existing_ids = set()
        if self.object is not None:
            existing_ids = set(ProductVariant.objects
                               .filter(product=self.object, base_variant__isnull=False)
                               .values_list('pk', flat=True))

        for f in packs.live_forms():
            cd = f.cleaned_data
            sku, pack_id, index = cd['sku'], cd.get('id'), cd['variant_index']

            if index not in live_variants:
                f.add_error('variant_index', "Choose which variant this selling unit belongs to.")
                ok = False
            if pack_id and pack_id not in existing_ids:
                f.add_error(None, "This selling unit no longer exists. Remove the row and add it again.")
                ok = False
            if sku.lower() in variant_skus:
                f.add_error('sku', "This SKU is already used by a variant of this product.")
                ok = False
                continue
            clash = ProductVariant.objects.filter(sku__iexact=sku)
            if pack_id:
                clash = clash.exclude(pk=pack_id)
            if clash.exists():
                f.add_error('sku', "This SKU is already in use.")
                ok = False
        return ok

    def save_variants(self, variants):
        saved = variants.save(commit=False)
        for v in saved:
            v.save()
        for v in variants.deleted_objects:
            v.is_active = False
            v.save(update_fields=['is_active'])
            v.packagings.update(is_active=False)
        return saved

    def save_packs(self, variants, packs):
        existing = {p.pk: p for p in ProductVariant.objects.filter(product=self.object, base_variant__isnull=False)}
        count = 0
        for f in packs.forms:
            cd = getattr(f, 'cleaned_data', None)
            if not cd:
                continue
            pack_id = cd.get('id')
            if cd.get('DELETE'):
                pack = existing.get(pack_id)
                if pack:
                    pack.is_active = False
                    pack.save(update_fields=['is_active'])
                continue

            base = variants.forms[cd['variant_index']].instance
            pack = existing.get(pack_id) or ProductVariant(product=self.object)
            cost = cd.get('cost_price')
            if cost is None:
                cost = (base.cost_price * cd['units_per_pack']).quantize(Decimal('0.01'))

            pack.base_variant = base
            pack.unit_name = cd['unit_name']
            pack.units_per_pack = cd['units_per_pack']
            pack.sku = cd['sku']
            pack.retail_price = cd['retail_price']
            pack.cost_price = cost
            pack.is_active = True
            pack.save()
            count += 1
        return count


class ProductCreateView(ProductEditorMixin, CashierRequiredMixin, CreateView):
    """
    Product + variants + selling units in one save. Stock is never created here;
    afterwards the user lands on the purchase form with the new variants preselected.
    """
    model = Product

    def get_editing_object(self):
        return None

    def form_valid(self, form, variants, packs):
        with transaction.atomic():
            self.object = form.save(commit=False)
            self.object.save()
            form.save_m2m()

            variants.instance = self.object
            saved_variants = self.save_variants(variants)
            pack_count = self.save_packs(variants, packs)

            log_action(self.request.user, AuditAction.RECORD_CREATE,
                       f"Product created: {self.object.name} "
                       f"({len(saved_variants)} variant(s), {pack_count} selling unit(s))",
                       self.request)

        base_ids = [str(f.instance.pk) for f in self.live_variant_forms(variants).values()]
        messages.success(
            self.request,
            f"{self.object.name} saved with {len(saved_variants)} variant(s) and {pack_count} selling unit(s). "
            f"Now record the purchase to bring it into stock."
        )
        return redirect(f"{reverse('purchase-order-create')}?variants={','.join(base_ids)}")


class ProductUpdateView(ProductEditorMixin, CashierRequiredMixin, UpdateView):
    model = Product

    def get_editing_object(self):
        return self.get_object()

    def form_valid(self, form, variants, packs):
        price_lines = _price_changes(variants, packs)

        with transaction.atomic():
            self.object = form.save(commit=False)
            self.object.save()
            form.save_m2m()

            variants.instance = self.object
            self.save_variants(variants)
            pack_count = self.save_packs(variants, packs)

            log_action(self.request.user, AuditAction.RECORD_UPDATE,
                       f"Product updated: {self.object.name}", self.request)
            
        if price_lines:
            extra = f" (+{len(price_lines) - 5} more)" if len(price_lines) > 5 else ""
            notify(self.request.tenant, f"Price changed: {self.object.name}",
                f"{self.request.user.get_username()} changed: " + "; ".join(price_lines[:5]) + extra,
                level=Notification.Level.WARNING, roles=MANAGEMENT,
                exclude_user=self.request.user)

        messages.success(self.request, f"{self.object.name} updated.")
        return redirect('product-detail', pk=self.object.pk)


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