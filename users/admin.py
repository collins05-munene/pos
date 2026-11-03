from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import ActivityLog, PasswordResetRequest, User

# Register your models here.
@admin.register(User)
class CustomUserAdmin(BaseUserAdmin):
    # Include your custom fields in fieldsets
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Custom Fields', {'fields': ('role', 'tenant', 'is_owner')}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Custom Fields', {'fields': ('role', 'tenant', 'is_owner')}),
    )
    
admin.site.register(ActivityLog)


@admin.register(PasswordResetRequest)
class PasswordResetRequestAdmin(admin.ModelAdmin):
    """Read-only audit view. Approve / reject from the platform dashboard, not here."""
    list_display = ("user", "tenant", "status", "requested_at", "decided_by", "decided_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("user__username", "tenant__name", "tenant__slug")
    readonly_fields = [f.name for f in PasswordResetRequest._meta.fields]

    def has_add_permission(self, request):
        return False
