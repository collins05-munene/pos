from django.contrib import admin

from .models import Discount


@admin.register(Discount)
class DiscountAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "value", "target", "is_active", "starts_at", "ends_at", "tenant")
    list_filter = ("is_active", "kind", "target")
    search_fields = ("name",)
    filter_horizontal = ("products", "categories", "branches")

    def get_queryset(self, request):
        return Discount.all_objects.all()
