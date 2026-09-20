
from django import forms

from .models import Category, Brand, Product,UnitOfMeasure, ProductVariant, ProductImage

class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'parent']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'parent': forms.Select(attrs={'class': 'form-control'})
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
    initial_stock = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        help_text="Initial inventory quantity for single-variant products.",
        widget=forms.NumberInput(attrs={'class': 'form-input', 'placeholder': '0'})
    )

    class Meta:
        model = Product
        fields = ['name', 'sku_prefix', 'category', 'brand', 'unit_of_measure', 'description', 'has_variations', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. Wireless Mouse'}),
            'sku_prefix': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. WM-100'}),
            'category': forms.Select(attrs={'class': 'form-select'}),
            'brand': forms.Select(attrs={'class': 'form-select'}),
            'unit_of_measure': forms.Select(attrs={'class': 'form-select'}),
            'description': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3}),
            'has_variations': forms.CheckboxInput(attrs={'class': 'form-checkbox', 'id': 'toggle-variations'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
        }


class ProductVariantForm(forms.ModelForm):
    initial_stock = forms.IntegerField(
        required=False,
        min_value=0,
        initial=0,
        help_text="Initial opening stock quantity for this variant.",
        widget=forms.NumberInput(attrs={'class': 'form-input', 'placeholder': '0'})
    )

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


ProductImageFormSet = forms.inlineformset_factory(
    Product,
    ProductImage,
    fields=['image', 'variant', 'is_primary'],
    extra=1,
    max_num=4,
    can_delete=True,
    widgets={
        'image': forms.FileInput(attrs={'class': 'form-file'}),
        'variant': forms.Select(attrs={'class': 'form-select'}),
        'is_primary': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
    }
)