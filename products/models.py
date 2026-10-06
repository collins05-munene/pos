from django.db import models

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

    class Meta:
        constraints = _unique_per_tenant("variant", "sku")

    def __str__(self):
        variant_info = f" - {self.size} " if self.size else ""
        variant_info += f" / {self.color}" if self.color else ""
        return f"{self.product.name}{variant_info} ({self.sku})"
