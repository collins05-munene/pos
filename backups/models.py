from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.files.storage import storages
from django.db import models
from django.utils import timezone

from tenants.models import TenantOwnedModel


def backup_storage():
    """Private storage alias 'backups' (see settings snippet). Never MEDIA_ROOT."""
    return storages["backups"]


class Backup(TenantOwnedModel):
    class Kind(models.TextChoices):
        MANUAL = "MANUAL", "Manual"
        AUTO = "AUTO", "Automatic"

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    file = models.FileField(storage=backup_storage, upload_to="", max_length=255, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    record_count = models.PositiveIntegerField(default=0)
    checksum = models.CharField(max_length=64, blank=True, help_text="SHA-256 of the stored file")
    encrypted = models.BooleanField(default=False)
    error = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} backup {self.created_at:%Y-%m-%d %H:%M} ({self.status})"


class BackupSchedule(TenantOwnedModel):
    class Frequency(models.TextChoices):
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"

    enabled = models.BooleanField(default=True)
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.DAILY)
    hour = models.PositiveSmallIntegerField(default=2, help_text="Run after this hour (0-23, local time).")
    keep_last = models.PositiveSmallIntegerField(default=14, help_text="Automatic backups to keep.")
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["tenant"], name="uniq_backup_schedule_per_tenant")]

    def next_run_at(self):
        if self.last_run_at is None:
            return None
        days = 1 if self.frequency == self.Frequency.DAILY else 7
        day = timezone.localtime(self.last_run_at).date() + timedelta(days=days)
        return timezone.make_aware(datetime.combine(day, time(self.hour)))

    def is_due(self, now=None):
        if not self.enabled:
            return False
        now = now or timezone.now()
        nxt = self.next_run_at()
        return nxt is None or now >= nxt