from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from tenants.models import TenantOwnedModel


class Discount(TenantOwnedModel):
    """
    A price reduction a manager or admin sets up in advance (Settings -> Discounts).

    It is applied AUTOMATICALLY at the till to every matching product while it is running, so a
    cashier never types a price and cannot invent a discount. The sale records exactly what was
    given on each line (see sales.OrderItem.discount_per_unit), so editing or deleting a discount
    later never changes history.

    Rules, in plain words:
      * percentage  -> that % off the selling price of ONE unit (a box and a tablet are priced
                       separately, so each gets its own % off)
      * fixed       -> that many KSH off ONE unit
      * if several discounts match one product, the single best one wins (no stacking)
      * `min_quantity` only applies once that many units of the SAME line are in the cart
      * outside starts_at / ends_at, or when switched off, it does nothing
    """
    class Kind(models.TextChoices):
        PERCENT = "PERCENT", "Percentage off"
        FIXED = "FIXED", "Fixed amount off (per unit)"

    class Target(models.TextChoices):
        PRODUCTS = "PRODUCTS", "Specific products"
        CATEGORIES = "CATEGORIES", "Whole categories"
        ALL = "ALL", "Every product"

    name = models.CharField(max_length=120, help_text="Shown on receipts, e.g. 'Weekend promo'.")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PERCENT)
    value = models.DecimalField(max_digits=12, decimal_places=2,
                                help_text="Percent (e.g. 10) or KSH per unit (e.g. 20).")
    target = models.CharField(max_length=12, choices=Target.choices, default=Target.PRODUCTS)
    products = models.ManyToManyField("products.Product", blank=True, related_name="discounts")
    categories = models.ManyToManyField("products.Category", blank=True, related_name="discounts")
    branches = models.ManyToManyField("inventory.Branch", blank=True, related_name="discounts",
                                      help_text="Leave empty to run at every branch.")

    min_quantity = models.PositiveIntegerField(default=1, help_text="Units of one line needed before it applies.")
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True, help_text="Leave empty to run until you switch it off.")
    is_active = models.BooleanField(default=True)
    note = models.CharField(max_length=255, blank=True, help_text="Internal note: why this discount exists.")

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, editable=False,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_active", "-starts_at", "name"]
        constraints = [
            models.CheckConstraint(condition=models.Q(value__gt=0), name="discount_value_gt_0"),
            models.CheckConstraint(condition=models.Q(min_quantity__gte=1), name="discount_min_qty_gte_1"),
        ]
        indexes = [models.Index(fields=["tenant", "is_active", "starts_at"])]

    def __str__(self):
        return f"{self.name} ({self.value_label})"

    @property
    def value_label(self):
        v = Decimal(self.value).normalize()
        v = format(v, "f")
        return f"{v}% off" if self.kind == self.Kind.PERCENT else f"KSH {v} off"

    def is_running(self, at=None):
        at = at or timezone.now()
        if not self.is_active or self.starts_at > at:
            return False
        return self.ends_at is None or self.ends_at > at

    @property
    def status(self):
        now = timezone.now()
        if not self.is_active:
            return "OFF"
        if self.starts_at > now:
            return "SCHEDULED"
        if self.ends_at is not None and self.ends_at <= now:
            return "ENDED"
        return "RUNNING"
