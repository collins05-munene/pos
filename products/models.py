from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from tenants.models import TenantOwnedModel


def _unique_per_tenant(model, *fields):
    """Constraint names must be unique database-wide, hence the model-qualified name."""
    return [models.UniqueConstraint(fields=["tenant", f], name=f"uniq_{model}_{f}_per_tenant")
            for f in fields]


def fmt_qty(value):
    """30.000 -> '30', 0.500 -> '0.5' (never exponent notation)."""
    return format(Decimal(value).normalize(), 'f')


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
    # Legacy: the supplier is now recorded on each purchase. Kept so old data isn't lost.
    supplier = models.ForeignKey('supplier.Supplier', on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    # The BASE unit: stock is counted in this unit (Tablet, Kilogram, Piece ...).
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
    """
    Two kinds of rows live in this table:

    * BASE variant  (base_variant is NULL, units_per_pack == 1)
        A size / colour / SKU of the product. It is the ONLY row that holds
        stock, counted in the product's unit of measure (tablets, kg ...).

    * SELLING UNIT  (base_variant -> a base variant)
        Another way of selling the same stock: its own SKU, name and price,
        but no stock of its own. One of it = `units_per_pack` base units.
        A box of 30 tablets -> 30.   500 g of rice (base unit kg) -> 0.5.
    """
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    sku = models.CharField(max_length=100)
    size = models.CharField(max_length=50, null=True, blank=True)
    color = models.CharField(max_length=50, blank=True)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2)
    retail_price = models.DecimalField(max_digits=12, decimal_places=2)
    low_stock_threshold = models.DecimalField(max_digits=12, decimal_places=3, default=10.000)
    is_active = models.BooleanField(default=True)

    unit_name = models.CharField(
        max_length=30, blank=True,
        help_text="How a selling unit is sold: Packet, Box, 500 g ... Blank on base variants.")
    base_variant = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.PROTECT, related_name='packagings',
        help_text="Set only on selling units; the base variant that holds the stock.")
    units_per_pack = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal('1'),
        help_text="How many base units one of this makes up.")

    class Meta:
        constraints = _unique_per_tenant("variant", "sku") + [
            models.CheckConstraint(condition=models.Q(units_per_pack__gt=0),
                                   name="variant_units_per_pack_gt_0"),
        ]

    # ---- helpers ---------------------------------------------------------
    @property
    def is_packaging(self):
        return self.base_variant_id is not None

    @property
    def stock_variant(self):
        """The base variant whose StockLevel this row sells from."""
        return self.base_variant if self.base_variant_id else self

    @property
    def unit_label(self):
        """Name of the unit this row is sold in."""
        return self.unit_name or self.product.unit_of_measure.name

    @property
    def base_unit_label(self):
        return self.stock_variant.unit_label

    @property
    def pack_label(self):
        return f"{self.unit_name} ×{fmt_qty(self.units_per_pack)}"

    def to_base_units(self, qty):
        return Decimal(qty) * Decimal(self.units_per_pack)

    def clean(self):
        super().clean()
        if self.base_variant_id:
            base = self.base_variant
            if self.pk and base.pk == self.pk:
                raise ValidationError("A selling unit cannot be its own base.")
            if base.base_variant_id:
                raise ValidationError("A selling unit must point at a base variant, not at another selling unit.")
            if base.product_id != self.product_id:
                raise ValidationError("The base variant must belong to the same product.")
        elif Decimal(self.units_per_pack) != 1:
            raise ValidationError("A base variant must have a pack size of 1.")

    def __str__(self):
        variant_info = f" - {self.size} " if self.size else ""
        variant_info += f" / {self.color}" if self.color else ""
        label = f"{self.product.name}{variant_info} ({self.sku})"
        if self.base_variant_id:
            label += f" [{self.pack_label}]"
        return label