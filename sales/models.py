from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from decimal import Decimal
from django.db.models.constraints import UniqueConstraint
from django.db import transaction

from products.models import ProductVariant
from inventory.models import Branch
from tenants.models import TenantOwnedModel

User = get_user_model()



def next_number(tenant, kind, prefix):
    if kind != "invoice":
        raise ValueError(f"Unknown number kind: {kind}")
    with transaction.atomic():
        
        last = (
            Order.objects
            .filter(tenant=tenant, invoice_number__startswith=prefix)
            .order_by("-invoice_number")
            .values_list("invoice_number", flat=True)
            .first()
        )
        n = int(last[len(prefix):]) if last else 0
        return f"{prefix}{n + 1:06d}" 

    
class CashRegisterSession(TenantOwnedModel):
    """
    Represents ONE shared, continuous cash pool for a branch — not a
    per-cashier drawer, and not an isolated daily ledger. Any cashier
    working a branch adds sales, cash-ins and cash-outs to the same
    pool while it is open; only one session may be OPEN per branch at
    a time (enforced by the constraint below).

    Continuity model:
        Day 1: opening 3,000 + cash sales 5,000            -> closing 8,000
        Day 2: opening 8,000 (carried forward, no manual
               override) + sales 2,000                     -> expected 9,000

    The pool never "resets" between sessions: a new session's
    opening_balance is always exactly the last closed session's
    physically-counted closing_balance for that branch (see
    get_branch_pool_balance / OpenRegisterView), so the running total
    is unbroken regardless of who opens or closes it.
    """
    STATUS_CHOICES = (
        ('OPEN', 'Open'),
        ('CLOSED', 'Closed'),
    )

    branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name='cash_sessions',
        null=True, blank=True
    )

    opened_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='opened_cash_sessions')
    closed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='closed_cash_sessions', null=True, blank=True)

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
        constraints = [
            models.UniqueConstraint(
                fields=['branch'],
                condition=models.Q(status='OPEN'),
                name='one_open_cash_session_per_branch',
            )
        ]

    def __str__(self):
        return f"Session #{self.id} - {self.branch} ({self.status})"

    @classmethod
    def get_active_session(cls, branch):
        """The single shared OPEN cash-pool session for a branch (or None)."""
        if branch is None:
            return None
        return cls.objects.filter(branch=branch, status='OPEN').first()

    @classmethod
    def get_branch_pool_balance(cls, branch):
        """
        The branch's continuous cash pool balance — authoritative whether
        or not a session is currently open. This is the single source of
        truth for "how much cash should be in this branch's pool right
        now", used both to seed the next session's opening_balance and
        to show a live figure between sessions (e.g. after closing,
        before the next open).

        - If a session is open: its live get_current_expected_cash()
          (opening + sales + cash_in - cash_out for that session, which
          itself chains back through every prior session).
        - Else: the last closed session's physically-counted
          closing_balance for this branch.
        - Else (branch has never had a session): 0.00.
        """
        if branch is None:
            return Decimal('0.00')

        active = cls.get_active_session(branch)
        if active:
            return active.get_current_expected_cash()

        last_closed = cls.objects.filter(
            branch=branch, status='CLOSED'
        ).order_by('-closed_at').first()

        if last_closed and last_closed.closing_balance is not None:
            return last_closed.closing_balance

        return Decimal('0.00')

    @property
    def total_cash_sales(self):
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

    @property
    def total_refunds(self):
        """Cash handed back to customers from the drawer during THIS session."""
        result = self.refunds.aggregate(total=models.Sum('total_amount'))['total']
        return result or Decimal('0.00')

    def get_current_expected_cash(self):
        """
        Opening float (already the carried-forward pool balance) + cash
        sales + cash-ins - cash-outs - customer refunds recorded against THIS session. Since
        opening_balance is always the prior session's closing_balance,
        this figure is equivalent to the cumulative all-time pool
        balance for the branch, not a per-session reset.
        """
        opening = self.opening_balance or Decimal('0.00')
        sales = self.total_cash_sales or Decimal('0.00')
        cash_in = self.total_cash_in or Decimal('0.00')
        cash_out = self.total_cash_out or Decimal('0.00')

        refunds = self.total_refunds or Decimal('0.00')

        return opening + sales + cash_in - cash_out - refunds

    def save(self, *args, **kwargs):
        if self.status == 'CLOSED':
            self.expected_closing_balance = self.get_current_expected_cash()
            if self.closing_balance is not None:
                self.discrepancy = self.closing_balance - self.expected_closing_balance
        super().save(*args, **kwargs)


class CashTransaction(TenantOwnedModel):
    TRANSACTION_TYPES = (
        ('CASH_IN', 'Cash In (Float Addition)'),
        ('CASH_OUT', 'Cash Out (Withdrawal)'),
    )

    session = models.ForeignKey(CashRegisterSession, on_delete=models.CASCADE, related_name='transactions')
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES, default='CASH_IN')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_transaction_type_display()} - KSH {self.amount}"


class Order(TenantOwnedModel):
    PAYMENT_METHODS = (
        ('CASH', 'Cash'),
    )
    invoice_number = models.CharField(max_length=50, blank=True, default="")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='sales', null=True)
    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name='sales')
    cash_session = models.ForeignKey(CashRegisterSession, on_delete=models.PROTECT, related_name='orders', null=True, blank=True)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, default='CASH')
    total_revenue = models.DecimalField(
        max_digits=12, decimal_places=2, default=0.00,
        help_text="What the customer actually paid for the goods: subtotal minus discounts.")
    subtotal = models.DecimalField(
        max_digits=12, decimal_places=2, default=0.00,
        help_text="Value of the goods at list price, before discounts (0 on sales made before discounts existed).")
    discount_total = models.DecimalField(
        max_digits=12, decimal_places=2, default=0.00, help_text="Total discounts given on this sale.")
    total_cogs = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    amount_received = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Cash physically handed over by the customer"
    )
    change_given = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Change handed back to the customer (amount_received - total_revenue)"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # --- offline / idempotency -------------------------------------------------
    # client_uuid is generated by the browser for every checkout. The same sale can
    # therefore be submitted twice (flaky network, retry after a timeout) and still
    # be recorded exactly once.
    client_uuid = models.UUIDField(null=True, blank=True, editable=False)
    is_offline_sale = models.BooleanField(default=False, help_text="Rung up with no connection and synced later.")
    synced_at = models.DateTimeField(null=True, blank=True)
    offline_flags = models.JSONField(default=list, blank=True,
                                     help_text="Anything a manager should look at: negative stock, price changes, clock skew ...")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "invoice_number"], name="uniq_tenant_invoice_number"),
            models.UniqueConstraint(fields=["tenant", "client_uuid"],
                                    condition=models.Q(client_uuid__isnull=False),
                                    name="uniq_tenant_order_client_uuid"),
        ]

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            self.invoice_number = next_number(self.tenant, "invoice", "INV-")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Invoice {self.invoice_number}"

    @property
    def refunded_amount(self):
        """Total cash refunded against this sale so far."""
        result = self.refunds.aggregate(total=models.Sum('total_amount'))['total']
        return result or Decimal('0.00')

    @property
    def refund_status(self):
        """'NONE', 'PARTIAL' or 'FULL' (by value refunded against value sold)."""
        refunded = self.refunded_amount
        if refunded <= 0:
            return 'NONE'
        return 'FULL' if refunded >= self.total_revenue else 'PARTIAL'

    @property
    def net_revenue(self):
        return self.total_revenue - self.refunded_amount


class OrderItem(TenantOwnedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    retail_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="List selling price per unit, before any discount")
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Buying price per unit")
    discount = models.ForeignKey('promotions.Discount', null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='order_items', help_text="The discount rule that was applied, if any")
    discount_name = models.CharField(max_length=120, blank=True, default='',
                                     help_text="Name of the discount at the time of sale (survives edits/deletes)")
    discount_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                            help_text="KSH taken off each unit")
    revenue_line = models.DecimalField(max_digits=12, decimal_places=2,
                                       help_text="What the customer paid for this line (after discount)")
    cogs_line = models.DecimalField(max_digits=12, decimal_places=2)
    profit_line = models.DecimalField(max_digits=12, decimal_places=2)

    @property
    def net_price(self):
        """What the customer actually paid per unit. Refunds hand back exactly this."""
        return self.retail_price - self.discount_per_unit

    @property
    def discount_line(self):
        return self.discount_per_unit * self.quantity

    def save(self, *args, **kwargs):
        self.revenue_line = self.net_price * self.quantity
        self.cogs_line = self.cost_price * self.quantity
        self.profit_line = self.revenue_line - self.cogs_line
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.variant.sku} X {self.quantity}"

class Receipt(TenantOwnedModel):
    """Frozen, customer-facing copy of a completed sale. See sales/receipts.py."""
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name='receipt')
    receipt_number = models.CharField(max_length=30)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='receipts', null=True)
    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name='receipts')
 
    # Snapshot fields: what the customer's paper receipt said at the time.
    shop_name = models.CharField(max_length=150)
    branch_name = models.CharField(max_length=150, blank=True, default="")
    cashier_name = models.CharField(max_length=150)
    payment_method = models.CharField(max_length=10, default='CASH')
    lines = models.JSONField(default=list)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                   help_text="Before discounts (0 on receipts issued before discounts existed)")
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    footer = models.CharField(max_length=200, blank=True, default='', help_text="Shop's footer text at the time of sale")
    amount_received = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    change_given = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
 
    issued_at = models.DateTimeField(default=timezone.now)
    print_count = models.PositiveIntegerField(default=0)
    last_printed_at = models.DateTimeField(null=True, blank=True)
 
    class Meta:
        ordering = ['-issued_at']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'receipt_number'], name='uniq_tenant_receipt_number'),
        ]
        indexes = [models.Index(fields=['tenant', '-issued_at'])]
 
    def __str__(self):
        return f"Receipt {self.receipt_number}"


class Refund(TenantOwnedModel):
    """
    Money handed back to a customer for goods from an earlier sale. See sales/refunds.py.

    The original Order and Receipt are never edited -- they stay exactly what the customer was
    given. A refund is its own record that points at the sale, so the books show both what was
    sold and what came back, and the cash leaves the drawer on the day it is actually paid out
    (cash_session is the register that is OPEN when the refund is made, not the sale's register).
    """
    REASONS = (
        ('WRONG_ITEM', 'Wrong item sold'),
        ('DEFECTIVE', 'Damaged / defective'),
        ('EXPIRED', 'Expired or near-expiry product'),
        ('CHANGED_MIND', 'Customer changed their mind'),
        ('OVERCHARGED', 'Overcharged / price error'),
        ('OTHER', 'Other'),
    )

    refund_number = models.CharField(max_length=30)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='refunds')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='refunds', null=True)
    cash_session = models.ForeignKey(CashRegisterSession, on_delete=models.PROTECT, related_name='refunds')
    processed_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='refunds_processed')

    reason = models.CharField(max_length=20, choices=REASONS)
    notes = models.TextField(blank=True, default='')

    total_amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="Cash paid back to the customer")
    cogs_reversed = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                        help_text="Cost of the goods that went back on the shelf")
    profit_reversed = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                          help_text="Profit given up: refund minus cost recovered")

    # Snapshot for the printed credit note.
    shop_name = models.CharField(max_length=150)
    branch_name = models.CharField(max_length=150, blank=True, default='')
    processed_by_name = models.CharField(max_length=150)

    client_uuid = models.UUIDField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'refund_number'], name='uniq_tenant_refund_number'),
            models.UniqueConstraint(fields=['tenant', 'client_uuid'],
                                    condition=models.Q(client_uuid__isnull=False),
                                    name='uniq_tenant_refund_client_uuid'),
        ]
        indexes = [models.Index(fields=['tenant', '-created_at'])]

    def __str__(self):
        return f"Refund {self.refund_number}"


class RefundItem(TenantOwnedModel):
    refund = models.ForeignKey(Refund, on_delete=models.CASCADE, related_name='items')
    order_item = models.ForeignKey(OrderItem, on_delete=models.PROTECT, related_name='refund_items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT)

    description = models.CharField(max_length=255)
    sku = models.CharField(max_length=100, blank=True, default='')
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="What the customer paid per unit")
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2)
    restocked = models.BooleanField(default=True, help_text="Went back into stock (False = written off)")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    cogs_reversed = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    def __str__(self):
        return f"{self.sku} x {self.quantity}"
