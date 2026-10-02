from django.core.management.base import BaseCommand, CommandError

from backups.models import Backup
from backups.services import BackupError, restore_missing_rows


class Command(BaseCommand):
    help = "Re-insert rows missing from the database using a backup (non-destructive)."

    def add_arguments(self, parser):
        parser.add_argument("backup_id", type=int)
        parser.add_argument("--yes", action="store_true")

    def handle(self, *args, **opts):
        backup = Backup.all_objects.filter(pk=opts["backup_id"], status="SUCCESS").first()
        if not backup:
            raise CommandError("No successful backup with that id.")
        if not opts["yes"] and input(f"Restore missing rows from {backup}? [y/N] ").lower() != "y":
            return
        try:
            restored = restore_missing_rows(backup)
        except BackupError as exc:
            raise CommandError(str(exc))
        for label, n in restored.items():
            self.stdout.write(f"{label}: {n} row(s) restored")
        self.stdout.write(self.style.SUCCESS("Done. Restored staff must have credentials reset."))