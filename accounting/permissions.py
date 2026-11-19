"""Who may see and change financial data. Tenant isolation is enforced separately by the models."""
from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied

FINANCE_ROLES = ("ADMIN", "ACCOUNTANT")


def can_view_finance(user):
    """Owner, admins and accountants of the shop. Platform superusers are not shop staff."""
    if not user.is_authenticated or user.is_superuser or getattr(user, "tenant_id", None) is None:
        return False
    return bool(user.is_owner or user.role in FINANCE_ROLES)


def can_configure_accounting(user):
    """Chart of accounts, opening balances, accounting basis: owner and admins only."""
    if not user.is_authenticated or user.is_superuser or getattr(user, "tenant_id", None) is None:
        return False
    return bool(user.is_owner or user.role == "ADMIN")


# Branch managers see their own branch only, and only these parts of Financial Management.
MANAGER_AREAS = {"overview", "expenses", "pnl", "cashflow", "analysis", "reports"}


def can_access_area(user, area):
    """All-access roles may open every area. A branch manager may open MANAGER_AREAS (their branch only)."""
    if not user.is_authenticated or user.is_superuser or getattr(user, "tenant_id", None) is None:
        return False
    if can_view_finance(user):
        return True
    if user.role == "MANAGER" and area in MANAGER_AREAS and getattr(user, "branch_id", None):
        if area == "expenses":
            from .services import get_settings
            return get_settings(user.tenant).managers_can_manage_expenses
        return True
    return False


class FinanceAccessMixin(AccessMixin):
    area = "overview"

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if getattr(request, "tenant", None) is None or not can_access_area(request.user, self.area):
            raise PermissionDenied("You do not have permission to view this financial information.")
        return super().dispatch(request, *args, **kwargs)
