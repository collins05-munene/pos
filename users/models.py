from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import RegexValidator
from django.db import models

from tenants.context import get_current_tenant
from tenants.models import TenantOwnedModel


class TenantUserManager(UserManager):
    """User.tenant_objects: only the active tenant's staff. Use it in staff-management screens."""
    use_in_migrations = False

    def get_queryset(self):
        qs = super().get_queryset()
        tenant = get_current_tenant()
        return qs.none() if tenant is None else qs.filter(tenant_id=tenant.pk)


class User(AbstractUser):
    class Roles(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        MANAGER = "MANAGER", "Manager"
        CASHIER = "CASHIER", "Cashier"

    role = models.CharField(max_length=15, choices=Roles.choices, default=Roles.CASHIER)
    
    pin = models.CharField(
        max_length=128, blank=True, null=True,
        validators=[RegexValidator(r'^\d{4,6}$', 'Pin must be 4  to 6 digits')],
        help_text="Hashed 4-6 digit PIN for cashier login.",
    )

    # NULL only for platform superusers.
    tenant = models.ForeignKey("tenants.Tenant", null=True, blank=True,
                               on_delete=models.PROTECT, related_name="users")
    is_owner = models.BooleanField(default=False,
                                   help_text="The business owner: the only user who sees billing.")

    objects = UserManager()              # unscoped: authentication needs it
    tenant_objects = TenantUserManager()

    class Meta:
        # DB-level guarantees, active once the backfill is done (TENANCY_ALLOW_NULL_TENANT off).
        constraints = [] if getattr(settings, "TENANCY_ALLOW_NULL_TENANT", False) else [
            models.CheckConstraint(
                condition=models.Q(is_superuser=True) | models.Q(tenant__isnull=False),
                name="user_has_tenant_unless_superuser"),
            models.UniqueConstraint(
                fields=["tenant"], condition=models.Q(is_owner=True),
                name="one_owner_per_tenant"),
        ]

    def save(self, *args, **kwargs):
        if self.pin and not self.pin.startswith('pbkdf2_'):
            self.pin = make_password(self.pin)
        super().save(*args, **kwargs)

    @property
    def is_admin(self):
        return self.role == self.Roles.ADMIN or self.is_superuser

    @property
    def is_manager(self):
        return self.role == self.Roles.MANAGER


class ActivityLog(TenantOwnedModel):
    # Overrides the abstract field: failed logins / platform events have no tenant.
    tenant = models.ForeignKey("tenants.Tenant", null=True, blank=True, editable=False,
                               on_delete=models.SET_NULL, related_name="activity_logs")
    tenant_required = False

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=255)
    details = models.TextField(blank=True, null=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"User: {self.user} IP: {self.ip_address} Action: {self.action} at {self.timestamp}"
