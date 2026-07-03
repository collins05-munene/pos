
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
    initial_stock = forms.DecimalField(
        max_digits=12, 
        decimal_places=3, 
        required=False, 
        initial=0.000,
        help_text="Enter initial wholesale stock quantity received at the Main Branch"
    )
    class Meta:
        model = Product
        fields = ['name', 'sku_prefix', 'category', 'brand', 'unit_of_measure', 'description', 'has_variations']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'sku_prefix': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. TSHT'}),
            'category': forms.Select(attrs={'class': 'form-control'}),             
            'brand': forms.Select(attrs={'class': 'form-control'}),
            'unit_of_measure': forms.Select(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}), 
            'has_variations': forms.CheckboxInput(attrs={'class': 'form-check-input'})          
        }

ProductVariantFormSet = forms.inlineformset_factory(
    Product,
    ProductVariant,
    fields = ['sku', 'size', 'color', 'cost_price', 'retail_price', 'low_stock_threshold'],
    extra = 1,
    can_delete=True,
    widgets= {
        'sku': forms.TextInput(attrs={'class': 'form=control', 'placeholder': 'SKU...'}),
        'size': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Size...'}),
        'color': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Color...'}),
        'cost_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        'retail_price': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'}),
        'low_stock_threshold': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01'})
    }
)

ProductImageFormSet = forms.inlineformset_factory(
    Product,
    ProductImage,
    fields = ['image', 'is_primary'],
    extra=1,
    max_num=4,
    validate_max=True,
    can_delete=True,
    widgets = {
        'image': forms.FileInput(attrs={'class': 'form-control'}),
        'is_primary': forms.CheckboxInput(attrs={'class': 'form-check-input'})
    }
)