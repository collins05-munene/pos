from decimal import Decimal

from django import forms
from django.db.models import Q
from django.utils import timezone

from inventory.models import Branch
from products.models import Category, Product, ProductVariant
from tenants.models import get_settings

from .models import Discount
from .permissions import is_admin_user


def _local_dt(attrs=None):
    return forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control", **(attrs or {})},
                               format="%Y-%m-%dT%H:%M")


class DiscountForm(forms.ModelForm):
    class Meta:
        model = Discount
        fields = ["name", "kind", "value", "target", "products", "categories", "branches",
                  "min_quantity", "starts_at", "ends_at", "is_active", "note"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "e.g. Weekend promo"}),
            "kind": forms.Select(attrs={"class": "form-control"}),
            "value": forms.NumberInput(attrs={"class": "form-control", "step": "0.01", "min": "0.01"}),
            "target": forms.Select(attrs={"class": "form-control"}),
            "products": forms.SelectMultiple(attrs={"class": "searchable", "data-placeholder": "Search products…"}),
            "categories": forms.SelectMultiple(attrs={"class": "searchable", "data-placeholder": "Search categories…"}),
            "branches": forms.SelectMultiple(attrs={"class": "searchable", "data-placeholder": "All branches"}),
            "min_quantity": forms.NumberInput(attrs={"class": "form-control", "min": "1"}),
            "starts_at": _local_dt(),
            "ends_at": _local_dt(),
            "note": forms.TextInput(attrs={"class": "form-control", "placeholder": "Why are we doing this? (optional)"}),
        }

    def __init__(self, *args, user=None, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user, self.tenant = user, tenant
        self.shop = get_settings(tenant)
        self.fields["products"].queryset = Product.objects.filter(is_active=True).order_by("name")
        self.fields["categories"].queryset = Category.objects.filter(is_active=True).order_by("name")
        self.fields["branches"].queryset = Branch.objects.filter(is_active=True)
        for f in ("starts_at", "ends_at"):
            self.fields[f].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
        self.fields["is_active"].label = "Switched on"

    @property
    def ceiling(self):
        """The most % off this user may give."""
        if is_admin_user(self.user):
            return Decimal(self.shop.max_discount_percent)
        return min(Decimal(self.shop.manager_max_discount_percent), Decimal(self.shop.max_discount_percent))

    def _targeted_variants(self, cd):
        qs = ProductVariant.objects.filter(is_active=True, product__is_active=True)
        target = cd.get("target")
        if target == Discount.Target.PRODUCTS:
            return qs.filter(product__in=cd.get("products") or [])
        if target == Discount.Target.CATEGORIES:
            cats = cd.get("categories") or []
            return qs.filter(Q(product__category__in=cats) | Q(product__category__parent__in=cats))
        return qs

    def clean(self):
        cd = super().clean()
        kind, value, target = cd.get("kind"), cd.get("value"), cd.get("target")
        if kind is None or value is None or target is None:
            return cd

        if target == Discount.Target.PRODUCTS and not cd.get("products"):
            self.add_error("products", "Choose at least one product, or change 'Applies to'.")
        if target == Discount.Target.CATEGORIES and not cd.get("categories"):
            self.add_error("categories", "Choose at least one category, or change 'Applies to'.")
        if target != Discount.Target.PRODUCTS:
            cd["products"] = Product.objects.none()
        if target != Discount.Target.CATEGORIES:
            cd["categories"] = Category.objects.none()

        starts, ends = cd.get("starts_at"), cd.get("ends_at")
        if starts and ends and ends <= starts:
            self.add_error("ends_at", "The end must be after the start.")

        if self.errors:
            return cd

        # How big is this discount really? Percent is direct; a fixed KSH amount is judged against
        # the cheapest product it will touch, because that is where it bites hardest.
        if kind == Discount.Kind.PERCENT:
            worst = Decimal(value)
            if worst > 100:
                self.add_error("value", "A percentage cannot be more than 100.")
                return cd
        else:
            prices = list(self._targeted_variants(cd).values_list("retail_price", flat=True))
            worst = max((Decimal(value) / p * 100 for p in prices if p and p > 0), default=Decimal(0))

        if worst > self.ceiling:
            who = "You may give at most" if not is_admin_user(self.user) else "This shop's limit is"
            self.add_error("value", f"{who} {self.ceiling.normalize():f}% off a product"
                                    + ("" if kind == Discount.Kind.PERCENT else
                                       f" (this would be {worst.quantize(Decimal('0.1'))}% off the cheapest product it covers)")
                                    + ". Ask an admin, or lower the discount.")
        return cd
