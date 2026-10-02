from django.views.generic import CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied

from .utils import log_action, AuditAction


class AuditLogMixin:
    """
    Automatically creates audit logs for CreateView, UpdateView
    and DeleteView.

    """

    def get_audit_label(self, obj):
        """
        Returns a human-readable description of the object.
        Example:
            Category: John Doe
            Product: Paracetamol
        """
        model_name = self.model._meta.verbose_name.title()
        return f"{model_name}: {obj}"

    def get_audit_action(self):
        """
        Determines the audit action based on the view type.
        """
        if isinstance(self, CreateView):
            return AuditAction.RECORD_CREATE

        if isinstance(self, UpdateView):
            return AuditAction.RECORD_UPDATE

        if isinstance(self, DeleteView):
            return AuditAction.RECORD_DELETE

        return None

    def form_valid(self, form):
        """
        Handles CreateView and UpdateView.
        """
        response = super().form_valid(form)

        action = self.get_audit_action()

        if action:
            log_action(
                user=self.request.user,
                action=action,
                details=self.get_audit_label(self.object),
                request=self.request
            )

        return response

    def delete(self, request, *args, **kwargs):
        """
        Handles DeleteView.
        """
        self.object = self.get_object()

        label = self.get_audit_label(self.object)

        response = super().delete(request, *args, **kwargs)

        log_action(
            user=request.user,
            action=AuditAction.RECORD_DELETE,
            details=f"{label} (id={self.object.pk})",
            request=request
        )

        return response


MANAGEMENT_ROLES = ("ADMIN", "MANAGER")


class AdminRequiredMixin(AccessMixin):
    """
    CBV mixin that verifies the current user is authenticated AND is a
    management user (ADMIN or MANAGER), or a superuser.
    """
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        user = request.user
        if getattr(user, "role", None) in MANAGEMENT_ROLES or user.is_superuser:
            return super().dispatch(request, *args, **kwargs)

        raise PermissionDenied("You do not have administrative permission to access this page.")


class CashierRequiredMixin(AccessMixin):
    """
    CBV mixin that verifies the current user is authenticated AND is a
    CASHIER, ADMIN or MANAGER, or a superuser.
    """
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        user = request.user
        if getattr(user, "role", None) in ("CASHIER",) + MANAGEMENT_ROLES or user.is_superuser:
            return super().dispatch(request, *args, **kwargs)

        raise PermissionDenied("You do not have cashier permissions to access this page.")

class TenantScopedQuerysetMixin:
    """
    Explicitly restricts a view's queryset to the request's tenant.
    Does not depend on the model's default manager. If there is no tenant
    (e.g. a superuser outside a support session), it returns nothing.
    """
    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        base = self.model._base_manager      
        if tenant is None:
            return base.none()
        return base.filter(tenant_id=tenant.pk)