from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications.models import Notification
from notifications.utils import notify, ADMINS
from tenants.models import Tenant


class Command(BaseCommand):
    help = "Time-based notifications. Run once a day."

    def handle(self, *args, **opts):
        today = timezone.localdate()
        for tenant in Tenant.objects.select_related("subscription"):
            sub = getattr(tenant, "subscription", None)
            if not sub:
                continue
            end = sub.current_period_end
            end = end.date() if hasattr(end, "date") else end

            if not tenant.has_access:
                notify(tenant, "Subscription expired",
                       "Your system is locked. Pay from Billing to restore access for your team.",
                       level=Notification.Level.ERROR, roles=ADMINS, dedupe_hours=72)
            elif end and (end - today).days in (7, 3, 1):
                days = (end - today).days
                what = "ends" if sub.cancel_at_period_end else "renews"
                notify(tenant, f"Subscription {what} in {days} day{'s' if days != 1 else ''}",
                       f"Your current period ends on {end:%d %b %Y}. Pay before then to avoid being locked out.",
                       level=Notification.Level.WARNING, roles=ADMINS, dedupe_hours=20)