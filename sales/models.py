from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from decimal import Decimal
from django.db.models import Sum
import decimal

from products.models import ProductVariant
from inventory.models import Branch

User = get_user_model()

# Create your models here
class CashRegisterSession(models.Model):
    STATUS_CHOICES = (
        ('OPEN', 'Open'),
        ('CLOSED', 'Closed'),
    )

    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name='cash_sessions')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='cash_sessions', null=True, blank=True)
    
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2)
    closing_balance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    expected_closing_balance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    discrepancy = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, help_text="Closing - Expected")
    
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='OPEN')
    notes = models.TextField(blank=True, null=True)
    
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-opened_at']

    def __str__(self):
        return f"Session #{self.id} - {self.cashier} ({self.status})"

    @classmethod
    def get_active_session(cls, user):
        return cls.objects.filter(cashier=user, status='OPEN').first()

    @property
    def total_cash_sales(self):
        """Sum of all cash sales made during this specific session."""
        # Ensure that orders are properly linked via Foreign Key or Session relation
        result = self.orders.filter(
            payment_method='CASH'
        ).aggregate(total=models.Sum('total_revenue'))['total']
        return result or Decimal('0.00')

    @property
    def total_cash_in(self):
        result = self.transactions.filter(
            transaction_type='CASH_IN'
        ).aggregate(total=models.Sum('amount'))['total']
        return result or Decimal('0.00')

    @property
    def total_cash_out(self):
        result = self.transactions.filter(
            transaction_type='CASH_OUT'
        ).aggregate(total=models.Sum('amount'))['total']
        return result or Decimal('0.00')

    def get_current_expected_cash(self):
        """Calculates expected cash: Opening float + Cash Sales + Cash In - Cash Out"""
        return self.opening_balance + self.total_cash_sales + self.total_cash_in - self.total_cash_out

    def save(self, *args, **kwargs):
        # Always compute expected closing balance upon session close
        if self.status == 'CLOSED':
            self.expected_closing_balance = self.get_current_expected_cash()
            if self.closing_balance is not None:
                self.discrepancy = self.closing_balance - self.expected_closing_balance
        super().save(*args, **kwargs)


class CashTransaction(models.Model):
    TRANSACTION_TYPES = (
        ('CASH_IN', 'Cash In (Float Addition)'),
        ('CASH_OUT', 'Cash Out (Expense / Payout)'),
    )

    session = models.ForeignKey(CashRegisterSession, on_delete=models.CASCADE, related_name='transactions')
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_transaction_type_display()} - KSH {self.amount}"


class Order(models.Model):
    PAYMENT_METHODS = (
        ('CASH', 'Cash'),
        ('MPESA', 'Mpesa Express')
    )

    invoice_number = models.CharField(max_length=50, unique=True)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='sales', null=True)
    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name='sales')
    cash_session = models.ForeignKey(CashRegisterSession, on_delete=models.PROTECT, related_name='orders', null=True, blank=True)
    
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