from django.core.management.base import BaseCommand

from backups.services import run_due_backups


class Command(BaseCommand):
    help = "Create automatic backups for every tenant whose schedule is due. Run hourly from cron."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Back up every active tenant now.")

    def handle(self, *args, **opts):
        results = run_due_backups(force=opts["force"])
        for tenant, b in results:
            self.stdout.write(f"{tenant.slug}: {b.status} ({b.record_count} records, {b.size_bytes} bytes) {b.error}")
        self.stdout.write(self.style.SUCCESS(f"{len(results)} backup(s) attempted."))