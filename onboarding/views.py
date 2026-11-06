from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from users.mixins import AdminRequiredMixin
from users.utils import AuditAction, log_action

from . import services
from .forms import BrandStepForm, CategoryStepForm, ProductStepForm

FORMS = {"category": CategoryStepForm, "brand": BrandStepForm, "product": ProductStepForm}


class GuideBase(AdminRequiredMixin, View):
    def dispatch(self, request, *args, **kwargs):
        # Platform superusers outside a support session have no shop to set up.
        if request.user.is_authenticated and getattr(request, "tenant", None) is None:
            raise PermissionDenied("No active business selected.")
        return super().dispatch(request, *args, **kwargs)


def _render_guide(request, *, active=None, bound=None, status=200):
    progress = services.get_progress()
    keys = [s.key for s in progress.steps]
    if active not in keys:
        current = progress.current
        active = current.key if current else None

    forms_ = {key: cls() for key, cls in FORMS.items()}
    if bound is not None:
        forms_[active] = bound

    from products.models import Category
    ctx = {
        "progress": progress,
        "steps": progress.steps,
        "active": active,
        "forms": forms_,
        "has_custom_category": Category.objects.exclude(slug="general").exists(),
        "stock_url": services.stock_link_for_latest_product(),
    }
    return render(request, "onboarding/guide.html", ctx, status=status)


class GuideView(GuideBase):
    def get(self, request):
        services.record_if_complete()
        return _render_guide(request, active=request.GET.get("step"))


class StepSubmitView(GuideBase):
    """POST /getting-started/step/<category|brand|product>/ -- creates the thing, then back to the guide."""

    def post(self, request, step):
        form_cls = FORMS.get(step)
        if form_cls is None:
            return redirect("onboarding:guide")

        form = form_cls(request.POST)
        if not form.is_valid():
            return _render_guide(request, active=step, bound=form, status=400)

        cd = form.cleaned_data
        if step == "category":
            obj, created = services.create_category(cd["name"])
            label = "Category"
        elif step == "brand":
            obj, created = services.create_brand(cd["name"], cd.get("description", ""))
            label = "Brand"
        else:
            obj, _variant = services.create_first_product(
                name=cd["name"], category=cd["category"], brand=cd["brand"],
                unit_of_measure=cd["unit_of_measure"], cost_price=cd["cost_price"],
                retail_price=cd["retail_price"], sku=cd.get("sku", ""))
            created, label = True, "Product"

        if created:
            log_action(request.user, AuditAction.RECORD_CREATE,
                       f"{label} created from the setup guide: {obj}", request)
            messages.success(request, self._success_message(step, obj))
        else:
            messages.info(request, f"You already have a {label.lower()} called '{obj}', so we used that one.")

        progress = services.record_if_complete()
        if progress.complete:
            return redirect(f"{reverse('onboarding:guide')}?done=1")
        return redirect("onboarding:guide")

    @staticmethod
    def _success_message(step, obj):
        return {
            "category": f"Category '{obj}' added.",
            "brand": f"Brand '{obj}' added.",
            "product": f"'{obj}' is now in your product list. Next, bring some of it into stock so it can be sold.",
        }[step]


class SkipStepView(GuideBase):
    def post(self, request, step):
        if step in services.SKIPPABLE:
            services.skip_step(step)
            messages.info(request, "No problem. You can come back to this any time from the sidebar.")
            progress = services.record_if_complete()
            if progress.complete:
                return redirect(f"{reverse('onboarding:guide')}?done=1")
        return redirect("onboarding:guide")


class DismissView(GuideBase):
    """Hide the dashboard reminder. The sidebar link stays until setup is finished."""

    def post(self, request):
        services.dismiss_reminder()
        messages.info(request, "Reminder hidden. Find 'Setup Guide' in the sidebar whenever you want to carry on.")
        return redirect("admin-dashboard")


class RestoreView(GuideBase):
    def post(self, request):
        services.restore_reminder()
        return redirect("onboarding:guide")
