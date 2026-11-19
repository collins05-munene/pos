from django.contrib import admin

from .models import Account, AccountingSettings, FinancialAuditLog, FiscalPeriod, JournalEntry, JournalLine


class PlatformAdmin(admin.ModelAdmin):
    """Platform superusers sit outside any shop, so the tenant-scoped default manager would show nothing."""
    def get_queryset(self, request):
        return self.model.all_objects.all()


class ReadOnlyAdmin(PlatformAdmin):
    """Ledger data is append-only; the admin may look but not edit or delete."""
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(JournalEntry)
class JournalEntryAdmin(ReadOnlyAdmin):
    list_display = ("reference", "tenant", "entry_date", "description", "status", "source_type")
    list_filter = ("status", "source_type")
    search_fields = ("reference", "description", "source_key")


@admin.register(JournalLine)
class JournalLineAdmin(ReadOnlyAdmin):
    list_display = ("entry", "account", "debit", "credit")


@admin.register(FinancialAuditLog)
class FinancialAuditLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "tenant", "user", "action", "object_type", "object_id")
    list_filter = ("action",)


@admin.register(Account)
class AccountAdmin(PlatformAdmin):
    list_display = ("code", "name", "tenant", "account_type", "is_active")
    list_filter = ("account_type", "is_active")


admin.site.register(AccountingSettings, PlatformAdmin)
admin.site.register(FiscalPeriod, PlatformAdmin)
