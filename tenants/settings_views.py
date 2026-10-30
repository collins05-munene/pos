from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from notifications.utils import ADMINS, notify, who
from users.mixins import MANAGEMENT_ROLES
from users.utils import AuditAction, log_action

from .forms import TenantSettingsForm
from .models import get_settings
from .settings_registry import SECTIONS, SHORTCUTS


def _is_admin(user):
    return bool(user.is_superuser or user.is_owner or user.role == "ADMIN")


class SettingsView(LoginRequiredMixin, View):
    """
    One home for the shop's rules. Admins/owner can change them; managers can look (and reach the
    pages they are allowed to use, like Discounts); cashiers are turned away.
    """
    template_name = "tenants/settings.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and request.user.role not in MANAGEMENT_ROLES and not request.user.is_superuser:
            raise PermissionDenied("Settings are for managers and admins.")
        return super().dispatch(request, *args, **kwargs)

    def _render(self, request, form, status=200):
        editable = _is_admin(request.user)
        sections = []
        for s in SECTIONS:
            link = None
            if s["link"]:
                try:
                    link = {"url": reverse(s["link"][0]), "label": s["link"][1]}
                except Exception:
                    link = None
            sections.append({**s, "bound": [form[n] for n in s["fields"]], "link": link})
        shortcuts = []
        for label, desc, name in SHORTCUTS:
            if not editable and name not in ("activity-logs",) and name.split(":")[0] in ("staff", "branches", "backup-list"):
                continue          # owner/admin-only pages are not advertised to managers
            try:
                shortcuts.append({"label": label, "desc": desc, "url": reverse(name)})
            except Exception:
                pass
        return render(request, self.template_name,
                      {"form": form, "sections": sections, "editable": editable, "shortcuts": shortcuts,
                       "saved": request.GET.get("saved") == "1"}, status=status)

    def get(self, request):
        form = TenantSettingsForm(instance=get_settings(request.tenant))
        if not _is_admin(request.user):
            for f in form.fields.values():
                f.disabled = True
        return self._render(request, form)

    def post(self, request):
        if not _is_admin(request.user):
            raise PermissionDenied("Only an admin can change settings.")
        instance = get_settings(request.tenant)
        form = TenantSettingsForm(request.POST, instance=instance)
        if not form.is_valid():
            return self._render(request, form, status=400)
        before = {n: getattr(instance, n) for n in form.changed_data}
        form.save()
        if form.changed_data:
            changes = "; ".join(f"{n}: {before[n]} -> {getattr(instance, n)}" for n in form.changed_data)
            log_action(request.user, AuditAction.SETTINGS_CHANGED, changes[:900], request)
            notify(request.tenant, "Shop settings changed",
                   f"{who(request.user)} changed: {', '.join(form.changed_data)}.",
                   roles=ADMINS, exclude_user=request.user)
        return redirect(f"{reverse('settings')}?saved=1")
