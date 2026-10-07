from django.db import models
from decimal import Decimal
from django.core.exceptions import ValidationError

from tenants.models import TenantOwnedModel


def _unique_per_tenant(model, *fields):
    """Constraint names must be unique database-wide, hence the model-qualified name."""
    return [models.UniqueConstraint(fields=["tenant", f], name=f"uniq_{model}_{f}_per_tenant")
            for f in fields]


class Category(TenantOwnedModel):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subcategories')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = "Categories"
        constraints = _unique_per_tenant("category", "name", "slug")

    def __str__(self):
        return self.name


class Brand(TenantOwnedModel):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ['name']
        constraints = _unique_per_tenant("brand", "name")

    def __str__(self):
        return self.name


class UnitOfMeasure(TenantOwnedModel):
    name = models.CharField(max_length=100)
    short_name = models.CharField(max_length=10)

    class Meta:
        ordering = ['name']
        constraints = _unique_per_tenant("uom", "name", "short_name")

    def __str__(self):
        return f"{self.name} ({self.short_name})"


class Product(TenantOwnedModel):
    name = models.CharField(max_length=255)
    sku_prefix = models.CharField(max_length=50, help_text="Base SKU prefix for this product line")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='products')
    brand = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    supplier = models.ForeignKey('supplier.Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    unit_of_measure = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, verbose_name="Unit of Measure")
    description = models.TextField(blank=True)
    has_variations = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = _unique_per_tenant("product", "sku_prefix")

    def __str__(self):
        return self.name


class ProductVariant(TenantOwnedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    sku = models.CharField(max_length=100)
    size = models.CharField(max_length=50, null=True, blank=True)
    color = models.CharField(max_length=50, blank=True)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2)
    retail_price = models.DecimalField(max_digits=12, decimal_places=2)
    low_stock_threshold = models.DecimalField(max_digits=12, decimal_places=3, default=10.000)
    is_active = models.BooleanField(default=True)

    unit_name = models.CharField(max_length=30, blank=True, help_text="How it's sold: Tablet, Packet, Box")
    base_variant = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT,
                                     related_name='packagings',
                                     help_text="Set only for packs; points at the unit that holds the stock.")
    units_per_pack = models.DecimalField(max_digits=12, decimal_places=3, default=1)

    class Meta:
        constraints = _unique_per_tenant("variant", "sku") + [
            models.CheckConstraint(condition=models.Q(units_per_pack__gt=0), name="variant_units_per_pack_gt_0"),
        ]

    @property
    def stock_variant(self):
        return self.base_variant if self.base_variant_id else self

    def to_base_units(self, qty):
        return Decimal(qty) * self.units_per_pack

    def clean(self):
        super().clean()
        if self.base_variant_id:
            b = self.base_variant
            if b.pk == self.pk or b.base_variant_id:
                raise ValidationError("A pack must point to a base unit, not to another pack.")
            if b.product_id != self.product_id:
                raise ValidationError("Base unit must belong to the same product.")
        elif self.pk and self.units_per_pack != 1:
            raise ValidationError("A base unit must have a pack size of 1.")
    class Meta:
        constraints = _unique_per_tenant("variant", "sku")

    def __str__(self):
        variant_info = f" - {self.size} " if self.size else ""
        variant_info += f" / {self.color}" if self.color else ""
        if self.base_variant_id:
            variant_info = f" [{self.unit_name} x{self.units_per_pack.normalize()}]"
        return f"{self.product.name}{variant_info} ({self.sku})"
       
