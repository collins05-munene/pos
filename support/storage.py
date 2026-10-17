import os
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def get_private_storage():
    """
    Support uploads (screenshots can show customer and sales data) live outside
    MEDIA_ROOT and are only served through an authenticated view.
    """
    root = getattr(settings, "SUPPORT_UPLOAD_ROOT", None) or os.path.join(
        settings.BASE_DIR, "private_uploads"
    )
    return FileSystemStorage(location=str(root))


def attachment_path(instance, filename):
    ext = os.path.splitext(filename)[1].lower()
    return f"support/{instance.ticket_id}/{uuid.uuid4().hex}{ext}"
