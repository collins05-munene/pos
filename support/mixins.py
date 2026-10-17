from django.contrib import messages
from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django_ratelimit.core import is_ratelimited

from users.mixins import MANAGEMENT_ROLES


def is_management(user):
    return getattr(user, "role", None) in MANAGEMENT_ROLES or user.is_superuser


class TenantRequiredMixin:
    """Put AFTER CashierRequiredMixin/AdminRequiredMixin so login is checked first."""

    def dispatch(self, request, *args, **kwargs):
        if getattr(request, "tenant", None) is None:
            raise PermissionDenied("No active business selected.")
        return super().dispatch(request, *args, **kwargs)


class PostRateLimitMixin:
    """Per-user throttle on POSTs so tickets, replies and votes can't be spammed."""

    ratelimit_rate = "20/h"

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST" and request.user.is_authenticated and is_ratelimited(
            request, group=f"support:{type(self).__name__}", key="user",
            rate=self.ratelimit_rate, method="POST", increment=True,
        ):
            messages.error(request, "You're sending requests too quickly. Please wait a few minutes and try again.")
            return redirect(request.path)
        return super().dispatch(request, *args, **kwargs)


class SupportStaffRequiredMixin(AccessMixin):
    """Your own support team (Django staff accounts), not tenant admins."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not (request.user.is_staff and request.user.is_active):
            raise PermissionDenied("This page is for the support team.")
        return super().dispatch(request, *args, **kwargs)
