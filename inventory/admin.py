from django.contrib import admin

from . import models
# Register your models here.
admin.site.register(models.Branch)
admin.site.register(models.InventoryTransfer)
admin.site.register(models.InventoryTransferItem)
admin.site.register(models.PurchaseOrder)
admin.site.register(models.PurchaseOrderItem)
admin.site.register(models.StockAdjustment)
admin.site.register(models.StockLevel)