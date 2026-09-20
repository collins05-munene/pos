from django.db import models
from django.contrib.auth import get_user_model

from supplier.models import Supplier
from products.models import ProductVariant

User = get_user_model()
# Create your models here.
class Branch(models.Model):
    name = models.CharField(max_length=100, unique=True)
    location = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "Branches"
    
    def __str__(self):
        return self.name
    
class StockLevel(models.Model):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='stock_levels')
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0.000)

    class Meta:
        unique_together = ('branch', 'variant')
        ordering = ['-branch']

    @property
    def is_low_stock(self):
        return self.quantity <= self.variant.low_stock_threshold
    

class StockAdjustment(models.Model):
    ADJUSTMENT_TYPES = (
        ('COUNT', 'Stcok Count / Audit'),
        ('DAMAGE', 'Damaged Items Written Off'),
        ('THEFT', 'Stolen Missing'),
        ('EXPIRY', 'Expired Items'),
        ('CORRECTION', 'Data Entry Correction')
    )
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='adjustments')
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE, related_name='adjustments')
    adjustment_type = models.CharField(max_length=20, choices=ADJUSTMENT_TYPES)
    quantity_changed = models.DecimalField(max_digits=12, decimal_places=3, help_text="Can be negative (for damage/loss) or positive (for corrections).")
    reason = models.TextField(blank=True, help_text="Detailed notes on why the adjustment occurred.")
    user = models.ForeignKey(User, on_delete=models.PROTECT, help_text="The staff member who logged the adjustment.")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.adjustment_type} ({self.quantity_changed}) at {self.branch.name}"


class PurchaseOrder(models.Model):
    STATUS_CHOICES = (
        ('DRAFT', 'Draft'),
        ('ORDERED', 'Ordered'),
        ('PARTIAL', 'Partially Received'),
        ('RECEIVED', 'Fully Received'),
        ('CANCELLED', 'Cancelled')
    )
    po_number = models.CharField(max_length=50, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name='purchase_orders')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='purchase_orders', help_text='Destination branch')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_pos')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.po_number} - {self.supplier.name}"
    

class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name='po_items')
    quantity_ordered = models.DecimalField(max_digits=12, decimal_places=3)
    quantity_received = models.DecimalField(max_digits=12, decimal_places=3, default=0.000)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, help_text='Cost price locked in at time of order')

    @property
    def quantity_remaining(self):
        return self.quantity_ordered - self.quantity_received

        
    def __str__(self):
        return f"{self.variant.sku} * {self.quantity_ordered}"
    
class InventoryTransfer(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending Dispatch'),
        ('IN_TRANSIT', 'In Transit'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    )
    transfer_number = models.CharField(max_length=50, unique=True)
    from_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='outgoing_transfers')
    to_branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='incoming_transfers')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    remarks = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='initiated_transfers')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Transfer {self.transfer_number}: {self.from_branch} -> {self.to_branch}"
    

class InventoryTransferItem(models.Model):
    transfer = models.ForeignKey(InventoryTransfer, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.variant.sku} * {self.quantity}"
