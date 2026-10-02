import csv
import gzip
import hashlib
import io
import json
import logging
import zipfile

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.files.base import ContentFile
from django.core.serializers.json import DjangoJSONEncoder
from django.core.management.color import no_style
from django.db import connection, transaction
from django.utils import timezone
from django.utils.text import slugify

from tenants.context import tenant_context          # same helper your M-Pesa note refers to
from tenants.models import Tenant, TenantOwnedModel
from users.models import User

from .models import Backup, BackupSchedule

log = logging.getLogger(__name__)
FORMAT_VERSION = 1


class BackupError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def _fernet():
    key = getattr(settings, "BACKUP_ENCRYPTION_KEY", "")
    if not key:
        return None
    from cryptography.fernet import Fernet
    return Fernet(key.encode() if isinstance(key, str) else key)


def tenant_models():
    """Every business-data table: all TenantOwnedModel subclasses except the backup tables."""
    skip = {Backup, BackupSchedule}
    return [m for m in apps.get_models()
            if issubclass(m, TenantOwnedModel) and not m._meta.abstract and m not in skip]


def _user_fields():
    # Never export password / PIN hashes.
    return [f.name for f in User._meta.concrete_fields if f.name not in {"password", "pin"}]


def _dump(qs, **kw):
    return json.loads(serializers.serialize("json", qs, **kw))


# ---------------------------------------------------------------- create
def build_payload(tenant):
    data, counts = {}, {}
    for model in tenant_models():
        rows = _dump(model.all_objects.filter(tenant=tenant).order_by("pk"))
        data[model._meta.label] = rows
        counts[model._meta.label] = len(rows)
    users = _dump(User.objects.filter(tenant=tenant).order_by("pk"), fields=_user_fields())
    data[User._meta.label] = users
    counts[User._meta.label] = len(users)
    return {
        "format_version": FORMAT_VERSION,
        "created_at": timezone.now().isoformat(),
        "tenant": _dump(Tenant.objects.filter(pk=tenant.pk)),
        "counts": counts,
        "data": data,
    }


def create_backup(tenant, kind, user=None):
    with tenant_context(tenant):
        backup = Backup.objects.create(tenant=tenant, kind=kind, created_by=user)
        try:
            payload = build_payload(tenant)
            blob = gzip.compress(json.dumps(payload, cls=DjangoJSONEncoder).encode())
            f = _fernet()
            if f:
                blob = f.encrypt(blob)
            stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
            name = f"{tenant.slug}/{stamp}-{kind.lower()}.json.gz" + (".enc" if f else "")
            backup.file.save(name, ContentFile(blob), save=False)
            backup.encrypted = bool(f)
            backup.size_bytes = len(blob)
            backup.checksum = hashlib.sha256(blob).hexdigest()
            backup.record_count = sum(payload["counts"].values())
            backup.status = Backup.Status.SUCCESS
        except Exception as exc:  # record the failure; never lose the attempt
            log.exception("Backup failed for tenant %s", tenant.pk)
            backup.status = Backup.Status.FAILED
            backup.error = str(exc)[:500]
        backup.completed_at = timezone.now()
        backup.save()
    return backup


def apply_retention(tenant, keep_last):
    with tenant_context(tenant):
        stale = list(Backup.objects.filter(kind=Backup.Kind.AUTO, status=Backup.Status.SUCCESS)
                     .order_by("-created_at").values_list("pk", flat=True)[keep_last:])
        for b in Backup.objects.filter(pk__in=stale):
            b.file.delete(save=False)
            b.delete()


def run_due_backups(force=False):
    """Called by cron. Creates a default schedule for tenants that lack one."""
    results = []
    for tenant in Tenant.objects.filter(is_active=True):
        with tenant_context(tenant):
            schedule, _ = BackupSchedule.objects.get_or_create(tenant=tenant)
            if not (force or schedule.is_due()):
                continue
            backup = create_backup(tenant, Backup.Kind.AUTO)
            if backup.status == Backup.Status.SUCCESS:
                schedule.last_run_at = timezone.now()   # failures retry on the next cron tick
                schedule.save(update_fields=["last_run_at"])
                apply_retention(tenant, schedule.keep_last)
            results.append((tenant, backup))
    return results


# ---------------------------------------------------------------- restore
def read_backup_gzip(backup):
    """Checksum-verified, DEcrypted gzip bytes of a stored backup (what the admin downloads)."""
    with backup.file.open("rb") as fh:
        blob = fh.read()
    if hashlib.sha256(blob).hexdigest() != backup.checksum:
        raise BackupError("Checksum mismatch: the backup file was altered or corrupted.")
    if backup.encrypted:
        f = _fernet()
        if not f:
            raise BackupError("This backup is encrypted but BACKUP_ENCRYPTION_KEY is not set.")
        try:
            blob = f.decrypt(blob)
        except Exception:
            raise BackupError("Could not decrypt this backup (wrong key?).")
    return blob


def load_payload(backup):
    return json.loads(gzip.decompress(read_backup_gzip(backup)))


def restore_missing_rows(backup):
    """
    NON-DESTRUCTIVE restore: re-inserts rows that no longer exist (matched by primary key).
    Never overwrites or deletes anything. Restored staff get an unusable password/PIN, so the
    owner must reset their credentials. For a full point-in-time rollback use a database dump.
    """
    payload = load_payload(backup)
    restored = {}
    touched_models = []

    def missing(model, rows):
        ids = [r["pk"] for r in rows]
        have = set(model._base_manager.filter(pk__in=ids).values_list("pk", flat=True))
        return [r for r in rows if r["pk"] not in have]

    with transaction.atomic():
        tenant_rows = missing(Tenant, payload["tenant"])
        for d in serializers.deserialize("python", tenant_rows):
            d.save()
        tenant = Tenant.objects.get(pk=payload["tenant"][0]["pk"])

        with tenant_context(tenant):
            from django.contrib.auth.hashers import make_password
            for d in serializers.deserialize("python", missing(User, payload["data"][User._meta.label])):
                d.object.password = make_password(None)
                d.object.pin = None
                d.save()
                restored[User._meta.label] = restored.get(User._meta.label, 0) + 1

            for model in tenant_models():
                rows = missing(model, payload["data"].get(model._meta.label, []))
                for d in serializers.deserialize("python", rows):
                    d.save()          # raw save: bypasses TenantOwnedModel.save, keeps original pk
                if rows:
                    restored[model._meta.label] = len(rows)
                    touched_models.append(model)

        if touched_models:
            with connection.cursor() as cur:
                for sql in connection.ops.sequence_reset_sql(no_style(), touched_models):
                    cur.execute(sql)
    return restored


# ---------------------------------------------------------------- spreadsheet export
EXPORT_EXCLUDE = {"tenant", "password", "pin", "is_superuser", "is_staff"}


def _guard(text):
    """Stop spreadsheet formula injection (=, +, -, @ at the start of a text cell)."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _cell(obj, field, cache):
    if field.is_relation and (field.many_to_one or field.one_to_one):
        rel = getattr(obj, field.name)
        if rel is None:
            return ""
        key = (rel._meta.label, rel.pk)
        if key not in cache:
            cache[key] = str(rel)
        return _guard(cache[key])
    if field.choices:
        return _guard(str(getattr(obj, f"get_{field.name}_display")() or ""))
    value = getattr(obj, field.attname)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if hasattr(value, "strftime"):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%Y-%m-%d %H:%M") if hasattr(value, "hour") else value.isoformat()
    if isinstance(value, str):
        return _guard(value)
    return str(value)          # numbers: left as-is so negatives stay numeric


def build_export_zip(tenant):
    """One CSV per table (opens in Excel), zipped. Generated on demand, never stored."""
    buf, used, cache = io.BytesIO(), set(), {}
    sources = [(m, m.all_objects.filter(tenant=tenant)) for m in tenant_models()]
    sources.append((User, User.objects.filter(tenant=tenant)))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for model, qs in sources:
            fields = [f for f in model._meta.concrete_fields if f.name not in EXPORT_EXCLUDE]
            fks = [f.name for f in fields if f.is_relation]
            qs = qs.select_related(*fks).order_by("pk")
            sio = io.StringIO()
            w = csv.writer(sio)
            w.writerow([str(f.verbose_name).title() for f in fields])
            for obj in qs.iterator(chunk_size=2000):
                w.writerow([_cell(obj, f, cache) for f in fields])
            name = slugify(str(model._meta.verbose_name_plural)) or model._meta.model_name
            if name in used:
                name = f"{model._meta.app_label}-{name}"
            used.add(name)
            zf.writestr(f"{name}.csv", sio.getvalue().encode("utf-8-sig"))   # BOM: Excel reads UTF-8
        zf.writestr("README.txt",
                    f"Data export for {tenant.name}\nCreated {timezone.localtime():%Y-%m-%d %H:%M}\n\n"
                    "Each .csv file is one table. Open them with Excel or Google Sheets.\n"
                    "Related records (product, category, user...) are shown by name.\n"
                    "Passwords and PINs are never included.\n")
    return buf.getvalue()