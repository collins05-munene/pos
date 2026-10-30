from django.contrib import admin
from .models import (BillingCredit, BillingEvent, Invoice, ManualPaymentRequest, MpesaCode, MpesaPayment,
                     Receipt, Subscription)


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("tenant", "status", "term_months", "current_period_end", "credit", "comp_until",
                    "custom_monthly_price")
    list_filter = ("status",)
    search_fields = ("tenant__name", "tenant__slug")

    @admin.display(description="Credit (KES)")
    def credit(self, obj):
        from .services import credit_balance
        return credit_balance(obj.tenant)


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



@admin.register(MpesaCode)
class MpesaCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "amount", "sender_name", "sender_phone", "created_at", "used_at", "used_by_tenant", "is_void")
    list_filter = ("is_void",)
    search_fields = ("code", "sender_phone", "sender_name")
    readonly_fields = ("created_at", "added_by", "used_at", "used_by_tenant", "used_invoice")


@admin.register(ManualPaymentRequest)
class ManualPaymentRequestAdmin(admin.ModelAdmin):
    list_display = ("created_at", "tenant", "code", "status", "invoice")
    list_filter = ("status",)
    search_fields = ("code", "tenant__name", "tenant__slug")
    readonly_fields = [f.name for f in ManualPaymentRequest._meta.fields]


@admin.register(BillingCredit)
class BillingCreditAdmin(admin.ModelAdmin):
    list_display = ("created_at", "tenant", "amount", "kind", "invoice", "mpesa_code")
    list_filter = ("kind",)
    search_fields = ("tenant__name", "tenant__slug", "note")


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ("number", "customer_name", "amount", "payment_method", "payment_reference", "paid_at")
    list_filter = ("payment_method",)
    search_fields = ("number", "customer_name", "payment_reference", "invoice__number", "tenant__slug")
    readonly_fields = [f.name for f in Receipt._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
