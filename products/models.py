from django.db import models

# Create your models here.
class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subcategories')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-name']
        verbose_name_plural = "Categories"
    
    def __str__(self):
        return self.name
    

class Brand(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ['-name']

    def __str__(self):
        return self.name
    

class UnitOfMeasure(models.Model):
    name = models.CharField(max_length=100, unique=True)
    short_name = models.CharField(max_length=10, unique=True)

    def __str__(self):
        return f"{self.name} ({self.short_name})"


class Product(models.Model):
    name = models.CharField(max_length=255)
    sku_prefix = models.CharField(max_length=50, unique=True, help_text="Base SKU prefix for this product line")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='products')
    brand = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    unit_of_measure = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT,  verbose_name="Unit of Measure")
    description = models.TextField(blank=True)
    has_variations = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
    def __str__(self):
        return self.name
    

class ProductVariant(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    sku = models.CharField(max_length=100, unique=True)
    size = models.CharField(max_length=50, null=True, blank=True)
    color = models.CharField(max_length=50, blank=True)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2)
    retail_price = models.DecimalField(max_digits=12, decimal_places=2)
    low_stock_threshold = models.DecimalField(max_digits=12, decimal_places=3, default=10.000)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        variant_info = f" - {self.size} " if self.size else ""
        variant_info += f" / {self.color}" if self.color else ""
        return f"{self.product.name}{variant_info} ({self.sku})"
    
class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, null=True, blank=True, help_text="Optional: linkimage to specific variant color")
    image = models.ImageField(upload_to='products/')
    is_primary = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)