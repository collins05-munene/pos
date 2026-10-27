from django.db import migrations, models


def mark_main_branches(apps, schema_editor):
    """Each existing shop's oldest branch becomes its main branch."""
    Branch = apps.get_model("inventory", "Branch")
    seen = set()
    for b in Branch._base_manager.order_by("tenant_id", "pk"):
        if b.tenant_id not in seen:
            seen.add(b.tenant_id)
            Branch._base_manager.filter(pk=b.pk).update(is_main=True)


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0010_alter_purchaseorder_supplier_and_more"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="branch",
            options={"ordering": ["-is_main", "name"], "verbose_name_plural": "Branches"},
        ),
        migrations.AddField(model_name="branch", name="code",
                            field=models.CharField(blank=True, help_text="Short code, e.g. NRB, WLK.", max_length=10)),
        migrations.AddField(model_name="branch", name="phone",
                            field=models.CharField(blank=True, max_length=20)),
        migrations.AddField(model_name="branch", name="is_main",
                            field=models.BooleanField(default=False, help_text="The shop's first branch. Cannot be archived.")),
        migrations.AddField(model_name="branch", name="awaiting_payment",
                            field=models.BooleanField(default=False)),
        migrations.AddField(model_name="branch", name="created_at",
                            field=models.DateTimeField(auto_now_add=True, null=True)),
        migrations.RunPython(mark_main_branches, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="branch",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_main", True)), fields=("tenant",),
                name="one_main_branch_per_tenant"),
        ),
    ]
