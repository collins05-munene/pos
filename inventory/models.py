from decimal import Decimal

from django.db import models
from django.db.models import Sum, F, ExpressionWrapper, DecimalField
from django.contrib.auth import get_user_model
from django.db.models.constraints import UniqueConstraint

from supplier.models import Supplier
from products.models import ProductVariant
from tenants.models import TenantOwnedModel

User = get_user_model()


class Branch(TenantOwnedModel):
    name = models.CharField(max_length=100)
    location = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "Branches"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "name"], 
                name="unique_tenant_branch_name"
            )
        ]

    def __str__(self):
        return self.name


class StockLevel(TenantOwnedModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name='stock_levels')
    variant = models.ForeignKey(ProductVariant, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=0.000)

    class Meta:
        unique_together = ('branch', 'variant')
        ordering = ['-branch']
        
    def breakdown(self):
        """Stock split from the biggest unit down: [('Box', 9), ('Packet', 2), ('Tablet', 8)]"""
        base_name = self.variant.unit_name or self.variant.product.unit_of_measure.short_name
        units = [(p.unit_name, p.units_per_pack) for p in self.variant.packagings.all() if p.is_active]
        units.append((base_name, Decimal('1')))
        units.sort(key=lambda u: u[1], reverse=True)

        remaining, parts = self.quantity, []
        for name, size in units:
            count, remaining = divmod(remaining, size)
            if count:
                parts.append((name, count))
        return parts

    @property
    def is_low_stock(self):
        return self.quantity <= self.variant.low_stock_threshold


class StockAdjustment(TenantOwnedModel):
    ADJUSTMENT_TYPES = (
        ('OPENING', 'Opening Stock (Initial Setup)'), 
        ('COUNT', 'Stock Count / Audit'),
        ('DAMAGE', 'Damaged Items Written Off'),
        ('THEFT', 'Stolen / Missing'),
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


class PurchaseOrder(TenantOwnedModel):
    """
    A stock-procurement transaction: what was purchased, from whom (if
    anyone — supplier is optional since goods are often bought from a
    market, an individual, or another informal source), at what cost,
    and how it was paid for.

    By default (see PurchaseOrderCreateView), creating one of these
    immediately increases inventory — it represents "we just bought
    this stock" rather than a formal advance order. The DRAFT/ORDERED
    statuses remain available for the less common case of ordering
    ahead and receiving later via ReceivePurchaseOrderView.

    Payment is tracked separately via the related PurchasePayment
    ledger (see below) rather than a single field here, so a purchase
    can be paid in full immediately, left entirely on credit, or paid
    off in installments over time — total_cost, total_paid,
    balance_payable and payment_status below are always derived from
    that ledger plus the line items, never duplicated/cached here.
    """
    STATUS_CHOICES = (
        ('DRAFT', 'Draft'),
        ('ORDERED', 'Ordered'),
        ('PARTIAL', 'Partially Received'),
        ('RECEIVED', 'Fully Received'),
        ('CANCELLED', 'Cancelled')
    )
    po_number = models.CharField(max_length=50)
    supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='purchase_orders',
        help_text="Optional — leave blank for market/individual/other informal purchases."
    )
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='purchase_orders', help_text='Destination branch')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='DRAFT')
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_pos')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "po_number"], 
                name="unique_tenant_po_number"
            )
        ]
        ordering = ['-created_at']


    def __str__(self):
        return f"{self.po_number} - {self.supplier.name if self.supplier else 'No supplier'}"

    @property
    def total_cost(self):
        """Total purchase cost: sum of quantity_ordered * unit_cost across line items."""
        result = self.items.aggregate(
            total=Sum(
                ExpressionWrapper(
                    F('quantity_ordered') * F('unit_cost'),
                    output_field=DecimalField(max_digits=14, decimal_places=2)
                )
            )
        )['total']
        return result or Decimal('0.00')

    @property
    def total_paid(self):
        """Sum of every PurchasePayment recorded against this purchase, regardless of method."""
        result = self.payments.aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')

    @property
    def balance_payable(self):
        """What's still owed — the supplier liability (or self-liability for informal purchases)."""
        return self.total_cost - self.total_paid

    @property
    def payment_status(self):
        paid = self.total_paid
        total = self.total_cost
        if paid <= Decimal('0.00'):
            return 'UNPAID'
        if paid >= total:
            return 'PAID'
        return 'PARTIAL'

    PAYMENT_STATUS_LABELS = {
        'UNPAID': 'Unpaid (Credit)',
        'PARTIAL': 'Partially Paid',
        'PAID': 'Fully Paid',
    }
    @property
    def payment_status_display(self):
        return self.PAYMENT_STATUS_LABELS[self.payment_status]

        
class PurchaseOrderItem(TenantOwnedModel):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, related_name='po_items')
    quantity_ordered = models.DecimalField(max_digits=12, decimal_places=3)
    quantity_received = models.DecimalField(max_digits=12, decimal_places=3, default=0.000)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, help_text='Cost price locked in at time of order')

    @property
    def quantity_remaining(self):
        return self.quantity_ordered - self.quantity_received

    @property
    def line_cost(self):
        """Cost commitment for this line: quantity_ordered * unit_cost."""
        return self.quantity_ordered * self.unit_cost

    def __str__(self):
        return f"{self.variant.sku} * {self.quantity_ordered}"


class PurchasePayment(TenantOwnedModel):
    """
    One payment event against a PurchaseOrder. Multiple rows per order
    are expected and normal — a credit purchase settled in installments
    is just several of these over time; a purchase paid in full at
    receipt is exactly one.

    A CASH payment is linked to the CashTransaction it created in the
    branch's shared cash pool (see inventory/views.py), giving a direct,
    queryable trail from "money left the till" to "this specific stock
    purchase" — rather than a generic, uncategorized cash withdrawal.
    BANK and MPESA payments don't touch the till, so cash_transaction
    stays null for those.

    Referencing 'sales.CashTransaction' as a lazy string (instead of
    importing the class) avoids a circular import: sales/models.py
    already imports Branch from this module, so this module can't
    import back from sales/models.py at module load time. The actual
    CashTransaction class is only imported where instances are
    created — inventory/views.py, which nothing in sales imports.
    """
    PAYMENT_METHODS = (
        ('CASH', 'Cash'),
        ('BANK', 'Bank Transfer'),
        ('MPESA', 'M-Pesa'),
    )

    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS)
    reference = models.CharField(
        max_length=100, blank=True,
        help_text="Bank/M-Pesa transaction code or receipt number, if applicable."
    )
    paid_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='purchase_payments')
    cash_transaction = models.OneToOneField(
        'sales.CashTransaction', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='purchase_payment',
        help_text="The branch cash-pool withdrawal this payment created, if paid in cash."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"KSH {self.amount} ({self.get_payment_method_display()}) - {self.purchase_order.po_number}"


class InventoryTransfer(TenantOwnedModel):
    STATUS_CHOICES = (
        ('PENDING', 'Pending Dispatch'),
        ('IN_TRANSIT', 'In Transit'),
        ('COMPLETED', 'Completed'),
        ('CANCELLED', 'Cancelled'),
    )
    transfer_number = models.CharField(max_length=50)
    from_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='outgoing_transfers')
    to_branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='incoming_transfers')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    remarks = models.TextField(blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='initiated_transfers')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "transfer_number"], 
                name="unique_tenant_transfer_number"
            )
        ]
        
    def __str__(self):
        return f"Transfer {self.transfer_number}: {self.from_branch} -> {self.to_branch}"


class InventoryTransferItem(TenantOwnedModel):
    transfer = models.ForeignKey(InventoryTransfer, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()

    def __str__(self):
        return f"{self.variant.sku} * {self.quantity}"