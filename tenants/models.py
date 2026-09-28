from django.conf import settings
from django.db import models, transaction
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
class TenantManager(models.Manager):
    """
    Default manager for every tenant-owned model. FAIL-CLOSED: with no active
    tenant it returns an empty queryset instead of everyone's data.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        tenant = get_current_tenant()
        if tenant is None:
            return qs.none()
        return qs.filter(tenant_id=tenant.pk)

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

    objects      -> scoped to the active tenant (use this everywhere in views)
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
