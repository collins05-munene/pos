from django import forms
from django.urls import reverse

from .models import Category, Brand, Product, UnitOfMeasure, ProductVariant
from supplier.models import Supplier


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'parent']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'parent': forms.Select(attrs={'class': 'searchable', 'data-placeholder': 'Search parent category…'})
        }


class BrandForm(forms.ModelForm):
    class Meta:
        model = Brand
        fields = ['name', 'description']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 3})
        }


class UnitOfMeasureForm(forms.ModelForm):
    class Meta:
        model = UnitOfMeasure
        fields = ['name', 'short_name']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Kilograms'}),
            'short_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. kg'})
        }


class ProductForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = ['name', 'sku_prefix', 'category', 'brand', 'unit_of_measure', 'description', 'has_variations', 'is_active', 'supplier']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. Wireless Mouse'}),
            'sku_prefix': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. WM-100'}),
            'category': forms.Select(attrs={'class': 'searchable', 'data-placeholder': 'Search category…'}),
            'brand': forms.Select(attrs={'class': 'searchable', 'data-placeholder': 'Search brand…'}),
            'supplier': forms.Select(attrs={'class': 'searchable', 'data-placeholder': 'Search supplier…'}),
            'unit_of_measure': forms.Select(attrs={'class': 'searchable', 'data-placeholder': 'Search unit…'}),
            'description': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3}),
            'has_variations': forms.CheckboxInput(attrs={'class': 'form-checkbox', 'id': 'toggle-variations'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['supplier'].queryset = Supplier.objects.filter(is_active=True).order_by('name')
        self.fields['supplier'].required = False
        self.fields['supplier'].empty_label = "Select Supplier (Optional)"

        cat = self.fields['category']
        cat.queryset = Category.objects.select_related('parent').order_by('name')
        cat.label_from_instance = lambda c: f"{c.parent.name} › {c.name}" if c.parent_id else c.name
        cat.widget.attrs['data-create-url'] = reverse('category-quick-create')

        brand = self.fields['brand']
        brand.queryset = Brand.objects.order_by('name')
        brand.widget.attrs['data-create-url'] = reverse('brand-quick-create')

        self.fields['unit_of_measure'].queryset = UnitOfMeasure.objects.order_by('name')


class ProductVariantForm(forms.ModelForm):
    """Defines a variant's identity and pricing only — no initial_stock (see ProductForm)."""

    class Meta:
        model = ProductVariant
        fields = ['sku', 'size', 'color', 'cost_price', 'retail_price', 'low_stock_threshold', 'is_active']
        widgets = {
            'sku': forms.TextInput(attrs={'class': 'form-input sku-input', 'placeholder': 'SKU'}),
            'size': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Size (optional)'}),
            'color': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Color (optional)'}),
            'cost_price': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01', 'placeholder': '0.00'}),
            'retail_price': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01', 'placeholder': '0.00'}),
            'low_stock_threshold': forms.NumberInput(attrs={'class': 'form-input', 'step': '1.000'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
        }


ProductVariantFormSet = forms.inlineformset_factory(
    Product,
    ProductVariant,
    form=ProductVariantForm,
    extra=1,
    can_delete=True
)