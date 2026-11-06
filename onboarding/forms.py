from decimal import Decimal

from django import forms

from products.models import Brand, Category, UnitOfMeasure

from .services import sku_in_use


def _clean_name(value, label):
    name = " ".join((value or "").split())
    if len(name) < 2:
        raise forms.ValidationError(f"{label} is too short.")
    if len(name) > 100:
        raise forms.ValidationError(f"{label} is too long (100 characters max).")
    return name


class CategoryStepForm(forms.Form):
    name = forms.CharField(max_length=200, label="Category name",
                           widget=forms.TextInput(attrs={"placeholder": "e.g. Pain relief, Beverages, Hardware", "autofocus": True}))

    def clean_name(self):
        return _clean_name(self.cleaned_data["name"], "Category name")


class BrandStepForm(forms.Form):
    name = forms.CharField(max_length=200, label="Brand name",
                           widget=forms.TextInput(attrs={"placeholder": "e.g. Panadol, Coca-Cola, Bamburi", "autofocus": True}))
    description = forms.CharField(required=False, max_length=500, label="Description (optional)",
                                  widget=forms.TextInput())

    def clean_name(self):
        return _clean_name(self.cleaned_data["name"], "Brand name")


class ProductStepForm(forms.Form):
    name = forms.CharField(max_length=255, label="Product name",
                           widget=forms.TextInput(attrs={"placeholder": "e.g. Paracetamol 500mg", "autofocus": True}))
    category = forms.ModelChoiceField(queryset=Category.objects.none(), label="Category", empty_label="Choose a category")
    brand = forms.ModelChoiceField(queryset=Brand.objects.none(), required=False, label="Brand (optional)",
                                   empty_label="No brand")
    unit_of_measure = forms.ModelChoiceField(queryset=UnitOfMeasure.objects.none(), label="Sold by",
                                             empty_label="Choose a unit",
                                             help_text="How you count this item: piece, kilogram, tablet, litre…")
    cost_price = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"), label="Buying price (KSH)",
                                    help_text="What one unit costs you.",
                                    widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "0.00"}))
    retail_price = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"), label="Selling price (KSH)",
                                      help_text="What the customer pays for one unit.",
                                      widget=forms.NumberInput(attrs={"step": "0.01", "placeholder": "0.00"}))
    sku = forms.CharField(max_length=50, required=False, label="SKU / code (optional)",
                          help_text="Leave blank and we'll make one from the name.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Evaluated per request, inside the active shop (tenant-scoped managers).
        self.fields["category"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["brand"].queryset = Brand.objects.filter(is_active=True).order_by("name")
        self.fields["unit_of_measure"].queryset = UnitOfMeasure.objects.filter(is_active=True).order_by("name")

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())

    def clean_sku(self):
        sku = self.cleaned_data.get("sku", "").strip()
        if sku and sku_in_use(sku):
            raise forms.ValidationError("That code is already used by another product. Pick a different one or leave it blank.")
        return sku
