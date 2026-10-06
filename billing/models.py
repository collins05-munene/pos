import uuid
from datetime import timedelta

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
