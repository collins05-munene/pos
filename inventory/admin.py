from django.contrib import admin

from . import models


@admin.register(models.Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "tenant", "is_main", "is_active", "awaiting_payment")
    list_filter = ("is_active", "awaiting_payment", "is_main")
    search_fields = ("name", "code", "tenant__name", "tenant__slug")


admin.site.register(models.InventoryTransfer)
admin.site.register(models.InventoryTransferItem)
admin.site.register(models.PurchaseOrder)
admin.site.register(models.PurchaseOrderItem)
admin.site.register(models.StockAdjustment)
admin.site.register(models.StockLevel)
