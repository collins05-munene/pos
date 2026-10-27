import json

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django_ratelimit.decorators import ratelimit

from billing import services as billing_services
from billing.models import Invoice
from . import branches as rules
from .rendering import html_or_json


def _is_boss(user):
    """Owner or admin: may add/archive/restore (adding can cost money)."""
    return user.is_superuser or user.is_owner or user.role == user.Roles.ADMIN


def _payload(b):
    return {
        "id": b.pk, "name": b.name, "code": b.code, "location": b.location, "phone": b.phone,
        "is_main": b.is_main, "status": b.status,
        "staff_count": b.staff.filter(is_active=True).count(),
    }


def _body(request):
    if request.content_type == "application/json":
        try:
            return json.loads(request.body or b"{}")
        except ValueError:
            return {}
    return request.POST


def _err(exc, status=400):
    return JsonResponse({"errors": {"__all__": exc.messages}}, status=status)


class BranchAccessMixin(LoginRequiredMixin):
    """Owner, admin and manager. Cashiers have no branch screens."""

    def dispatch(self, request, *args, **kwargs):
        u = request.user
        if u.is_authenticated and (not u.tenant_id or not u.can_switch_branch):
            raise PermissionDenied("Branch management is for owners, admins and managers.")
        return super().dispatch(request, *args, **kwargs)


class BranchListView(BranchAccessMixin, View):
    def get(self, request):
        tenant = request.tenant
        data = {
            "shop": {"name": tenant.name, "slug": tenant.slug},
            "branches": [_payload(b) for b in rules.tenant_branches(tenant)],
            "summary": {k: (str(v) if not isinstance(v, (int, str)) else v)
                        for k, v in rules.branch_summary(tenant).items()},
            "active_branch_id": request.branch.pk if request.branch else None,
            "can_add": _is_boss(request.user),
        }
        return html_or_json(request, "tenants/branches.html", data)

    @method_decorator(ratelimit(key="user", rate="20/m", method="POST", block=True))
    def post(self, request):
        """Add a branch. Past the paid allowance it waits for payment and an invoice is issued."""
        if not _is_boss(request.user):
            raise PermissionDenied("Only the owner or an admin can add branches.")
        d = _body(request)
        try:
            branch = rules.create_branch(request.tenant, name=d.get("name"), code=d.get("code", ""),
                                         location=d.get("location", ""), phone=d.get("phone", ""))
            invoice = None
            if branch.awaiting_payment:
                invoice = billing_services.issue_branch_upgrade(request.tenant)
                branch.refresh_from_db()   # credit may have paid it on the spot
        except (ValidationError, billing_services.BillingError) as exc:
            return _err(exc if isinstance(exc, ValidationError) else ValidationError(str(exc)))
        out = {"branch": _payload(branch)}
        if invoice is not None and invoice.status == Invoice.Status.OPEN:
            out["invoice"] = {"number": invoice.number, "total": str(invoice.total),
                              "amount_due": str(billing_services.amount_due(invoice)),
                              "is_upgrade": invoice.is_upgrade}
            out["next"] = "/billing/"
        return JsonResponse(out, status=201)


class BranchActionView(BranchAccessMixin, View):
    """POST /branches/<id>/<update|archive|restore>/"""

    @method_decorator(ratelimit(key="user", rate="30/m", method="POST", block=True))
    def post(self, request, pk, action):
        branch = get_object_or_404(rules.tenant_branches(request.tenant), pk=pk)
        d = _body(request)
        try:
            if action == "update":
                rules.update_branch(branch, name=d.get("name"), code=d.get("code", ""),
                                    location=d.get("location", ""), phone=d.get("phone", ""))
            elif action in ("archive", "restore"):
                if not _is_boss(request.user):
                    raise PermissionDenied("Only the owner or an admin can do that.")
                if action == "archive":
                    rules.archive_branch(branch)
                else:
                    rules.restore_branch(branch)
                    if branch.awaiting_payment:
                        billing_services.issue_branch_upgrade(request.tenant)
                        branch.refresh_from_db()
            else:
                return JsonResponse({"error": "unknown_action"}, status=404)
        except ValidationError as exc:
            return _err(exc)
        return JsonResponse({"branch": _payload(branch)})


class BranchSwitchView(BranchAccessMixin, View):
    def post(self, request):
        try:
            b = rules.switch_branch(request, _body(request).get("branch_id"))
        except ValidationError as exc:
            return _err(exc, 403)
        return JsonResponse({"branch": _payload(b)})
