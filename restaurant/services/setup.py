"""
First-time setup for a restaurant / hotel workspace, and the helpers that create menu items and
ingredients (each is a normal Product + variant underneath, so the rest of the POS keeps working).
"""
from decimal import Decimal

from django.db import transaction

from products.models import Category, Product, ProductVariant, UnitOfMeasure
from tenants.context import get_current_tenant
from tenants.models import next_number
from inventory.models import Branch

from ..models import (FloorZone, Ingredient, MenuItem, RestaurantSettings, Station, Table,
                      get_restaurant_settings)
from .errors import RestaurantError

INGREDIENT_UNITS = [("Gram", "g"), ("Kilogram", "kg"), ("Millilitre", "ml"), ("Litre", "l"),
                    ("Piece", "pcs"), ("Portion", "por"), ("Pack", "pk")]


def _slug(name):
    from django.utils.text import slugify
    return slugify(name)[:100] or "category"


def ensure_restaurant_setup(tenant=None):
    """Idempotent. Safe to call on every visit to the dashboard."""
    tenant = tenant or get_current_tenant()
    if tenant is None:
        return
    with transaction.atomic():
        get_restaurant_settings(tenant)
        if not Station.all_objects.filter(tenant=tenant).exists():
            Station.all_objects.create(tenant=tenant, name="Kitchen", color="#ea580c", sort_order=1)
            Station.all_objects.create(tenant=tenant, name="Bar", color="#0891b2", sort_order=2)
        for branch in Branch.all_objects.filter(tenant=tenant, is_active=True):
            if not FloorZone.all_objects.filter(tenant=tenant, branch=branch).exists():
                FloorZone.all_objects.create(tenant=tenant, branch=branch, name="Main hall", sort_order=1)
        for name, short in INGREDIENT_UNITS:
            if not UnitOfMeasure.all_objects.filter(tenant=tenant, name=name).exists():
                UnitOfMeasure.all_objects.create(tenant=tenant, name=name, short_name=short)
        for cat in ("Meals", "Drinks", "Extras", "Ingredients"):
            if not Category.all_objects.filter(tenant=tenant, name=cat).exists():
                Category.all_objects.create(tenant=tenant, name=cat, slug=_slug(cat))


def default_station(tenant=None):
    tenant = tenant or get_current_tenant()
    st = Station.objects.filter(is_active=True).order_by("sort_order", "id").first()
    if st is None:
        st = Station.all_objects.create(tenant=tenant, name="Kitchen", sort_order=1)
    return st


def _unit(name="Portion", short="por"):
    u = UnitOfMeasure.objects.filter(name=name).first()
    return u or UnitOfMeasure.objects.create(name=name, short_name=short)


# ------------------------------------------------------------------------------ menu items
@transaction.atomic
def create_menu_item(*, name, category, price, kind=MenuItem.Kind.MEAL, fulfilment=MenuItem.Fulfilment.RECIPE,
                     station=None, prep_minutes=10, description="", cost=Decimal("0"), unit=None,
                     low_stock_threshold=Decimal("5")):
    tenant = get_current_tenant()
    if tenant is None:
        raise RestaurantError("No active business.")
    sku = next_number(tenant, "menu_sku", "M", 5)
    product = Product.objects.create(
        name=name.strip(), sku_prefix=sku, category=category, unit_of_measure=unit or _unit(),
        description=description, has_variations=False)
    variant = ProductVariant.objects.create(
        product=product, sku=sku, cost_price=cost, retail_price=price,
        low_stock_threshold=low_stock_threshold)
    return MenuItem.objects.create(
        variant=variant, kind=kind, fulfilment=fulfilment, station=station or default_station(),
        prep_minutes=prep_minutes)


@transaction.atomic
def update_menu_item(item, *, name, category, price, kind, fulfilment, station, prep_minutes, description="",
                     is_available=True, is_active=True):
    p, v = item.variant.product, item.variant
    p.name, p.category, p.description = name.strip(), category, description
    p.save(update_fields=["name", "category", "description"])
    v.retail_price = price
    v.save(update_fields=["retail_price"])
    item.kind, item.fulfilment, item.station = kind, fulfilment, station
    item.prep_minutes, item.is_available, item.is_active = prep_minutes, is_available, is_active
    item.save()
    return item


# ------------------------------------------------------------------------------ ingredients
@transaction.atomic
def create_ingredient(*, name, unit, cost_per_unit, low_stock_threshold, recipe_unit="", units_per_base=Decimal("1")):
    tenant = get_current_tenant()
    if tenant is None:
        raise RestaurantError("No active business.")
    cat = Category.objects.filter(name="Ingredients").first() or Category.objects.create(
        name="Ingredients", slug="ingredients")
    sku = next_number(tenant, "ingredient_sku", "ING", 5)
    product = Product.objects.create(name=name.strip(), sku_prefix=sku, category=cat, unit_of_measure=unit)
    variant = ProductVariant.objects.create(
        product=product, sku=sku, cost_price=cost_per_unit, retail_price=Decimal("0"),
        low_stock_threshold=low_stock_threshold)
    return Ingredient.objects.create(variant=variant, recipe_unit=recipe_unit, units_per_base=units_per_base)


@transaction.atomic
def update_ingredient(ing, *, name, cost_per_unit, low_stock_threshold, recipe_unit, units_per_base, is_active):
    p, v = ing.variant.product, ing.variant
    p.name = name.strip()
    p.save(update_fields=["name"])
    v.cost_price, v.low_stock_threshold = cost_per_unit, low_stock_threshold
    v.save(update_fields=["cost_price", "low_stock_threshold"])
    ing.recipe_unit, ing.units_per_base, ing.is_active = recipe_unit, units_per_base, is_active
    ing.save()
    return ing
