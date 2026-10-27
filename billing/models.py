import uuid
from datetime import timedelta

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

from tenants.models import Tenant
from . import conf


class Subscription(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting first payment"
        TRIALING = "TRIALING", "Trial"
        ACTIVE = "ACTIVE", "Active"
        PAST_DUE = "PAST_DUE", "Past due (grace period)"
        SUSPENDED = "SUSPENDED", "Suspended (locked)"
        CANCELLED = "CANCELLED", "Cancelled"

    tenant = models.OneToOneField(Tenant, on_delete=models.CASCADE, related_name="subscription")
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    term_months = models.PositiveSmallIntegerField(default=1)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True, db_index=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    suspended_at = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    monthly_equivalent = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="What this tenant pays per month after term discount. Feeds MRR.",
    )

    custom_monthly_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    branch_slots = models.PositiveSmallIntegerField(
        default=1, help_text="Branches this shop has paid for (1 is included in the base price).")
    comp_until = models.DateTimeField(null=True, blank=True,
                                      help_text="Free access until this moment, regardless of payment.")
    override_note = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tenant.slug}: {self.status}"

    def access_deadline(self):
        if not self.current_period_end:
            return None
        if self.cancel_at_period_end:
            return self.current_period_end
        return self.current_period_end + timedelta(days=conf.get("GRACE_DAYS"))

    def grants_access(self, now=None):
        """
        Computed from dates, not just `status`, so a late cron run never gives
        free access or locks a paid customer.
        """
        now = now or timezone.now()
        if self.comp_until and now < self.comp_until:
            return True
        if self.status == self.Status.TRIALING:
            return bool(self.trial_ends_at and now < self.trial_ends_at)
        if self.status in (self.Status.ACTIVE, self.Status.PAST_DUE):
            deadline = self.access_deadline()
            return bool(deadline and now < deadline)
        return False


class Invoice(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        PAID = "PAID", "Paid"
        VOID = "VOID", "Void"

    number = models.CharField(max_length=20, unique=True, blank=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="invoices")
    term_months = models.PositiveSmallIntegerField()
    list_price_amount = models.DecimalField(max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    subscription_amount = models.DecimalField(max_digits=12, decimal_places=2)
    install_fee_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    branch_slots = models.PositiveSmallIntegerField(
        default=1, help_text="Branches this invoice covers; applied to the subscription when paid.")
    is_upgrade = models.BooleanField(
        default=False, help_text="Mid-term add-on (extra branches). Paying it never extends the period.")
    total = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN, db_index=True)
    issued_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True, db_index=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-issued_at"]

    def __str__(self):
        return f"{self.number} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = f"TMP-{uuid.uuid4().hex[:12]}"
        super().save(*args, **kwargs)
        if self.number.startswith("TMP-"):
            self.number = f"INV-{self.pk:06d}"
            super().save(update_fields=["number"])


class MpesaPayment(models.Model):
    """One STK-push attempt. An invoice may have several (cancelled, wrong PIN, retry)."""

    class Status(models.TextChoices):
        INITIATED = "INITIATED", "Waiting for customer"
        SUCCESS = "SUCCESS", "Paid"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled by customer"

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="mpesa_payments")
    phone = models.CharField(max_length=15)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    merchant_request_id = models.CharField(max_length=64, blank=True)
    checkout_request_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.INITIATED, db_index=True)
    result_code = models.IntegerField(null=True, blank=True)
    result_desc = models.CharField(max_length=255, blank=True)
    mpesa_receipt = models.CharField(max_length=30, unique=True, null=True, blank=True)
    raw_callback = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class BillingEvent(models.Model):
    """Append-only ledger of everything that happens. Feeds audit + email/SMS hooks."""
    tenant = models.ForeignKey(Tenant, on_delete=models.SET_NULL, null=True, blank=True,related_name="billing_events")
    type = models.CharField(max_length=50, db_index=True)
    payload = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]



class MpesaCode(models.Model):
    """
    A payment YOU received, entered from the M-Pesa confirmation SMS in the platform
    admin. A shop's typed-in code is approved only if it matches an unused row here
    and the amount covers the invoice. Each code can pay exactly one invoice.
    """
    code = models.CharField(max_length=20, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    sender_phone = models.CharField(max_length=20, blank=True)
    sender_name = models.CharField(max_length=120, blank=True)
    transaction_at = models.DateTimeField(null=True, blank=True,
                                          help_text="Date/time printed in the SMS.")
    raw_message = models.TextField(blank=True)
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_void = models.BooleanField(default=False, help_text="Entered by mistake; can never be used.")
    used_at = models.DateTimeField(null=True, blank=True)
    used_by_tenant = models.ForeignKey(Tenant, null=True, blank=True,
                                       on_delete=models.SET_NULL, related_name="+")
    used_invoice = models.ForeignKey(Invoice, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="manual_codes")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} (KES {self.amount})"

    @property
    def state(self):
        if self.is_void:
            return "VOID"
        return "USED" if self.used_at else "UNUSED"


class ManualPaymentRequest(models.Model):
    """A shop saying "I paid, here is my M-Pesa code". Audit trail for every attempt."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for confirmation"
        APPROVED = "APPROVED", "Approved"
        PARTIAL = "PARTIAL", "Received, balance still due"
        REJECTED = "REJECTED", "Rejected"

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="manual_payment_requests")
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="manual_requests")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    code = models.CharField(max_length=20, db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices,
                              default=Status.PENDING, db_index=True)
    reason = models.CharField(max_length=255, blank=True)
    matched_code = models.ForeignKey(MpesaCode, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="requests")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]



class BillingCredit(models.Model):
    """
    Per-shop wallet ledger (signed amounts; balance = sum). Manual payments land here first:
    an underpayment sits as credit until the balance covers the invoice, and an overpayment
    leaves the surplus for the next invoice.
    """

    class Kind(models.TextChoices):
        TOPUP = "TOPUP", "Payment received"
        APPLIED = "APPLIED", "Applied to invoice"
        ADJUSTMENT = "ADJUSTMENT", "Manual adjustment"

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="billing_credits")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    mpesa_code = models.ForeignKey(MpesaCode, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="credits")
    invoice = models.ForeignKey(Invoice, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="credits")
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
