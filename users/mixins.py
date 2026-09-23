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


class AdminRequiredMixin(AccessMixin):
    """
    CBV mixin that verifies the current user is authenticated AND has the 'ADMIN' role.
    """
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        # Check role attribute or method (supports user.role == 'ADMIN' or user.is_admin)
        user_role = getattr(request.user, 'role', None)
        is_admin_flag = getattr(request.user, 'is_admin', False)

        if user_role == 'ADMIN' or is_admin_flag or request.user.is_superuser:
            return super().dispatch(request, *args, **kwargs)

        raise PermissionDenied("You do not have administrative permission to access this page.")


class CashierRequiredMixin(AccessMixin):
    """
    CBV mixin that verifies the current user is authenticated AND has either 'CASHIER' or 'ADMIN' role.
    (Admins are typically permitted to access cashier functions as well).
    """
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        user_role = getattr(request.user, 'role', None)

        # Allow CASHIERs or ADMINs / Superusers
        if user_role in ['CASHIER', 'ADMIN'] or request.user.is_superuser:
            return super().dispatch(request, *args, **kwargs)

        raise PermissionDenied("You do not have cashier permissions to access this page.")