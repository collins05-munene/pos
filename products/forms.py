from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseFormSet, formset_factory
from django.urls import reverse

from .models import Category, Brand, Product, UnitOfMeasure, ProductVariant


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
        # Supplier is recorded on the purchase, not here.
        fields = ['name', 'sku_prefix', 'category', 'brand', 'unit_of_measure',
                  'description', 'has_variations', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. Paracetamol 500mg'}),
            'sku_prefix': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. PARA-500'}),
            'category': forms.Select(attrs={'class': 'searchable', 'data-placeholder': 'Search category…'}),
            'brand': forms.Select(attrs={'class': 'searchable', 'data-placeholder': 'Search brand…'}),
            'unit_of_measure': forms.Select(attrs={'class': 'searchable', 'data-placeholder': 'Search unit…'}),
            'description': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3}),
            'has_variations': forms.CheckboxInput(attrs={'class': 'form-checkbox', 'id': 'toggle-variations'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        cat = self.fields['category']
        cat.queryset = Category.objects.select_related('parent').order_by('name')
        cat.label_from_instance = lambda c: f"{c.parent.name} › {c.name}" if c.parent_id else c.name
        cat.widget.attrs['data-create-url'] = reverse('category-quick-create')

        brand = self.fields['brand']
        brand.queryset = Brand.objects.order_by('name')
        brand.widget.attrs['data-create-url'] = reverse('brand-quick-create')

        self.fields['unit_of_measure'].queryset = UnitOfMeasure.objects.order_by('name')


# --------------------------------------------------------------------------
# Base variants (size / colour / SKU) - these are what hold stock
# --------------------------------------------------------------------------
class ProductVariantForm(forms.ModelForm):
    class Meta:
        model = ProductVariant
        # is_active is not a field: it isn't rendered, so it would post as False.
        fields = ['sku', 'size', 'color', 'cost_price', 'retail_price', 'low_stock_threshold']
        widgets = {
            'sku': forms.TextInput(attrs={'class': 'form-input sku-input', 'placeholder': 'SKU'}),
            'size': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Size (optional)'}),
            'color': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Color (optional)'}),
            'cost_price': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01', 'placeholder': '0.00'}),
            'retail_price': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01', 'placeholder': '0.00'}),
            'low_stock_threshold': forms.NumberInput(attrs={'class': 'form-input', 'step': '1.000'}),
        }


# extra=0 + min_num=1: exactly one row on a new product, existing rows on edit, never zero.
ProductVariantFormSet = forms.inlineformset_factory(
    Product, ProductVariant, form=ProductVariantForm,
    extra=0, min_num=1, validate_min=True, can_delete=True,
)


# --------------------------------------------------------------------------
# Selling units (packet, box, 500 g ...) - edited on the same page as the product
# --------------------------------------------------------------------------
class SellingUnitForm(forms.Form):
    """
    One selling unit of a base variant. `variant_index` is the position of the
    base variant row in the variants formset (variants-<index>-...), which is
    stable even though the variant has no database id yet on a new product.
    """
    id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    variant_index = forms.IntegerField(
        min_value=0, label="For variant",
        widget=forms.Select(attrs={'class': 'form-select pack-variant-select'}),
    )
    unit_name = forms.CharField(
        max_length=30, label="Unit name",
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. Box'}),
    )
    units_per_pack = forms.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal('0.001'), label="Contains (base units)",
        widget=forms.NumberInput(attrs={'class': 'form-input', 'step': '0.001', 'min': '0.001', 'placeholder': 'e.g. 30'}),
    )
    sku = forms.CharField(
        max_length=100, label="SKU / barcode",
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Unique SKU'}),
    )
    retail_price = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal('0'), label="Retail price",
        widget=forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01', 'placeholder': '0.00'}),
    )
    cost_price = forms.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal('0'), required=False, label="Cost price",
        widget=forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01', 'placeholder': 'auto'}),
    )

    CONTENT_FIELDS = ('unit_name', 'sku', 'units_per_pack', 'retail_price', 'cost_price')

    def has_changed(self):
        # variant_index is auto-filled by JS, so it must not make a blank row look "filled in".
        return any(name in self.changed_data for name in self.CONTENT_FIELDS)

    def clean_unit_name(self):
        name = " ".join(self.cleaned_data['unit_name'].split())
        if not name:
            raise ValidationError("Enter a unit name.")
        return name

    def clean_sku(self):
        sku = self.cleaned_data['sku'].strip()
        if not sku:
            raise ValidationError("Enter a SKU.")
        return sku

    def clean_units_per_pack(self):
        units = self.cleaned_data['units_per_pack']
        if units == 1:
            raise ValidationError(
                "1 is the base unit itself, which is already sold from the main variant.")
        return units


class BaseSellingUnitFormSet(BaseFormSet):
    def live_forms(self):
        """Filled-in, not-deleted rows."""
        return [f for f in self.forms
                if getattr(f, 'cleaned_data', None) and not f.cleaned_data.get('DELETE')]

    def clean(self):
        if any(self.errors):
            return
        seen_sku, seen_unit = set(), set()
        for form in self.live_forms():
            cd = form.cleaned_data
            sku = cd['sku'].lower()
            if sku in seen_sku:
                raise ValidationError(f"SKU '{cd['sku']}' is used by more than one selling unit.")
            seen_sku.add(sku)
            key = (cd['variant_index'], cd['unit_name'].lower())
            if key in seen_unit:
                raise ValidationError(f"'{cd['unit_name']}' is listed twice for the same variant.")
            seen_unit.add(key)


SellingUnitFormSet = formset_factory(
    SellingUnitForm, formset=BaseSellingUnitFormSet, extra=0, can_delete=True,
)