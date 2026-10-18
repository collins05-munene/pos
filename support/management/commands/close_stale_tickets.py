from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from support import services
from support.models import Ticket


class Command(BaseCommand):
    help = "Close tickets that have been resolved for N days with no customer reply. Run daily."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7)

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=opts["days"])
        stale = Ticket._base_manager.filter(status=Ticket.Status.RESOLVED, resolved_at__lt=cutoff)
        n = 0
        for t in stale:
            services.update_ticket(t, actor=None, status=Ticket.Status.CLOSED, notify=False)
            n += 1
        self.stdout.write(f"Closed {n} ticket(s).")
