from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.generic import ListView, CreateView, DeleteView, UpdateView
from django.db import transaction
from django.utils.text import slugify

from users.views import AdminRequiredMixin, LoginRequiredMixin

from .models import Category, Brand, UnitOfMeasure, Product, ProductVariant
from inventory.models import Branch
from .forms import CategoryForm, BrandForm, UnitOfMeasureForm, ProductForm, ProductVariantFormSet, ProductImageFormSet

# Create your views here.
class CategoryListView(LoginRequiredMixin, ListView):
    model = Category
    template_name = 'products/category_list.html'
    context_object_name = 'categories'
    paginate_by = 20

class CategoryCreateView(AdminRequiredMixin, CreateView):
    model = Category
    form_class = CategoryForm
    template_name = 'products/category_form.html'
    success_url = reverse_lazy('category-list')

    def form_valid(self, form):
        category = form.save(commit=False)
        category.slug = slugify(category.name)
        category.save()
        return super().form_valid(form)

class CategoryUpdateView(AdminRequiredMixin, UpdateView):
    model = Category
    form_class = CategoryForm
    template_name = 'products/category_form.html'
    success_url = reverse_lazy('category-list')

class CategoryDeleteView(AdminRequiredMixin, DeleteView):
    model = Category
    template_name = 'products/category_confirm_delete.html'
    success_url = reverse_lazy('category-list')

class BrandListView(LoginRequiredMixin, ListView):
    model = Brand
    template_name = 'products/brand_list.html'
    context_object_name = 'brands'
    paginate_by = 10

class BrandCreateView(AdminRequiredMixin, CreateView):
    model = Brand
    template_name = 'products/brand_form.html'
    form_class = BrandForm
    success_url = reverse_lazy('brand-list')

class BrandUpdateView(AdminRequiredMixin, UpdateView):
    model = Brand
    template_name = 'products/brand_form.html'
    form_class = BrandForm
    success_url = reverse_lazy('brand-list')

class BrandDeleteView(AdminRequiredMixin, DeleteView):
    model = Brand
    template_name = 'products/brand_confirm_delete.html'
    success_url = reverse_lazy('brand-list')

class UoMListView(LoginRequiredMixin, ListView):
    model = UnitOfMeasure
    context_object_name = 'uoms'
    template_name = 'products/uom_list.html'
    paginate_by = 5

class UoMCreateView(AdminRequiredMixin, CreateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'products/uom_form.html'
    success_url = reverse_lazy('uom-list')
    
class UoMUpdateView(AdminRequiredMixin, UpdateView):
    model = UnitOfMeasure
    form_class = UnitOfMeasureForm
    template_name = 'products/uom_form.html'
    success_url = reverse_lazy('uom-list')

class UoMDeleteView(AdminRequiredMixin, DeleteView):
    model = UnitOfMeasure
    template_name = 'products/uom_confirm_delete.html'
    success_url = reverse_lazy('uom-list')


class ProductListView(LoginRequiredMixin, ListView):
    model = Product
    template_name = 'products/product_list.html'
    context_object_name = 'products'
    paginate_by = 15
    queryset = Product.objects.select_related('category', 'brand', 'unit_of_measure').prefetch_related('images')

class ProductCreateView(AdminRequiredMixin, CreateView):
    model = Product
    form_class = ProductForm
    template_name = 'products/product_form.html'
    success_url = reverse_lazy('product-list')

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        if self.request.POST:
            data['variants'] = ProductVariantFormSet(self.request.POST, instance=self.object)
            data['images'] = ProductImageFormSet(self.request.POST, self.request.FILES, instance=self.object)
        else:
            data['variants'] = ProductVariantFormSet(instance=self.object)
            images_formset = ProductImageFormSet(instance=self.object)
            images_formset.extra = 4
            data['images'] = images_formset
        return data
    
    def form_valid(self, form):
        context = self.get_context_data()
        variants = context['variants']
        images = context['images']

        with transaction.atomic():
            if form.is_valid() and variants.is_valid() and images.is_valid():
                # 1. Save the core product
                self.object = form.save()
                
                # 2. Save the variants and images
                variants.instance = self.object
                saved_variants = variants.save()
                
                images.instance = self.object
                images.save()

                # 3. Handle Initial Stock Assignment
                initial_qty = form.cleaned_data.get('initial_stock', 0)
                if initial_qty > 0:
                    # Fetch your main tracking branch
                    main_branch, _ = Branch.objects.get_or_create(
                        name="Main Branch",
                        defaults={"location": "Headquarters", "is_active": True}
                    )
                    
                    # Allocate this quantity across all variants created right now
                    for variant in variants.cleaned_data:
                        # Skip variants marked for deletion in the formset
                        if variant.get('DELETE'):
                            continue
                            
                        # Grab the actual database instance of the created variant
                        variant_instance = variant.get('id') or ProductVariant.objects.get(sku=variant.get('sku'))
                        
                        from inventory.models import StockLevel
                        StockLevel.objects.update_or_create(
                            branch=main_branch,
                            variant=variant_instance,
                            defaults={'quantity': initial_qty}
                        )

                return super().form_valid(form)
            else:
                return self.form_invalid(form)
            
class ProductUpdateView(LoginRequiredMixin, UpdateView):
    model = Product
    form_class = ProductForm
    template_name = 'products/product_form.html'
    success_url = reverse_lazy('product-list')

    def get_context_data(self, **kwargs):
        data = super().get_context_data(**kwargs)
        
        has_variants = self.object.variants.exists() if self.object else False

        if self.request.POST:
            data['variants'] = ProductVariantFormSet(self.request.POST, instance=self.object)
            data['images'] = ProductImageFormSet(self.request.POST, self.request.FILES, instance=self.object)
        else:
            variants_formset = ProductVariantFormSet(instance=self.object)
            if has_variants:
                variants_formset.extra = 0
            data['variants'] = variants_formset


            images_formset = ProductImageFormSet(instance=self.object)

            current_image_count = self.object.images.count() if self.object else 0
           
            images_formset.extra = max(0, 4 - current_image_count)
            data['images'] = images_formset
        return data
    
    def form_valid(self,form):
        context = self.get_context_data()
        variants = context['variants']
        images = context['images']

        with transaction.atomic():
            if form.is_valid() and variants.is_valid() and images.is_valid():
                self.object = form.save()
                variants.instance = self.object
                variants.save()
                images.instance = self.object
                images.save()
                return super().form_valid(form)
            else:
                return self.form_invalid(form)
            

class ProductDeleteView(LoginRequiredMixin,DeleteView):
    model = Product
    template_name = 'products/product_confirm_delete.html'
    success_url = reverse_lazy('product-list')


class ProductDetailView(AdminRequiredMixin, DeleteView):
    model = Product
    template_name = 'products/product_detail.html'
    context_object_name = 'product'

    def get_queryset(self):
        return super().get_queryset().prefetch_related('variants', 'images')