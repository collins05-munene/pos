"""
Phase-A -> Phase-B bridge: put every pre-existing row and user into ONE tenant,
and give that tenant free access so existing customers are not locked out.
"""
from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from billing.models import Subscription
from tenants.models import Tenant, TenantOwnedModel, generate_unique_slug


class Command(BaseCommand):
    help = "Assign all un-owned rows to a 'legacy' tenant with permanent complimentary access."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Business name of the existing customer")
        parser.add_argument("--business-type", default="MINI_MART")
        parser.add_argument("--comp-years", type=int, default=50)

    @transaction.atomic
    def handle(self, *args, **o):
        tenant = Tenant.objects.filter(name=o["name"]).first() or Tenant.objects.create(
            name=o["name"], slug=generate_unique_slug(o["name"]), business_type=o["business_type"])
        forever = timezone.now() + timedelta(days=365 * o["comp_years"])
        Subscription.objects.get_or_create(
            tenant=tenant,
            defaults={"status": Subscription.Status.ACTIVE, "comp_until": forever,
                      "current_period_end": forever, "override_note": "Legacy customer (pre-SaaS)"},
        )
        User = get_user_model()
        n = User.objects.filter(tenant__isnull=True, is_superuser=False).update(tenant=tenant)
        self.stdout.write(f"users -> {tenant.slug}: {n}")
        for model in apps.get_models():
            if issubclass(model, TenantOwnedModel) and not model._meta.abstract:
                n = model.all_objects.filter(tenant__isnull=True).update(tenant=tenant)
                self.stdout.write(f"{model._meta.label}: {n}")
        first_admin = User.objects.filter(tenant=tenant, role="ADMIN").order_by("id").first()
        if first_admin and not User.objects.filter(tenant=tenant, is_owner=True).exists():
            first_admin.is_owner = True
            first_admin.save(update_fields=["is_owner"])
            self.stdout.write(f"owner set: {first_admin.username}")
        self.stdout.write(self.style.SUCCESS(f"Legacy tenant ready: {tenant.slug}"))
