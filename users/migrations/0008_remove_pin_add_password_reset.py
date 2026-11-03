import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0004_product_images"),
        ("users", "0007_cashier_pin_to_password"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="user",
            name="pin",
        ),
        migrations.CreateModel(
            name="PasswordResetRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(
                    choices=[("PENDING", "Pending"), ("APPROVED", "Approved"),
                             ("REJECTED", "Rejected"), ("COMPLETED", "Completed")],
                    db_index=True, default="PENDING", max_length=10)),
                ("requested_at", models.DateTimeField(auto_now_add=True)),
                ("requested_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_note", models.CharField(blank=True, max_length=255)),
                ("token_hash", models.CharField(blank=True, max_length=64, null=True, unique=True)),
                ("link_expires_at", models.DateTimeField(blank=True, null=True)),
                ("link_sent_to", models.EmailField(blank=True, help_text="Where the approved link was emailed.", max_length=254)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                 related_name="+", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                             related_name="reset_requests", to="tenants.tenant")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name="reset_requests", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-requested_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="passwordresetrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "PENDING")), fields=("user",),
                name="one_pending_reset_per_user"),
        ),
    ]
