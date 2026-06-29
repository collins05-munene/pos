from django.db import models

from products.models import Product

# Create your models here.
class Supplier(models.Model):
    PAYMENT_TERMS_CHOICES = [
        ('CASH', 'Cash on Delivery'),
        ('NET15', 'Net 15 Days'),
        ('NET30', 'Net 30 Days'),
        ('NET60', 'Net 60 Days')
    ]
    company_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    payment_terms = models.CharField(max_length=10, choices=PAYMENT_TERMS_CHOICES, default='CASH')  
    tax_number = models.CharField(max_length=100, blank=True, verbose_name="Tax/VAT ID")

    notes = models.TextField(blank=True, help_text="Internal notes about reliability, delivery speeds, etc.")
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-company_name']

    def __str__(self):
        return self.company_name
    

class SupplierProduct(models.Model):
    supplier = models.ForeignKey('Supplier', on_delete=models.CASCADE, related_name='product_links')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='supplier_links')

    supplier_sku = models.CharField(max_length=100, blank=True, help_text="The SKU code this specific supplier uses.")
    lead_time_days = models.PositiveBigIntegerField(default=7, help_text="Average days it takes this supplier to deliver this item.")
    is_primary = models.BooleanField(default=False, help_text="Is this our preferred vendor for this product?")


    class Meta:
        unique_together = ('supplier', 'product')
        verbose_name = "Supplier Product Link"
        ordering = ['is_primary', 'product__name']

    def __str__(self):
        return f"{self.supplier.company_name} -> {self.product.name}"