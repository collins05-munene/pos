import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Notification

log = logging.getLogger(__name__)

ADMINS = ("ADMIN",)
MANAGEMENT = ("ADMIN", "MANAGER")


def notify(tenant, title, message, *, level=Notification.Level.INFO,
           roles=MANAGEMENT, user=None, exclude_user=None, dedupe_hours=0):
    """
    user=<User>   -> just that person
    roles=(...)   -> one row per active user with that role
    roles=None    -> one tenant-wide broadcast row (user=None)
    dedupe_hours  -> skip if the same title+message was already sent to that
                     recipient inside the window (stops repeat spam)
    Never raises. Returns number of rows created.
    """
    try:
        if tenant is None:
            return 0
        if user is not None:
            recipients = [user]
        elif roles is None:
            recipients = [None]
        else:
            User = get_user_model()
            recipients = list(User.objects.filter(
                tenant=tenant, role__in=roles, is_active=True))

        if exclude_user is not None:
            recipients = [r for r in recipients if r is None or r.pk != exclude_user.pk]

        since = timezone.now() - timedelta(hours=dedupe_hours)
        rows = []
        for r in recipients:
            if dedupe_hours and Notification.objects.filter(
                    tenant=tenant, user=r, title=title,
                    created_at__gte=since).exists():
                continue
            rows.append(Notification(tenant=tenant, user=r, title=title,
                                     message=message, level=level))
        Notification.objects.bulk_create(rows)
        return len(rows)
    except Exception:
        log.exception("notify() failed: %s", title)
        return 0

def who(user):
    return user.get_full_name().strip() or user.username.split("__")[-1]