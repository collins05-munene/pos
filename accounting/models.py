"""
Double-entry accounting foundation for Unisale.

Design rules (see FINANCIAL MANAGEMENT spec, phases 5 and 16):
  * Every table is tenant-owned (TenantOwnedModel) -> tenant isolation comes for free.
  * Money is Decimal(14, 2), never float.
  * A POSTED journal entry is immutable. Corrections are made with a reversal entry.
  * Posting is idempotent: (tenant, source_key) is unique, so replaying a source
    transaction (retry, offline re-sync, double click) cannot create a second entry.
  * Balances are never cached here: they are always summed from journal lines, so every
    number on a report can be traced to the lines that produced it.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from tenants.models import TenantManager, TenantOwnedModel

from .storage import receipt_path, receipt_storage

MONEY = dict(max_digits=14, decimal_places=2)


class LedgerImmutable(Exception):
    """Raised on any attempt to edit or delete posted ledger data."""


# --------------------------------------------------------------------------
# Querysets that refuse to bulk-edit posted ledger rows
# --------------------------------------------------------------------------
class _PostedGuardQuerySet(models.QuerySet):
    """update()/delete() skip Model.save()/delete(), so guard them here too."""
    posted_lookup = "status"          # overridden on the line queryset

    def _guard(self):
        if self.exclude(**{self.posted_lookup: "DRAFT"}).exists():
            raise LedgerImmutable("Posted journal entries and their lines cannot be changed or deleted. "
                                  "Post a reversal entry instead.")

    def update(self, **kwargs):
        self._guard()
        return super().update(**kwargs)

    def delete(self):
        self._guard()
        return super().delete()


class EntryQuerySet(_PostedGuardQuerySet):
    posted_lookup = "status"


class LineQuerySet(_PostedGuardQuerySet):
    posted_lookup = "entry__status"


class EntryManager(TenantManager.from_queryset(EntryQuerySet)):
    pass


class LineManager(TenantManager.from_queryset(LineQuerySet)):
    pass


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
class AccountingSettings(TenantOwnedModel):
    class Basis(models.TextChoices):
        CASH = "CASH", "Cash basis"
        ACCRUAL = "ACCRUAL", "Accrual basis"

    reporting_basis = models.CharField(max_length=10, choices=Basis.choices, default=Basis.ACCRUAL,
                                       help_text="Only changes how reports are built. Never alters stored entries.")
    currency_code = models.CharField(max_length=3, default="KES")
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1, help_text="1 = January")
    opening_balance_date = models.DateField(
        null=True, blank=True,
        help_text="The ledger starts on this date. Nothing earlier can be posted.")
    # Not hardcoded: owners set their own rate(s); None means 'not configured'.
    default_tax_rate_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    is_initialised = models.BooleanField(default=False)
    managers_can_manage_expenses = models.BooleanField(
        default=True, help_text="Branch managers may record expenses and payments for their own branch.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Accounting settings"
        constraints = [models.UniqueConstraint(fields=["tenant"], name="uniq_acct_settings_per_tenant")]

    def __str__(self):
        return f"Accounting settings ({self.tenant_id})"


# --------------------------------------------------------------------------
# Chart of accounts
# --------------------------------------------------------------------------
class Account(TenantOwnedModel):
    class Type(models.TextChoices):
        ASSET = "ASSET", "Asset"
        LIABILITY = "LIABILITY", "Liability"
        EQUITY = "EQUITY", "Equity"
        REVENUE = "REVENUE", "Revenue"
        COGS = "COGS", "Cost of goods sold"
        OPEX = "OPEX", "Operating expense"
        OTHER_EXPENSE = "OTHER_EXPENSE", "Other expense / finance cost"

    # code range -> type (spec phase 5). Used to validate custom accounts.
    CODE_PREFIX = {"1": Type.ASSET, "2": Type.LIABILITY, "3": Type.EQUITY, "4": Type.REVENUE,
                   "5": Type.COGS, "6": Type.OPEX, "7": Type.OTHER_EXPENSE}
    DEBIT_TYPES = (Type.ASSET, Type.COGS, Type.OPEX, Type.OTHER_EXPENSE)

    code = models.CharField(max_length=10)
    name = models.CharField(max_length=120)
    account_type = models.CharField(max_length=15, choices=Type.choices)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    is_header = models.BooleanField(default=False, help_text="Group heading. Cannot be posted to.")
    is_contra = models.BooleanField(default=False,
                                    help_text="Balance runs opposite to its type (e.g. accumulated depreciation).")
    system_key = models.CharField(max_length=40, null=True, blank=True,
                                  help_text="Stable handle code uses to find this account. Not editable by users.")
    is_system = models.BooleanField(default=False, help_text="Created by the platform; cannot be deleted.")
    is_active = models.BooleanField(default=True)
    description = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uniq_account_code_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "system_key"], condition=Q(system_key__isnull=False),
                                    name="uniq_account_system_key_per_tenant"),
        ]

    def __str__(self):
        return f"{self.code} {self.name}"

    @property
    def normal_side(self):
        """'DEBIT' or 'CREDIT': the side that increases this account."""
        natural_debit = self.account_type in self.DEBIT_TYPES
        return "DEBIT" if natural_debit != self.is_contra else "CREDIT"

    def clean(self):
        from django.core.exceptions import ValidationError
        expected = self.CODE_PREFIX.get(str(self.code)[:1])
        if expected is None or not str(self.code).isdigit():
            raise ValidationError({"code": "Account code must be numeric and start with 1-7."})
        if expected != self.account_type:
            raise ValidationError({"account_type": f"Codes starting {self.code[:1]} are {expected.label} accounts."})

    def save(self, *args, **kwargs):
        if self.pk and self.is_system:
            original = Account.all_objects.filter(pk=self.pk).values("code", "account_type", "system_key").first()
            if original and (original["code"] != self.code or original["account_type"] != self.account_type
                             or original["system_key"] != self.system_key):
                raise LedgerImmutable("Code, type and key of a system account cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_system:
            raise LedgerImmutable("System accounts cannot be deleted. Deactivate them instead.")
        if JournalLine.all_objects.filter(account=self).exists():
            raise LedgerImmutable("This account has postings. Deactivate it instead of deleting it.")
        return super().delete(*args, **kwargs)


# --------------------------------------------------------------------------
# Fiscal periods
# --------------------------------------------------------------------------
class FiscalPeriod(TenantOwnedModel):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    name = models.CharField(max_length=40)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")

    class Meta:
        ordering = ["start_date"]
        constraints = [
            models.UniqueConstraint(fields=["tenant", "start_date"], name="uniq_period_start_per_tenant"),
            models.CheckConstraint(condition=Q(end_date__gte=models.F("start_date")), name="period_end_after_start"),
        ]

    def __str__(self):
        return self.name


# --------------------------------------------------------------------------
# Journals
# --------------------------------------------------------------------------
class JournalEntry(TenantOwnedModel):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        POSTED = "POSTED", "Posted"
        REVERSED = "REVERSED", "Reversed"

    reference = models.CharField(max_length=30)
    entry_date = models.DateField(db_index=True, help_text="Transaction date (accrual date).")
    description = models.CharField(max_length=255)
    branch = models.ForeignKey("inventory.Branch", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="journal_entries")
    period = models.ForeignKey(FiscalPeriod, on_delete=models.PROTECT, related_name="entries")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.POSTED)

    # What created it. source_key makes posting idempotent.
    source_type = models.CharField(max_length=40, db_index=True, help_text="e.g. SALE, REFUND, EXPENSE, MANUAL")
    source_id = models.CharField(max_length=64, blank=True)
    source_key = models.CharField(max_length=120, null=True, blank=True)

    reverses = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="reversed_by", help_text="Set on a reversal entry.")
    reversal_reason = models.CharField(max_length=255, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    objects = EntryManager()
    all_objects = models.Manager.from_queryset(EntryQuerySet)()

    class Meta:
        ordering = ["-entry_date", "-id"]
        verbose_name_plural = "Journal entries"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "reference"], name="uniq_journal_ref_per_tenant"),
            models.UniqueConstraint(fields=["tenant", "source_key"], condition=Q(source_key__isnull=False),
                                    name="uniq_journal_source_key_per_tenant"),
        ]
        indexes = [models.Index(fields=["tenant", "entry_date"], name="je_tenant_date_idx"),
                   models.Index(fields=["tenant", "branch", "entry_date"], name="je_tenant_branch_date_idx")]

    def __str__(self):
        return self.reference

    def save(self, *args, **kwargs):
        if self.pk:
            old = JournalEntry.all_objects.filter(pk=self.pk).values(
                "status", "entry_date", "description", "branch_id", "source_key", "reference", "period_id",
                "source_type", "source_id").first()
            if old and old["status"] != "DRAFT":
                same = all(old[k] == getattr(self, k) for k in (
                    "entry_date", "description", "branch_id", "source_key", "reference", "period_id",
                    "source_type", "source_id"))
                # The only permitted change on a posted entry: POSTED -> REVERSED (done by reverse_entry()).
                if not same or (old["status"] == "REVERSED") or self.status not in ("POSTED", "REVERSED"):
                    raise LedgerImmutable("A posted journal entry cannot be modified. Post a reversal instead.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status != self.Status.DRAFT:
            raise LedgerImmutable("A posted journal entry cannot be deleted. Post a reversal instead.")
        return super().delete(*args, **kwargs)

    def get_lines(self):
        """Lines of this entry. Explicit, so it works outside a request (no active tenant)."""
        return JournalLine.all_objects.filter(entry=self).select_related("account")

    @property
    def total_debit(self):
        return self.get_lines().aggregate(t=models.Sum("debit"))["t"] or Decimal("0.00")

    @property
    def total_credit(self):
        return self.get_lines().aggregate(t=models.Sum("credit"))["t"] or Decimal("0.00")


class JournalLine(TenantOwnedModel):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    branch = models.ForeignKey("inventory.Branch", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="journal_lines",
                               help_text="Branch this line belongs to (can differ from the entry for allocations).")
    debit = models.DecimalField(default=Decimal("0.00"), **MONEY)
    credit = models.DecimalField(default=Decimal("0.00"), **MONEY)
    memo = models.CharField(max_length=255, blank=True)

    objects = LineManager()
    all_objects = models.Manager.from_queryset(LineQuerySet)()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(condition=Q(debit__gte=0) & Q(credit__gte=0), name="line_non_negative"),
            # exactly one side carries the amount
            models.CheckConstraint(
                condition=(Q(debit__gt=0) & Q(credit=0)) | (Q(credit__gt=0) & Q(debit=0)),
                name="line_one_sided"),
        ]
        indexes = [models.Index(fields=["tenant", "account"], name="jl_tenant_account_idx"),
                   models.Index(fields=["tenant", "branch"], name="jl_tenant_branch_idx")]

    def __str__(self):
        return f"{self.account.code} Dr {self.debit} Cr {self.credit}"

    def save(self, *args, **kwargs):
        if self.entry.status != JournalEntry.Status.DRAFT:
            raise LedgerImmutable("Lines of a posted journal entry cannot be changed.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.entry.status != JournalEntry.Status.DRAFT:
            raise LedgerImmutable("Lines of a posted journal entry cannot be deleted.")
        return super().delete(*args, **kwargs)


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------
class FinancialAuditLog(TenantOwnedModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                             related_name="+")
    action = models.CharField(max_length=40, db_index=True)
    object_type = models.CharField(max_length=40, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise LedgerImmutable("Audit log entries cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerImmutable("Audit log entries cannot be deleted.")


# ==========================================================================
# Expenses
# ==========================================================================
PAYMENT_METHODS = (("CASH", "Cash"), ("BANK", "Bank transfer"), ("MPESA", "M-Pesa"))
METHOD_ACCOUNT_KEY = {"CASH": "CASH", "BANK": "BANK", "MPESA": "MPESA"}


class ExpenseCategory(TenantOwnedModel):
    name = models.CharField(max_length=80)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="expense_categories",
                                help_text="Ledger account this category's expenses are recorded in.")
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Expense categories"
        constraints = [models.UniqueConstraint(fields=["tenant", "name"], name="uniq_expense_category_per_tenant")]

    def __str__(self):
        return self.name

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.account_id and (self.account.is_header or self.account.account_type not in
                                (Account.Type.OPEX, Account.Type.OTHER_EXPENSE, Account.Type.COGS)):
            raise ValidationError({"account": "Choose an expense account (codes 5xxx-7xxx)."})

    def delete(self, *args, **kwargs):
        if self.is_system:
            raise LedgerImmutable("Default categories cannot be deleted. Deactivate them instead.")
        if Expense.all_objects.filter(category=self).exists():
            raise LedgerImmutable("This category has expenses. Deactivate it instead.")
        return super().delete(*args, **kwargs)


class RecurringExpense(TenantOwnedModel):
    class Frequency(models.TextChoices):
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"
        QUARTERLY = "QUARTERLY", "Quarterly"
        ANNUAL = "ANNUAL", "Annual"

    title = models.CharField(max_length=150)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="recurring")
    amount = models.DecimalField(**MONEY)
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    branch = models.ForeignKey("inventory.Branch", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="recurring_expenses", help_text="Blank = head office")
    vendor = models.CharField(max_length=150, blank=True)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, blank=True)
    due_after_days = models.PositiveSmallIntegerField(default=0, help_text="Due date = expense date + this many days")
    description = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="recurring_amount_positive")]

    def __str__(self):
        return self.title


class Expense(TenantOwnedModel):
    class Status(models.TextChoices):
        UNPAID = "UNPAID", "Unpaid"
        PARTIAL = "PARTIAL", "Partially paid"
        PAID = "PAID", "Paid"
        CANCELLED = "CANCELLED", "Cancelled"

    title = models.CharField(max_length=150)
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT, related_name="expenses")
    amount = models.DecimalField(**MONEY)
    expense_date = models.DateField(db_index=True, help_text="When the cost was incurred (accrual date).")
    due_date = models.DateField(null=True, blank=True)
    branch = models.ForeignKey("inventory.Branch", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="expenses", help_text="Blank = head office")
    vendor = models.CharField(max_length=150, blank=True)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, blank=True,
                                      help_text="How you expect to pay. Payments record the real method.")
    reference = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    receipt = models.FileField(upload_to=receipt_path, storage=receipt_storage, max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNPAID, db_index=True)
    recurring = models.ForeignKey(RecurringExpense, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="expenses")
    recurrence_date = models.DateField(null=True, blank=True)
    revision = models.PositiveSmallIntegerField(default=1, help_text="Bumped whenever the posted figures are re-stated.")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-expense_date", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="expense_amount_positive"),
            # one generated expense per recurrence date: the guard against double generation
            models.UniqueConstraint(fields=["recurring", "recurrence_date"], condition=Q(recurring__isnull=False),
                                    name="uniq_expense_per_recurrence_date"),
        ]
        indexes = [models.Index(fields=["tenant", "expense_date"], name="exp_tenant_date_idx"),
                   models.Index(fields=["tenant", "branch", "expense_date"], name="exp_tenant_branch_idx")]

    def __str__(self):
        return self.title

    def _payments(self):
        return ExpensePayment.all_objects.filter(expense=self, is_voided=False)

    @property
    def amount_paid(self):
        return self._payments().aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")

    @property
    def balance(self):
        if self.status == self.Status.CANCELLED:
            return Decimal("0.00")
        return self.amount - self.amount_paid

    @property
    def is_overdue(self):
        import datetime
        return bool(self.status in (self.Status.UNPAID, self.Status.PARTIAL) and self.due_date
                    and self.due_date < datetime.date.today())

    @property
    def display_status(self):
        return "Overdue" if self.is_overdue else self.get_status_display()


class ExpenseAllocation(TenantOwnedModel):
    """Splits one (usually head-office) expense across branches. Percentages must total 100."""
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="allocations")
    branch = models.ForeignKey("inventory.Branch", on_delete=models.PROTECT, related_name="+")
    percent = models.DecimalField(max_digits=5, decimal_places=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["expense", "branch"], name="uniq_allocation_branch")]


class ExpensePayment(TenantOwnedModel):
    expense = models.ForeignKey(Expense, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(**MONEY)
    payment_date = models.DateField(db_index=True)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+",
                                help_text="Cash/bank/M-Pesa account the money left from.")
    reference = models.CharField(max_length=100, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_voided = models.BooleanField(default=False)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                  related_name="+")
    void_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-payment_date", "-id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="expense_payment_positive")]
        indexes = [models.Index(fields=["tenant", "payment_date"], name="exppay_tenant_date_idx")]


# ==========================================================================
# Other income, owner transactions
# ==========================================================================
class OtherIncome(TenantOwnedModel):
    class Kind(models.TextChoices):
        RENTAL = "RENTAL", "Rental income"
        INTEREST = "INTEREST", "Interest income"
        SERVICE = "SERVICE", "Service income"
        OTHER = "OTHER", "Other business income"

    KIND_ACCOUNT = {"RENTAL": "INCOME_RENTAL", "INTEREST": "INCOME_INTEREST",
                    "SERVICE": "INCOME_SERVICE", "OTHER": "INCOME_OTHER"}

    title = models.CharField(max_length=150)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OTHER)
    amount = models.DecimalField(**MONEY)
    income_date = models.DateField(db_index=True, help_text="When it was earned.")
    received_on = models.DateField(null=True, blank=True, help_text="When the money arrived. Blank = still owed to you.")
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, blank=True)
    branch = models.ForeignKey("inventory.Branch", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="other_income")
    payer = models.CharField(max_length=150, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    description = models.CharField(max_length=255, blank=True)
    is_cancelled = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-income_date", "-id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="other_income_positive")]


class OwnerTransaction(TenantOwnedModel):
    class Kind(models.TextChoices):
        CAPITAL = "CAPITAL", "Owner capital contribution"
        DRAWING = "DRAWING", "Owner withdrawal (drawings)"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(**MONEY)
    txn_date = models.DateField(db_index=True)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS)
    branch = models.ForeignKey("inventory.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    reference = models.CharField(max_length=100, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-txn_date", "-id"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="owner_txn_positive")]


# ==========================================================================
# Fixed assets
# ==========================================================================
class FixedAsset(TenantOwnedModel):
    class Category(models.TextChoices):
        EQUIPMENT = "EQUIPMENT", "Equipment"
        VEHICLE = "VEHICLE", "Vehicle"
        FURNITURE = "FURNITURE", "Furniture and fittings"
        COMPUTER = "COMPUTER", "Computers"
        OTHER = "OTHER", "Other"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "In use"
        DISPOSED = "DISPOSED", "Disposed"

    name = models.CharField(max_length=150)
    category = models.CharField(max_length=12, choices=Category.choices, default=Category.EQUIPMENT)
    cost = models.DecimalField(**MONEY)
    purchase_date = models.DateField()
    branch = models.ForeignKey("inventory.Branch", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="fixed_assets")
    useful_life_months = models.PositiveSmallIntegerField()
    residual_value = models.DecimalField(default=Decimal("0.00"), **MONEY)
    depreciation_method = models.CharField(max_length=20, default="STRAIGHT_LINE")
    paid_with = models.CharField(max_length=10, choices=PAYMENT_METHODS + (("CREDIT", "On credit (unpaid)"),),
                                 blank=True)
    existing_asset = models.BooleanField(
        default=False, help_text="Bought before the books started: already inside your opening balances.")
    opening_accumulated_depreciation = models.DecimalField(default=Decimal("0.00"), **MONEY)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    disposal_date = models.DateField(null=True, blank=True)
    disposal_proceeds = models.DecimalField(null=True, blank=True, **MONEY)
    disposal_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=Q(cost__gt=0), name="asset_cost_positive"),
            models.CheckConstraint(condition=Q(residual_value__gte=0) & Q(residual_value__lt=models.F("cost")),
                                   name="asset_residual_below_cost"),
        ]

    def __str__(self):
        return self.name

    @property
    def depreciable_amount(self):
        return self.cost - self.residual_value

    @property
    def accumulated_depreciation(self):
        posted = AssetDepreciation.all_objects.filter(asset=self).aggregate(t=models.Sum("amount"))["t"] or Decimal("0.00")
        return self.opening_accumulated_depreciation + posted

    @property
    def book_value(self):
        if self.status == self.Status.DISPOSED:
            return Decimal("0.00")
        return self.cost - self.accumulated_depreciation


class AssetDepreciation(TenantOwnedModel):
    asset = models.ForeignKey(FixedAsset, on_delete=models.PROTECT, related_name="depreciation")
    period_end = models.DateField()
    amount = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["period_end"]
        constraints = [models.UniqueConstraint(fields=["asset", "period_end"], name="uniq_depreciation_per_month"),
                       models.CheckConstraint(condition=Q(amount__gt=0), name="depreciation_positive")]


# ==========================================================================
# Loans
# ==========================================================================
class Loan(TenantOwnedModel):
    class Frequency(models.TextChoices):
        WEEKLY = "WEEKLY", "Weekly"
        MONTHLY = "MONTHLY", "Monthly"
        QUARTERLY = "QUARTERLY", "Quarterly"

    lender = models.CharField(max_length=150)
    principal = models.DecimalField(**MONEY)
    interest_rate_percent = models.DecimalField(max_digits=6, decimal_places=3, help_text="Per year")
    start_date = models.DateField()
    installments = models.PositiveSmallIntegerField(default=12)
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY)
    received_via = models.CharField(max_length=10, choices=PAYMENT_METHODS, blank=True)
    branch = models.ForeignKey("inventory.Branch", null=True, blank=True, on_delete=models.PROTECT, related_name="loans")
    existing_loan = models.BooleanField(default=False,
                                        help_text="Taken before the books started: already in your opening balances.")
    opening_outstanding = models.DecimalField(null=True, blank=True, **MONEY)
    is_closed = models.BooleanField(default=False)
    notes = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_date"]
        constraints = [models.CheckConstraint(condition=Q(principal__gt=0), name="loan_principal_positive")]

    def __str__(self):
        return f"{self.lender} {self.principal}"

    @property
    def principal_repaid(self):
        return LoanRepayment.all_objects.filter(loan=self).aggregate(t=models.Sum("principal_paid"))["t"] or Decimal("0.00")

    @property
    def interest_paid(self):
        return LoanRepayment.all_objects.filter(loan=self).aggregate(t=models.Sum("interest_paid"))["t"] or Decimal("0.00")

    @property
    def outstanding_principal(self):
        start = self.opening_outstanding if self.existing_loan and self.opening_outstanding is not None else self.principal
        return start - self.principal_repaid


class LoanRepayment(TenantOwnedModel):
    loan = models.ForeignKey(Loan, on_delete=models.PROTECT, related_name="repayments")
    paid_on = models.DateField()
    principal_paid = models.DecimalField(default=Decimal("0.00"), **MONEY)
    interest_paid = models.DecimalField(default=Decimal("0.00"), **MONEY)
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS)
    reference = models.CharField(max_length=100, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]
        constraints = [models.CheckConstraint(condition=Q(principal_paid__gte=0) & Q(interest_paid__gte=0)
                                              & (Q(principal_paid__gt=0) | Q(interest_paid__gt=0)),
                                              name="loan_repayment_positive")]
