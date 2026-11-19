from decimal import Decimal

from django import forms

from inventory.models import Branch
from products.models import Category, UnitOfMeasure

from .models import (DeliveryRider, DeliveryZone, Ingredient, MenuItem, Modifier, RecipeLine, RestaurantSettings,
                     Station, Table, WasteRecord)


class StyledMixin:
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        for f in self.fields.values():
            w = f.widget
            if isinstance(w, forms.CheckboxInput):
                continue
            w.attrs.setdefault("class", "form-input")


class MenuItemForm(StyledMixin, forms.Form):
    name = forms.CharField(max_length=255)
    category = forms.ModelChoiceField(queryset=Category.objects.none())
    price = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"))
    kind = forms.ChoiceField(choices=MenuItem.Kind.choices)
    fulfilment = forms.ChoiceField(choices=MenuItem.Fulfilment.choices, label="How is it stocked?")
    station = forms.ModelChoiceField(queryset=Station.objects.none(), required=False, label="Made at")
    prep_minutes = forms.IntegerField(min_value=0, max_value=600, initial=10, label="Prep time (minutes)")
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    cost = forms.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False,
                              label="Cost price", help_text="Only for items resold from stock. Recipe items work their cost out from the recipe.")
    is_available = forms.BooleanField(required=False, initial=True, label="Available (untick to 86 it)")
    is_active = forms.BooleanField(required=False, initial=True, label="On the menu")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["category"].queryset = Category.objects.filter(is_active=True)
        self.fields["station"].queryset = Station.objects.filter(is_active=True)

    @classmethod
    def for_item(cls, item):
        return cls(initial=dict(
            name=item.name, category=item.category_id if hasattr(item, "category_id") else item.category,
            price=item.price, kind=item.kind, fulfilment=item.fulfilment, station=item.station_id,
            prep_minutes=item.prep_minutes, description=item.variant.product.description,
            cost=item.variant.cost_price, is_available=item.is_available, is_active=item.is_active))


class IngredientForm(StyledMixin, forms.Form):
    name = forms.CharField(max_length=255)
    unit = forms.ModelChoiceField(queryset=UnitOfMeasure.objects.none(), label="Stock unit",
                                  help_text="The unit you count and buy in. Smallest unit you cook with works best (g, ml).")
    cost_per_unit = forms.DecimalField(max_digits=12, decimal_places=4, min_value=Decimal("0"), label="Cost per stock unit (KSH)")
    low_stock_threshold = forms.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0"), initial=Decimal("1000"),
                                             label="Low-stock alert at")
    recipe_unit = forms.CharField(max_length=15, required=False, label="Recipe unit (optional)",
                                  help_text="Only if recipes use a different unit, e.g. stock in kg, recipes in g.")
    units_per_base = forms.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"), initial=Decimal("1"),
                                        label="Recipe units per stock unit", help_text="Stock in kg, recipes in g -> 1000. Otherwise leave 1.")
    is_active = forms.BooleanField(required=False, initial=True)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["unit"].queryset = UnitOfMeasure.objects.filter(is_active=True)

    @classmethod
    def for_ingredient(cls, ing):
        v = ing.variant
        return cls(initial=dict(name=ing.name, unit=v.product.unit_of_measure_id, cost_per_unit=v.cost_price,
                                low_stock_threshold=v.low_stock_threshold, recipe_unit=ing.recipe_unit,
                                units_per_base=ing.units_per_base, is_active=ing.is_active))


class RecipeLineForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = RecipeLine
        fields = ["ingredient", "quantity", "waste_percent"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["ingredient"].queryset = Ingredient.objects.filter(is_active=True).select_related("variant__product")
        self.fields["quantity"].min_value = Decimal("0.001")


class StationForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Station
        fields = ["name", "color", "sort_order", "is_active"]
        widgets = {"color": forms.TextInput(attrs={"type": "color"})}


class ModifierForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Modifier
        fields = ["name", "kind", "price", "ingredient", "ingredient_qty", "menu_items", "is_active"]
        widgets = {"menu_items": forms.SelectMultiple(attrs={"size": 8})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["ingredient"].queryset = Ingredient.objects.filter(is_active=True).select_related("variant__product")
        self.fields["menu_items"].queryset = MenuItem.objects.filter(is_active=True).select_related("variant__product")

    def clean(self):
        c = super().clean()
        if c.get("kind") == Modifier.Kind.EXTRA and c.get("ingredient") and not c.get("ingredient_qty"):
            self.add_error("ingredient_qty", "How much of the ingredient does this extra use?")
        if c.get("kind") == Modifier.Kind.REMOVE and Decimal(c.get("price") or 0) != 0:
            self.add_error("price", "A removal is free.")
        return c


class RiderForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = DeliveryRider
        fields = ["name", "phone", "is_active"]


class ZoneForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = DeliveryZone
        fields = ["name", "fee", "is_active"]


class SettingsForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = RestaurantSettings
        fields = ["deduction_policy", "allow_negative_stock", "service_charge_percent", "service_charge_dine_in_only",
                  "default_delivery_fee", "kds_late_minutes", "accept_mpesa", "accept_card",
                  "online_ordering_enabled", "auto_accept_online"]

    def clean_service_charge_percent(self):
        v = self.cleaned_data["service_charge_percent"]
        if v < 0 or v > 50:
            raise forms.ValidationError("Between 0 and 50.")
        return v


class WasteForm(StyledMixin, forms.Form):
    subject = forms.ChoiceField(label="What was wasted?")
    quantity = forms.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"),
                                  help_text="Ingredient: amount in its recipe unit. Dish: number of portions.")
    reason = forms.ChoiceField(choices=WasteRecord.Reason.choices)
    notes = forms.CharField(max_length=200, required=False)

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        choices = [("", "Choose...")]
        choices += [(f"i{i.pk}", f"Ingredient: {i.name} ({i.unit_label})")
                    for i in Ingredient.objects.filter(is_active=True).select_related("variant__product")]
        choices += [(f"m{m.pk}", f"Dish: {m.name}") for m in MenuItem.objects.filter(is_active=True).select_related("variant__product")]
        self.fields["subject"].choices = choices


class TableForm(StyledMixin, forms.ModelForm):
    class Meta:
        model = Table
        fields = ["name", "seats", "shape", "zone"]
