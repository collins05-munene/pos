from django.core.management.base import BaseCommand

from billing.services import reconcile_paystack_payments, run_billing_cycle


class Command(BaseCommand):
    help = "Daily job: past-due / suspend / cancel transitions, renewal invoices, and a re-check of unfinished Paystack payments."

    def handle(self, *args, **options):
        self.stdout.write(f"billing cycle: {run_billing_cycle() or 'nothing to do'}")
        self.stdout.write(f"paystack payments settled by re-check: {reconcile_paystack_payments()}")
