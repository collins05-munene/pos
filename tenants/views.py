import json

from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views import View
from django_ratelimit.decorators import ratelimit

from billing.pricing import quote_table
from .forms import SignupForm
from .mixins import OwnerRequiredMixin
from .rendering import html_or_json
from .services import (
    create_staff_user, list_staff, register_business, set_staff_active,
    set_staff_credential, staff_handle,
)


@method_decorator(ratelimit(key="ip", rate="5/h", method="POST", block=True), name="post")
class SignupView(View):
    def get(self, request):
        """The sign-up page for a browser; pricing JSON for that page's own fetch()."""
        return html_or_json(request, "tenants/signup.html",
                            {"pricing": quote_table(include_install_fee=True)})

    def post(self, request):
        if request.content_type == "application/json":
            try:
                data = json.loads(request.body or b"{}")
            except ValueError:
                return JsonResponse({"error": "invalid_json"}, status=400)
        else:
            data = request.POST

        form = SignupForm(data)
        if not form.is_valid():
            return JsonResponse({"errors": form.errors.get_json_data()}, status=400)

        try:
            tenant, owner, invoice = register_business(**form.cleaned_data)
        except ValidationError as exc:
            return JsonResponse({"errors": {"__all__": exc.messages}}, status=400)

        login(request, owner, backend="django.contrib.auth.backends.ModelBackend")
        return JsonResponse({
            "tenant": {"name": tenant.name, "business_code": tenant.slug},
            "invoice": {"number": invoice.number, "total": str(invoice.total),
                        "term_months": invoice.term_months},
            "next": "/billing/",   # owner is logged in; billing pages are exempt from the lock
        }, status=201)


ALLOWED_STAFF_ROLES = ("CASHIER", "MANAGER")  # ADMIN is reserved for the owner, set at sign-up


def _staff_payload(user):
    return {
        "id": user.pk, "handle": staff_handle(user.tenant, user.username),
        "role": user.role, "is_active": user.is_active, "date_joined": user.date_joined,
        "login_method": "PIN" if user.role == "CASHIER" else "password",
    }


@method_decorator(ratelimit(key="user", rate="30/m", method="POST", block=True), name="post")
class StaffListView(OwnerRequiredMixin, View):
    def get(self, request):
        payload = {
            "shop": {"name": request.tenant.name, "slug": request.tenant.slug},
            "staff": [_staff_payload(u) for u in list_staff(request.tenant)],
        }
        return html_or_json(request, "tenants/staff.html", payload)

    def post(self, request):
        """Add a cashier or manager."""
        handle = (request.POST.get("handle") or "").strip()
        role = (request.POST.get("role") or "").strip().upper()
        first_name = (request.POST.get("first_name") or "").strip()
        credential = request.POST.get("credential") or ""

        if role not in ALLOWED_STAFF_ROLES:
            return JsonResponse({"errors": {"role": ["Choose Cashier or Manager."]}}, status=400)

        kwargs = dict(tenant=request.tenant, handle=handle, role=role, first_name=first_name)
        kwargs["pin" if role == "CASHIER" else "password"] = credential

        try:
            user = create_staff_user(**kwargs)
        except ValidationError as exc:
            field = "credential" if "PIN" in "".join(exc.messages) or "password" in "".join(exc.messages) else "handle"
            return JsonResponse({"errors": {field: exc.messages}}, status=400)
        return JsonResponse({"staff": _staff_payload(user)}, status=201)


class StaffActionView(OwnerRequiredMixin, View):
    """POST /staff/<id>/<action>/ -- reset-credential, activate, deactivate."""

    def post(self, request, pk, action):
        user = get_object_or_404(list_staff(request.tenant), pk=pk)
        try:
            if action == "reset-credential":
                set_staff_credential(user, value=request.POST.get("value") or "")
            elif action == "deactivate":
                set_staff_active(user, is_active=False)
            elif action == "activate":
                set_staff_active(user, is_active=True)
            else:
                return JsonResponse({"error": "unknown_action"}, status=404)
        except ValidationError as exc:
            return JsonResponse({"errors": {"value": exc.messages}}, status=400)
        return JsonResponse({"staff": _staff_payload(user)})