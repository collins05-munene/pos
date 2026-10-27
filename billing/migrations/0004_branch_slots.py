from django.db import migrations, models


def backfill_slots(apps, schema_editor):
    """Existing shops keep what they already pay for: slots = branches they currently have (>=1)."""
    Subscription = apps.get_model("billing", "Subscription")
    Branch = apps.get_model("inventory", "Branch")
    Invoice = apps.get_model("billing", "Invoice")
    for sub in Subscription.objects.all():
        n = max(Branch._base_manager.filter(tenant_id=sub.tenant_id).count(), 1)
        Subscription.objects.filter(pk=sub.pk).update(branch_slots=n)
        # Invoices still open were priced before branches cost money; keep their slot count in
        # line so paying one never lowers what the shop already has.
        Invoice.objects.filter(tenant_id=sub.tenant_id, status="OPEN").update(branch_slots=n)


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0003_credit_and_partial_payments"),
        ("inventory", "0011_branch_details"),
    ]

    operations = [
        migrations.AddField(model_name="subscription", name="branch_slots",
                            field=models.PositiveSmallIntegerField(default=1, help_text="Branches this shop has paid for (1 is included in the base price).")),
        migrations.AddField(model_name="invoice", name="branch_slots",
                            field=models.PositiveSmallIntegerField(default=1, help_text="Branches this invoice covers; applied to the subscription when paid.")),
        migrations.AddField(model_name="invoice", name="is_upgrade",
                            field=models.BooleanField(default=False, help_text="Mid-term add-on (extra branches). Paying it never extends the period.")),
        migrations.RunPython(backfill_slots, migrations.RunPython.noop),
    ]
