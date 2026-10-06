from django.contrib import admin

from .models import Backup, BackupSchedule

# Register your models here.
admin.site.register(Backup)
admin.site.register(BackupSchedule)