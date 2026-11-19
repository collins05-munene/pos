"""
Run the routine accounting upkeep for every business that has started its books:

    python manage.py accounting_housekeeping            # all businesses
    python manage.py accounting_housekeeping --tenant 5

Creates due repeating expenses, posts finished months of depreciation and posts any sale, refund,
supplier payment, stock adjustment or transfer that is missing from the ledger. Safe to run as often
as you like (hourly or daily from cron): everything it does is idempotent.
"""
import datetime

from django.core.management.base import BaseCommand

from accounting import integration as ig
from accounting import operations as ops
from accounting.models import AccountingSettings


class Command(BaseCommand):
    help = "Generate repeating expenses, post depreciation and catch up the ledger."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", type=int, help="Only this business (id).")

    def handle(self, *args, **opts):
        qs = AccountingSettings.all_objects.filter(opening_balance_date__isnull=False).select_related("tenant")
        if opts.get("tenant"):
            qs = qs.filter(tenant_id=opts["tenant"])
        last_month_end = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
        for cfg in qs:
            t = cfg.tenant
            recurring = ops.generate_recurring_expenses(t)
            depreciation = ops.generate_depreciation(t, up_to=last_month_end)
            caught_up = sum(ig.sync_ledger(t).values())
            self.stdout.write(f"{t}: {recurring} repeating expense(s), {depreciation} depreciation month(s), {caught_up} catch-up posting(s)")
        self.stdout.write(self.style.SUCCESS("Accounting housekeeping complete."))
