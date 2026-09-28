from django.contrib import admin
from .models import Tenant


class TenantAwareAdmin(admin.ModelAdmin):
    """Mixin for admin classes of tenant-owned models: platform staff see ALL tenants."""

    def get_queryset(self, request):
        qs = self.model.all_objects.all()
        ordering = self.get_ordering(request)
        return qs.order_by(*ordering) if ordering else qs

    def save_model(self, request, obj, form, change):
        if not obj.tenant_id:
            raise ValueError("Create tenant-owned rows inside a tenant, not from the global admin.")
        super().save_model(request, obj, form, change)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "business_type", "is_active", "created_at")
    list_filter = ("business_type", "is_active")
    search_fields = ("name", "slug", "email", "phone")
