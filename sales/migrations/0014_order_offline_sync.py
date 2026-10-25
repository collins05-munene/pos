from django.db import migrations, models


class Migration(migrations.Migration):
    """Offline selling: idempotency key + audit fields on Order."""

    dependencies = [
        ("sales", "0013_receipt"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="client_uuid",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="is_offline_sale",
            field=models.BooleanField(default=False, help_text="Rung up with no connection and synced later."),
        ),
        migrations.AddField(
            model_name="order",
            name="synced_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="offline_flags",
            field=models.JSONField(blank=True, default=list, help_text="Anything a manager should look at: negative stock, price changes, clock skew ..."),
        ),
        migrations.AddConstraint(
            model_name="order",
            constraint=models.UniqueConstraint(
                condition=models.Q(("client_uuid__isnull", False)),
                fields=("tenant", "client_uuid"),
                name="uniq_tenant_order_client_uuid",
            ),
        ),
    ]
