from decimal import Decimal

from django import forms
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
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. Wireless Mouse'}),
            'sku_prefix': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. WM-100'}),
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


class ProductVariantForm(forms.ModelForm):
    """Identity and pricing of a BASE variant. Stock enters via purchases; packs are added separately."""

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


class PackagingForm(forms.ModelForm):
    """A selling unit (packet, box…) of a base variant. It shares the base variant's stock."""

    units_per_pack = forms.DecimalField(
        max_digits=12, decimal_places=3, min_value=Decimal('0.001'),
        label="Base units in one pack",
        help_text="e.g. 30 if one box holds 30 tablets.",
        widget=forms.NumberInput(attrs={'class': 'form-input', 'step': '0.001'}),
    )

    class Meta:
        model = ProductVariant
        fields = ['unit_name', 'sku', 'units_per_pack', 'retail_price', 'cost_price']
        labels = {'unit_name': 'Unit name', 'sku': 'SKU'}
        widgets = {
            'unit_name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. Box'}),
            'sku': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Unique SKU / barcode'}),
            'retail_price': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01'}),
            'cost_price': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01'}),
        }

    def __init__(self, *args, base_variant, **kwargs):
        super().__init__(*args, **kwargs)
        self.base = base_variant
        self.instance.product = base_variant.product
        self.instance.base_variant = base_variant
        self.fields['unit_name'].required = True
        self.fields['cost_price'].required = False
        self.fields['cost_price'].help_text = "Leave blank to use base cost × pack size."

    def clean_sku(self):
        sku = self.cleaned_data['sku'].strip()
        clash = ProductVariant.objects.filter(sku__iexact=sku).exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("This SKU is already in use.")
        return sku

    def clean_units_per_pack(self):
        units = self.cleaned_data['units_per_pack']
        if units == 1:
            raise forms.ValidationError(
                "1 is the base unit itself, which is already sold from the main variant.")
        return units

    def clean(self):
        cd = super().clean()
        units = cd.get('units_per_pack')
        if units and not cd.get('cost_price'):
            cd['cost_price'] = (self.base.cost_price * units).quantize(Decimal('0.01'))
        return cd