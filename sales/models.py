from django.db import models
from django.contrib.auth import get_user_model

from products.models import ProductVariant
from inventory.models import Branch

User = get_user_model()

# Create your models here.
class Order(models.Model):
    PAYMENT_METHODS = (
        ('CASH', 'Cash'),
        ('MPESA', 'Mpesa Express')
    )

    invoice_number = models.CharField(max_length=50, unique=True)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='sales', null=True)
    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name='sales')
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS)
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_cogs = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Invoice {self.invoice_number}"

class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    retail_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Selling price per unit")
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Buying price per unit")
    revenue_line = models.DecimalField(max_digits=12, decimal_places=2)
    cogs_line = models.DecimalField(max_digits=12, decimal_places=2)
    profit_line = models.DecimalField(max_digits=12, decimal_places=2)

    def save(self, *args, **kwargs):
        self.revenue_line = self.retail_price * self.quantity
        self.cogs_line = self.cost_price * self.quantity
        self.profit_line = self.revenue_line - self.cogs_line
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.variant.sku} X {self.quantity}"