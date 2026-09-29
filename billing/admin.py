from django.contrib import admin
from .models import BillingEvent, Invoice, MpesaPayment, Subscription


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("tenant", "status", "term_months", "current_period_end", "comp_until", "custom_monthly_price")
    list_filter = ("status",)
    search_fields = ("tenant__name", "tenant__slug")


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "tenant", "total", "status", "issued_at", "paid_at")
    list_filter = ("status",)
    search_fields = ("number", "tenant__name")


@admin.register(MpesaPayment)
class MpesaPaymentAdmin(admin.ModelAdmin):
    list_display = ("mpesa_receipt", "tenant", "amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("mpesa_receipt", "phone", "checkout_request_id")
    readonly_fields = [f.name for f in MpesaPayment._meta.fields]


@admin.register(BillingEvent)
class BillingEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "type", "tenant")
    list_filter = ("type",)
    readonly_fields = ("tenant", "type", "payload", "created_at")
