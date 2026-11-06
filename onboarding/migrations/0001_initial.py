import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def hide_guide_for_existing_shops(apps, schema_editor):
    """
    Shops that exist before this feature shipped are already up and running: no forced redirect and
    no dashboard banner for them. (The 'Setup Guide' sidebar link still appears until their data shows
    the basics are in place, and it disappears by itself.)
    """
    Tenant = apps.get_model("tenants", "Tenant")
    Progress = apps.get_model("onboarding", "OnboardingProgress")
    now = timezone.now()
    Progress.objects.bulk_create([
        Progress(tenant_id=pk, welcomed_at=now, dismissed_at=now)
        for pk in Tenant.objects.values_list("pk", flat=True)
    ])


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("tenants", "0005_alter_tenantsettings_refund_window_days"),
    ]

    operations = [
        migrations.CreateModel(
            name="OnboardingProgress",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("skipped_steps", models.JSONField(blank=True, default=list, help_text="Optional steps the owner chose to do later, e.g. ['brand'].")),
                ("welcomed_at", models.DateTimeField(blank=True, null=True, help_text="The one-time redirect to the guide has already happened.")),
                ("dismissed_at", models.DateTimeField(blank=True, null=True, help_text="The owner hid the dashboard reminder.")),
                ("completed_at", models.DateTimeField(blank=True, null=True, help_text="Every step was finished. Stops all further checks.")),
                ("tenant", models.ForeignKey(db_index=True, editable=False, on_delete=django.db.models.deletion.PROTECT, related_name="%(app_label)s_%(class)s_set", to="tenants.tenant")),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="onboardingprogress",
            constraint=models.UniqueConstraint(fields=("tenant",), name="uniq_onboarding_per_tenant"),
        ),
        migrations.RunPython(hide_guide_for_existing_shops, migrations.RunPython.noop),
    ]
