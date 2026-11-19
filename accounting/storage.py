"""Expense receipts can show supplier and bank details, so they live outside MEDIA_ROOT (like support
attachments) and are only served through an authenticated, tenant-checked view."""
import os
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def receipt_storage():
    root = getattr(settings, "SUPPORT_UPLOAD_ROOT", None) or os.path.join(settings.BASE_DIR, "private_uploads")
    return FileSystemStorage(location=str(root))


def receipt_path(instance, filename):
    ext = os.path.splitext(filename)[1].lower()
    return f"expense-receipts/{instance.tenant_id}/{uuid.uuid4().hex}{ext}"
