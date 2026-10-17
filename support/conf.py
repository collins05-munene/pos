"""
Support settings, all optional. Override any of these in settings.py.

    APP_VERSION = "2026.10.1"                 # shown to staff on every ticket
    SUPPORT_EMAIL = "support@yourdomain.co.ke"  # team inbox for new tickets / customer replies
    SUPPORT_WHATSAPP_NUMBER = "254712345678"  # international format, digits only
    SUPPORT_TIMEZONE = "Africa/Nairobi"
    SUPPORT_HOURS = {0: (time(8), time(18)), ...}   # Monday = 0
    SITE_URL = "https://app.yourdomain.co.ke" # used for links inside emails
    SUPPORT_UPLOAD_ROOT = "/var/app/private_uploads"  # keep OUTSIDE MEDIA_ROOT
"""
from datetime import time

from django.conf import settings

PRODUCT_NAME = getattr(settings, "SUPPORT_PRODUCT_NAME", "Sellify POS")
APP_VERSION = getattr(settings, "APP_VERSION", "unknown")
SITE_URL = getattr(settings, "SITE_URL", "")
SUPPORT_EMAIL = getattr(settings, "SUPPORT_EMAIL", "")
WHATSAPP_NUMBER = getattr(settings, "SUPPORT_WHATSAPP_NUMBER", "")
TIMEZONE = getattr(settings, "SUPPORT_TIMEZONE", "Africa/Nairobi")

# Monday = 0 ... Sunday = 6. A missing day means closed.
HOURS = getattr(
    settings,
    "SUPPORT_HOURS",
    {
        0: (time(8, 0), time(18, 0)),
        1: (time(8, 0), time(18, 0)),
        2: (time(8, 0), time(18, 0)),
        3: (time(8, 0), time(18, 0)),
        4: (time(8, 0), time(18, 0)),
        5: (time(9, 0), time(13, 0)),
    },
)

# First-reply targets shown to customers, by severity/priority.
RESPONSE_TARGETS = getattr(
    settings,
    "SUPPORT_RESPONSE_TARGETS",
    {
        "CRITICAL": "within 1 hour",
        "HIGH": "within 4 hours",
        "MEDIUM": "within 1 business day",
        "LOW": "within 2 business days",
    },
)
RESPONSE_SUMMARY = getattr(
    settings,
    "SUPPORT_RESPONSE_SUMMARY",
    "Most requests get a first reply within 1 business day.",
)

# Attachment limits
MAX_ATTACHMENTS = 5
MAX_FILE_MB = 5      # images, PDF, text, CSV
MAX_VIDEO_MB = 25    # screen recordings
MAX_TOTAL_MB = 40
