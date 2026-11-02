import json

from django.conf import settings
from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django_ratelimit.decorators import ratelimit
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, render, redirect

from billing import conf
from billing.pricing import quote_table
from .branches import active_branches, assign_user_branch
from .forms import SignupForm, StaffDetailsForm
from .mixins import OwnerRequiredMixin
from .rendering import html_or_json
from .services import (ResendTooSoon, SignupLinkExpired, SignupLinkInvalid, VerificationEmailFailed,
                       complete_signup, create_staff_user, list_staff, resend_verification,
                       set_staff_active, set_staff_credential, staff_handle, start_signup,
                       update_staff_details)
from notifications.models import Notification
from notifications.utils import notify, MANAGEMENT


def _form_errors(form):
    return {f: list(errs) for f, errs in form.errors.items()}


SIGNUP_EMAIL_KEY = "signup_pending_email"


# Generous on purpose: every POST counts, including ones bounced for a typo, and a shop's wifi is often
# shared. What actually protects inboxes from abuse is the per-address cooldown in start_signup().
@method_decorator(ratelimit(key="ip", rate="20/h", method="POST", block=True), name="post")
class SignupView(View):
    def get(self, request):
        """The sign-up page for a browser; pricing JSON for that page's own fetch()."""
        try:
            count = max(min(int(request.GET.get("branches", 1)), conf.get("MAX_BRANCHES")), 1)
        except (TypeError, ValueError):
            count = 1
        return html_or_json(request, "tenants/signup.html", {
            "pricing": quote_table(include_install_fee=True, extra_branches=count - 1),
            "branches": {"count": count, "max": conf.get("MAX_BRANCHES"),
                         "extra_monthly": conf.get("BRANCH_MONTHLY_PRICE")},
        })

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
            start_signup(**form.cleaned_data)
        except ResendTooSoon as exc:
            return JsonResponse({"errors": {"__all__": [
                f"We just emailed that address. Check your inbox (and spam), or try again in {exc.seconds} seconds."]}},
                status=429)
        except VerificationEmailFailed:
            return JsonResponse({"errors": {"__all__": [
                "We couldn't send your confirmation email just now. Please try again in a few minutes."]}},
                status=503)

        request.session[SIGNUP_EMAIL_KEY] = form.cleaned_data["email"]
        return JsonResponse({"email": form.cleaned_data["email"],
                             "next": reverse("tenants:check-email")}, status=202)


class CheckEmailView(View):
    """'We emailed you a link' page, with a resend button."""

    def get(self, request):
        email = request.session.get(SIGNUP_EMAIL_KEY)
        if not email:
            return redirect("tenants:signup")
        return render(request, "tenants/check_email.html", {
            "email": email, "hours": settings.SIGNUP_VERIFY_HOURS, "sent": request.GET.get("sent") == "1",
            "wait": request.GET.get("wait"), "failed": request.GET.get("failed") == "1",
        })


@method_decorator(ratelimit(key="ip", rate="10/h", method="POST", block=True), name="post")
class ResendVerificationView(View):
    def post(self, request):
        email = request.session.get(SIGNUP_EMAIL_KEY)
        if not email:
            return redirect("tenants:signup")
        url = reverse("tenants:check-email")
        try:
            resend_verification(email)
        except ResendTooSoon as exc:
            return redirect(f"{url}?wait={exc.seconds}")
        except VerificationEmailFailed:
            return redirect(f"{url}?failed=1")
        return redirect(f"{url}?sent=1")


class VerifyEmailView(View):
    """The emailed link: creates the shop, signs the owner in, sends them to pay."""

    def get(self, request, token):
        try:
            tenant, owner, invoice = complete_signup(token)
        except SignupLinkExpired:
            return render(request, "tenants/verify_result.html", {"state": "expired"}, status=410)
        except SignupLinkInvalid:
            return render(request, "tenants/verify_result.html", {"state": "invalid"}, status=404)

        login(request, owner, backend="django.contrib.auth.backends.ModelBackend")
        request.session.pop(SIGNUP_EMAIL_KEY, None)

        notify(tenant, f"Welcome to {tenant.name}",
               "Next steps: add your products, record a purchase to bring stock in, "
               "then add your cashiers.", user=owner)
        notify(tenant, "Invoice awaiting payment",
               f"Invoice {invoice.number} for KSH {invoice.total} is ready. Pay from Billing to activate your subscription.",
               level=Notification.Level.WARNING, user=owner)
        return redirect("/billing/")


ALLOWED_STAFF_ROLES = ("CASHIER", "MANAGER")  

def _staff_payload(user):
    return {
        "id": user.pk, "handle": staff_handle(user.tenant, user.username),
        "first_name": user.first_name, "last_name": user.last_name,
        "full_name": user.get_full_name(), "email": user.email, "phone": user.phone,
        "national_id": user.national_id or "",
        "role": user.role, "is_active": user.is_active, "date_joined": user.date_joined,
        "branch_id": user.branch_id, "branch_name": user.branch.name if user.branch_id else "",
        "login_method": "PIN" if user.role == "CASHIER" else "password",
    }


@method_decorator(ratelimit(key="user", rate="30/m", method="POST", block=True), name="post")
class StaffListView(OwnerRequiredMixin, View):
    def get(self, request):
        payload = {
            "shop": {"name": request.tenant.name, "slug": request.tenant.slug},
            "staff": [_staff_payload(u) for u in list_staff(request.tenant).select_related("branch")],
            "branches": [{"id": b.pk, "name": b.name} for b in active_branches(request.tenant)],
        }
        return html_or_json(request, "tenants/staff.html", payload)

    def post(self, request):
        """Add a cashier or manager."""
        handle = (request.POST.get("handle") or "").strip()
        role = (request.POST.get("role") or "").strip().upper()
        credential = request.POST.get("credential") or ""

        if role not in ALLOWED_STAFF_ROLES:
            return JsonResponse({"errors": {"role": ["Choose Cashier or Manager."]}}, status=400)

        form = StaffDetailsForm(request.POST, tenant=request.tenant)
        if not form.is_valid():
            return JsonResponse({"errors": _form_errors(form)}, status=400)

        branch = active_branches(request.tenant).filter(pk=request.POST.get("branch_id") or 0).first()
        if branch is None and role == "CASHIER":
            return JsonResponse({"errors": {"branch_id": ["Choose which branch this cashier works at."]}}, status=400)

        kwargs = dict(tenant=request.tenant, handle=handle, role=role, branch=branch, **form.cleaned_data)
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
            elif action == "set-branch":
                branch = active_branches(request.tenant).filter(pk=request.POST.get("branch_id") or 0).first()
                assign_user_branch(user, branch)
            elif action == "update-details":
                form = StaffDetailsForm(request.POST, tenant=request.tenant, instance=user)
                if not form.is_valid():
                    return JsonResponse({"errors": _form_errors(form)}, status=400)
                update_staff_details(user, **form.cleaned_data)
            else:
                return JsonResponse({"error": "unknown_action"}, status=404)
        except ValidationError as exc:
            return JsonResponse({"errors": {"value": exc.messages}}, status=400)
        return JsonResponse({"staff": _staff_payload(user)})


class ProfileView(LoginRequiredMixin, View):
    """Any signed-in user (owner, manager, cashier) sees and edits their own details."""
    template_name = "tenants/profile.html"

    def _render(self, request, form, status=200):
        u = request.user
        return render(request, self.template_name, {
            "form": form,
            "handle": staff_handle(u.tenant, u.username) if u.tenant_id else u.username,
            "id_locked": bool(u.national_id),
            "saved": request.GET.get("saved") == "1",
        }, status=status)

    def get(self, request):
        u = request.user
        return self._render(request, StaffDetailsForm(tenant=u.tenant, instance=u, require_id=False))

    def post(self, request):
        u = request.user
        form = StaffDetailsForm(request.POST, tenant=u.tenant, instance=u, require_id=False)
        if not form.is_valid():
            return self._render(request, form, status=400)
        data = form.cleaned_data
        data["national_id"] = u.national_id or data["national_id"]
        try:
            update_staff_details(u, **data)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self._render(request, form, status=400)
        return redirect(f"{reverse('tenants:my-profile')}?saved=1")