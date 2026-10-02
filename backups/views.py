from datetime import date

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView
from django_ratelimit.decorators import ratelimit

from users.mixins import AdminRequiredMixin
from users.utils import log_action

from .forms import BackupScheduleForm
from .models import Backup, BackupSchedule
from .services import BackupError, build_export_zip, create_backup, read_backup_gzip


def _schedule(tenant):
    return BackupSchedule.objects.get_or_create(tenant=tenant)[0]


def _password_ok(request, what):
    """Downloads/exports hand over the whole business: re-check the admin's password."""
    if request.user.check_password(request.POST.get("password", "")):
        return True
    log_action(request.user, "Backup Access Denied", f"Wrong password on {what}", request)
    messages.error(request, "Incorrect password. Nothing was downloaded.")
    return False


def _file_response(data, content_type, filename):
    resp = HttpResponse(data, content_type=content_type)
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp["Cache-Control"] = "no-store"
    return resp


class BackupListView(AdminRequiredMixin, TemplateView):
    template_name = "backups/backup_list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        schedule = _schedule(self.request.tenant)
        ctx.update(
            backups=Backup.objects.select_related("created_by")[:30],   # scoped to tenant
            schedule=schedule,
            form=kwargs.get("form") or BackupScheduleForm(instance=schedule),
            next_run=schedule.next_run_at(),
            encrypted=bool(getattr(settings, "BACKUP_ENCRYPTION_KEY", "")),
        )
        return ctx


@method_decorator(ratelimit(key="user", rate="5/h", method="POST", block=True), name="post")
class BackupCreateView(AdminRequiredMixin, View):
    def post(self, request):
        backup = create_backup(request.tenant, Backup.Kind.MANUAL, user=request.user)
        if backup.status == Backup.Status.SUCCESS:
            log_action(request.user, "Backup Created", f"Manual backup #{backup.pk}", request)
            messages.success(request, "Backup created. You can download it below.")
        else:
            messages.error(request, "Backup failed. Please try again or contact support.")
        return redirect("backup-list")


class BackupScheduleView(AdminRequiredMixin, View):
    def post(self, request):
        form = BackupScheduleForm(request.POST, instance=_schedule(request.tenant))
        if form.is_valid():
            form.save()
            log_action(request.user, "Backup Schedule Updated", str(form.cleaned_data), request)
            messages.success(request, "Backup schedule saved.")
        else:
            messages.error(request, "Invalid schedule: " + "; ".join(
                e for errs in form.errors.values() for e in errs))
        return redirect("backup-list")


@method_decorator(ratelimit(key="user", rate="10/h", method="POST", block=True), name="post")
class BackupDownloadView(AdminRequiredMixin, View):
    """POST (password required). Stored file stays encrypted; the admin receives a decrypted .json.gz."""

    def post(self, request, pk):
        backup = get_object_or_404(Backup, pk=pk, status=Backup.Status.SUCCESS)  # tenant-scoped
        if not _password_ok(request, f"download of backup #{backup.pk}"):
            return redirect("backup-list")
        try:
            data = read_backup_gzip(backup)
        except BackupError as exc:
            messages.error(request, str(exc))
            return redirect("backup-list")
        log_action(request.user, "Backup Downloaded", f"Backup #{backup.pk}", request)
        name = f"{request.tenant.slug}-backup-{backup.created_at:%Y%m%d-%H%M}.json.gz"
        return _file_response(data, "application/gzip", name)


@method_decorator(ratelimit(key="user", rate="5/h", method="POST", block=True), name="post")
class BackupExportView(AdminRequiredMixin, View):
    """POST (password required). Spreadsheet export (zip of CSVs), built on demand."""

    def post(self, request):
        if not _password_ok(request, "spreadsheet export"):
            return redirect("backup-list")
        data = build_export_zip(request.tenant)
        log_action(request.user, "Data Exported", "Spreadsheet export (CSV zip)", request)
        name = f"{request.tenant.slug}-data-export-{date.today():%Y%m%d}.zip"
        return _file_response(data, "application/zip", name)


class BackupDeleteView(AdminRequiredMixin, View):
    def post(self, request, pk):
        backup = get_object_or_404(Backup, pk=pk)
        log_action(request.user, "Backup Deleted", f"Backup #{backup.pk}", request)
        backup.file.delete(save=False)
        backup.delete()
        messages.success(request, "Backup deleted.")
        return redirect("backup-list")