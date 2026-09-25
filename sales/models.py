from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from decimal import Decimal

from products.models import ProductVariant
from inventory.models import Branch

User = get_user_model()


class CashRegisterSession(models.Model):
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

    def get_current_expected_cash(self):
        """
        Opening float (already the carried-forward pool balance) + cash
        sales + cash-ins - cash-outs recorded against THIS session. Since
        opening_balance is always the prior session's closing_balance,
        this figure is equivalent to the cumulative all-time pool
        balance for the branch, not a per-session reset.
        """
        opening = self.opening_balance or Decimal('0.00')
        sales = self.total_cash_sales or Decimal('0.00')
        cash_in = self.total_cash_in or Decimal('0.00')
        cash_out = self.total_cash_out or Decimal('0.00')

        return opening + sales + cash_in - cash_out

    def save(self, *args, **kwargs):
        if self.status == 'CLOSED':
            self.expected_closing_balance = self.get_current_expected_cash()
            if self.closing_balance is not None:
                self.discrepancy = self.closing_balance - self.expected_closing_balance
        super().save(*args, **kwargs)


class CashTransaction(models.Model):
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


class Order(models.Model):
    PAYMENT_METHODS = (
        ('CASH', 'Cash'),
    )

    invoice_number = models.CharField(max_length=50, unique=True)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='sales', null=True)
    cashier = models.ForeignKey(User, on_delete=models.PROTECT, related_name='sales')
    cash_session = models.ForeignKey(CashRegisterSession, on_delete=models.PROTECT, related_name='orders', null=True, blank=True)

    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, default='CASH')
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_cogs = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_profit = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # Cash payment details
    amount_received = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Cash physically handed over by the customer"
    )
    change_given = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Change handed back to the customer (amount_received - total_revenue)"
    )

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