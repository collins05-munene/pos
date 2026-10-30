from django.conf import settings
from django.db import models, transaction
from django.db.models import Expression
from django.utils.text import slugify

from .context import get_current_tenant

RESERVED_SLUGS = {
    "admin", "api", "app", "billing", "platform", "signup", "static", "media",
    "www", "support", "login", "logout", "users", "help", "mail",
}


class TenantNotSet(Exception):
    """Tried to save a tenant-owned row with no tenant known."""


class CrossTenantWrite(Exception):
    """Tried to save a row belonging to a different tenant than the active one."""


class Tenant(models.Model):
    class BusinessType(models.TextChoices):
        MINI_MART = "MINI_MART", "Mini Mart"
        PHARMACY = "PHARMACY", "Pharmacy"
        OTHER = "OTHER", "Other"

    name = models.CharField(max_length=150)
    slug = models.SlugField(
        max_length=40, unique=True,
        help_text="Business code. Staff type this at the POS login screen.",
    )
    business_type = models.CharField(
        max_length=20, choices=BusinessType.choices, default=BusinessType.MINI_MART
    )
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(
        default=True,
        help_text="Platform kill-switch, independent of billing. Off = locked out.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.slug})"

    @property
    def has_access(self):
        """May this tenant's users use the product right now?"""
        if not self.is_active:
            return False
        sub = getattr(self, "subscription", None)  # missing row -> AttributeError subclass
        return bool(sub and sub.grants_access())


def generate_unique_slug(name):
    base = slugify(name)[:30].strip("-") or "business"
    if base in RESERVED_SLUGS:
        base = f"{base}-shop"
    slug, n = base, 1
    while Tenant.objects.filter(slug=slug).exists():
        n += 1
        slug = f"{base}-{n}"
    return slug


# --------------------------------------------------------------------------
# Tenant-scoped base model + manager
# --------------------------------------------------------------------------
class CurrentTenantId(Expression):
    """
    SQL fragment that reads the active tenant when the query is COMPILED (i.e. when
    it actually runs), not when the queryset is built. That matters because Django
    builds querysets at import time in places you don't control: `queryset = Model.objects...`
    on class-based views and every ModelForm ForeignKey dropdown. With no active tenant
    it renders NULL, and `tenant_id = NULL` matches nothing: fail-closed.
    """
    output_field = models.BigIntegerField()

    def as_sql(self, compiler, connection):
        tenant = get_current_tenant()
        if tenant is None:
            return "NULL", []
        return "%s", [tenant.pk]


def scope_to_current_tenant(queryset):
    return queryset.filter(tenant_id=CurrentTenantId())


class TenantManager(models.Manager):
    """Default manager for every tenant-owned model (see CurrentTenantId)."""

    def get_queryset(self):
        return scope_to_current_tenant(super().get_queryset())

    def bulk_create(self, objs, *args, **kwargs):
        tenant = get_current_tenant()
        objs = list(objs)
        for obj in objs:
            if obj.tenant_id is None and tenant is not None:
                obj.tenant_id = tenant.pk
        return super().bulk_create(objs, *args, **kwargs)


class TenantOwnedModel(models.Model):
    """
    Inherit from this for every business-data table.

    objects      -> scoped to the active tenant, resolved at query time (use in views)
    all_objects  -> unscoped (admin, migrations, platform reports, cron)
    """
    tenant_required = True

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.PROTECT,
        editable=False,            # never appears in ModelForms -> no mass-assignment
        db_index=True,
        # Migration phase A only (see MIGRATION.md). Must be False in normal operation.
        null=getattr(settings, "TENANCY_ALLOW_NULL_TENANT", False),
        related_name="%(app_label)s_%(class)s_set",
    )

    objects = TenantManager()       # first manager == _default_manager
    all_objects = models.Manager()

    class Meta:
        abstract = True

    def validate_constraints(self, exclude=None):
        """
        ModelForms exclude non-editable fields from validation, which silently skips every
        per-tenant UniqueConstraint (they all include `tenant`). Validate them anyway so a
        duplicate SKU is a friendly form error instead of an IntegrityError/500.
        """
        if self.tenant_id is None:
            current = get_current_tenant()
            if current is not None:
                self.tenant_id = current.pk
        exclude = set(exclude or ()) - {"tenant"}
        super().validate_constraints(exclude=exclude)

    def save(self, *args, **kwargs):
        current = get_current_tenant()
        if self.tenant_id is None:
            if current is not None:
                self.tenant_id = current.pk
            elif self.tenant_required:
                raise TenantNotSet(
                    f"Cannot save {self.__class__.__name__} without an active tenant."
                )
        elif current is not None and self.tenant_id != current.pk:
            raise CrossTenantWrite(
                f"{self.__class__.__name__} belongs to tenant {self.tenant_id}, "
                f"active tenant is {current.pk}."
            )
        super().save(*args, **kwargs)


# --------------------------------------------------------------------------
# Per-tenant document numbering (each shop's invoices start at 000001)
# --------------------------------------------------------------------------
class TenantSequence(models.Model):
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="sequences")
    name = models.CharField(max_length=30)
    last_value = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "name"], name="uniq_tenant_sequence")
        ]


def next_number(tenant, name, prefix="", width=6):
    """next_number(tenant, 'invoice', 'INV-') -> 'INV-000001'. Race-safe on PostgreSQL."""
    with transaction.atomic():
        seq, _ = TenantSequence.objects.select_for_update().get_or_create(
            tenant=tenant, name=name
        )
        seq.last_value += 1
        seq.save(update_fields=["last_value"])
    return f"{prefix}{seq.last_value:0{width}d}"

# --------------------------------------------------------------------------
# Per-shop business rules (Settings page)
# --------------------------------------------------------------------------
class TenantSettings(TenantOwnedModel):
    """
    One row per shop holding the rules the owner/admin can change without a developer.

    Add a new rule by adding a field here + one entry in tenants/settings_registry.py;
    the Settings page, validation and audit trail pick it up from the registry.
    Always read it through get_settings(tenant): it creates the row with defaults on first use.
    """
    class BelowCost(models.TextChoices):
        LIMIT = "LIMIT", "Stop the discount at cost price (recommended)"
        ALLOW = "ALLOW", "Allow selling below cost"

    # ---- discounts
    discounts_enabled = models.BooleanField(
        default=True, help_text="Master switch. Off = no discount is applied anywhere, nothing is deleted.")
    managers_can_manage_discounts = models.BooleanField(
        default=True, help_text="Off = only admins can create, edit or switch discounts.")
    max_discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=50,
        help_text="No sale line may ever be discounted by more than this %, whoever set it up.")
    manager_max_discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=20,
        help_text="The most a manager (not an admin) may give on one product.")
    below_cost_policy = models.CharField(max_length=10, choices=BelowCost.choices, default=BelowCost.LIMIT)

    # ---- refunds
    refund_window_days = models.PositiveIntegerField(
        default=30, help_text="Managers can refund a sale up to this many days after it. Admins can go past it.")

    # ---- receipts
    receipt_footer = models.CharField(
        max_length=200, blank=True, default="", help_text="Printed at the bottom of every receipt, e.g. 'Goods once sold are not returnable'.")

    # ---- offline selling
    offline_max_hours = models.PositiveIntegerField(
        default=72, help_text="How long a till may keep selling without internet before it must reconnect.")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Tenant settings"
        constraints = [models.UniqueConstraint(fields=["tenant"], name="uniq_settings_per_tenant")]

    def __str__(self):
        return f"Settings for {self.tenant_id}"


def get_settings(tenant=None):
    """The shop's settings row (created with defaults the first time). `tenant` defaults to the active one."""
    tenant = tenant or get_current_tenant()
    if tenant is None:
        return TenantSettings()          # unsaved defaults: callers outside a tenant still get sane rules
    obj = TenantSettings.all_objects.filter(tenant=tenant).first()
    if obj is None:
        obj, _ = TenantSettings.all_objects.get_or_create(tenant=tenant)
    return obj
