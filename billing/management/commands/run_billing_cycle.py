from django.core.management.base import BaseCommand

from billing.services import run_billing_cycle


class Command(BaseCommand):
    help = "Daily job: past-due / suspend / cancel transitions and renewal invoices."

    def handle(self, *args, **options):
        self.stdout.write(f"billing cycle: {run_billing_cycle() or 'nothing to do'}")
