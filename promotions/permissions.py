from tenants.models import get_settings


def is_admin_user(user):
    """Admin, shop owner or platform superuser: may change shop-wide rules."""
    return bool(user.is_authenticated and (user.is_superuser or user.is_owner or user.role == "ADMIN"))


def can_manage_discounts(user, tenant=None):
    """Admins always; managers only while Settings allows it. Cashiers never."""
    if not user.is_authenticated:
        return False
    if is_admin_user(user):
        return True
    return user.role == "MANAGER" and get_settings(tenant or user.tenant).managers_can_manage_discounts
