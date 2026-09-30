from django.contrib import admin

from .models import ActivityLog, User
# Register your models here.
@admin.register(User)
class CustomUserAdmin(BaseUserAdmin):
    # Include your custom fields in fieldsets
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Custom Fields', {'fields': ('role', 'pin', 'tenant', 'is_owner')}),
    )
    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        ('Custom Fields', {'fields': ('role', 'pin', 'tenant', 'is_owner')}),
    )
    
admin.site.register(ActivityLog)
