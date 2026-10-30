from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from notifications.utils import ADMINS, notify, who
from products.models import Product
from tenants.models import get_settings
from users.mixins import AdminRequiredMixin
from users.utils import AuditAction, log_action

from .forms import DiscountForm
from .models import Discount
from .permissions import can_manage_discounts


class DiscountWriteMixin(AdminRequiredMixin):
    """Management roles only, and managers only while Settings allows it."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_manage_discounts(request.user, request.tenant):
            raise PermissionDenied("Only admins can manage discounts in this shop.")
        return super().dispatch(request, *args, **kwargs)


class DiscountListView(AdminRequiredMixin, ListView):
    model = Discount
    template_name = "promotions/discount_list.html"
    context_object_name = "discounts"
    paginate_by = 20

    STATUSES = ("running", "scheduled", "ended", "off")

    def get_queryset(self):
        now = timezone.now()
        given = ExpressionWrapper(F("order_items__discount_per_unit") * F("order_items__quantity"),
                                  output_field=DecimalField(max_digits=14, decimal_places=2))
        qs = (Discount.objects.annotate(
                  times_used=Count("order_items__order", distinct=True),
                  total_given=Coalesce(Sum(given), Decimal("0.00"),
                                       output_field=DecimalField(max_digits=14, decimal_places=2)))
              .prefetch_related("products", "categories", "branches")
              .order_by("-is_active", "-starts_at", "name", "pk"))
        status = self.request.GET.get("status", "")
        if status == "running":
            qs = qs.filter(is_active=True, starts_at__lte=now).filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
        elif status == "scheduled":
            qs = qs.filter(is_active=True, starts_at__gt=now)
        elif status == "ended":
            qs = qs.filter(is_active=True, ends_at__lte=now)
        elif status == "off":
            qs = qs.filter(is_active=False)
        q = " ".join(self.request.GET.get("q", "").split())[:100]
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(products__name__icontains=q)
                           | Q(categories__name__icontains=q)).distinct()
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        shop = get_settings(self.request.tenant)
        ctx.update(status=self.request.GET.get("status", ""), q=self.request.GET.get("q", ""),
                   shop=shop, can_manage=can_manage_discounts(self.request.user, self.request.tenant))
        return ctx


class _DiscountFormMixin(DiscountWriteMixin):
    model = Discount
    form_class = DiscountForm
    template_name = "promotions/discount_form.html"
    success_url = reverse_lazy("discount-list")

    def get_form_kwargs(self):
        kw = super().get_form_kwargs()
        kw.update(user=self.request.user, tenant=self.request.tenant)
        return kw

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["shop"] = get_settings(self.request.tenant)
        ctx["ceiling"] = ctx["form"].ceiling
        return ctx


class DiscountCreateView(_DiscountFormMixin, CreateView):
    def get_initial(self):
        initial = super().get_initial()
        initial["starts_at"] = timezone.localtime().replace(second=0, microsecond=0)
        pid = self.request.GET.get("product")
        if pid and str(pid).isdigit():
            product = Product.objects.filter(pk=pid).first()
            if product:
                initial.update(target=Discount.Target.PRODUCTS, products=[product], name=f"{product.name} offer")
        return initial

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        d = self.object
        log_action(self.request.user, AuditAction.DISCOUNT_CREATED, f"{d.name}: {d.value_label}", self.request)
        if self.request.user.role != "ADMIN" and not self.request.user.is_owner:
            notify(self.request.tenant, "Discount created",
                   f"{who(self.request.user)} created '{d.name}' ({d.value_label}).",
                   roles=ADMINS, exclude_user=self.request.user)
        messages.success(self.request, f"Discount '{d.name}' saved.")
        return response


class DiscountUpdateView(_DiscountFormMixin, UpdateView):
    def form_valid(self, form):
        response = super().form_valid(form)
        d = self.object
        log_action(self.request.user, AuditAction.DISCOUNT_UPDATED,
                   f"{d.name}: {d.value_label} (changed: {', '.join(form.changed_data) or 'nothing'})", self.request)
        messages.success(self.request, f"Discount '{d.name}' updated.")
        return response


class DiscountToggleView(DiscountWriteMixin, View):
    """POST: switch a discount on/off without opening the form."""

    def post(self, request, pk):
        d = get_object_or_404(Discount, pk=pk)
        d.is_active = not d.is_active
        d.save(update_fields=["is_active", "updated_at"])
        log_action(request.user, AuditAction.DISCOUNT_SWITCHED,
                   f"{d.name} switched {'ON' if d.is_active else 'OFF'}", request)
        messages.success(request, f"'{d.name}' is now {'on' if d.is_active else 'off'}.")
        return redirect(request.POST.get("next") or "discount-list")


class DiscountDeleteView(DiscountWriteMixin, DeleteView):
    model = Discount
    template_name = "promotions/discount_confirm_delete.html"
    success_url = reverse_lazy("discount-list")

    def form_valid(self, form):
        d = self.object
        label = f"{d.name}: {d.value_label}"
        response = super().form_valid(form)       # past sales keep the discount's name and amounts
        log_action(self.request.user, AuditAction.DISCOUNT_DELETED, label, self.request)
        messages.success(self.request, f"Discount '{d.name}' deleted. Past sales are unchanged.")
        return response
