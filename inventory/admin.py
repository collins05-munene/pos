from django.contrib import admin

from . import models


@admin.register(models.Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "tenant", "is_main", "is_active", "awaiting_payment")
    list_filter = ("is_active", "awaiting_payment", "is_main")
    search_fields = ("name", "code", "tenant__name", "tenant__slug")


class TransferItemInline(admin.TabularInline):
    model = models.InventoryTransferItem
    extra = 0
    readonly_fields = ("variant", "quantity", "quantity_received")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(models.InventoryTransfer)
class InventoryTransferAdmin(admin.ModelAdmin):
    """Read-only on purpose: stock only moves through the Transfers screens (inventory/transfers.py)."""
    list_display = ("transfer_number", "from_branch", "to_branch", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("transfer_number",)
    inlines = [TransferItemInline]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
admin.site.register(models.PurchaseOrder)
admin.site.register(models.PurchaseOrderItem)
admin.site.register(models.StockAdjustment)
admin.site.register(models.StockLevel)
