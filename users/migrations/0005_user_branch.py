import django.db.models.deletion
from django.db import migrations, models


def pin_cashiers_to_main(apps, schema_editor):
    """Before this, every user silently worked at the shop's first branch. Make that explicit."""
    User = apps.get_model("users", "User")
    Branch = apps.get_model("inventory", "Branch")
    for b in Branch._base_manager.filter(is_main=True):
        User.objects.filter(tenant_id=b.tenant_id, branch__isnull=True).update(branch_id=b.pk)


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0004_user_national_id_user_phone"),
        ("inventory", "0011_branch_details"),
    ]

    operations = [
        migrations.AddField(
            model_name="user", name="branch",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="staff", to="inventory.branch",
                help_text="Cashiers work at exactly this branch. Managers/admins start here but can switch."),
        ),
        migrations.RunPython(pin_cashiers_to_main, migrations.RunPython.noop),
    ]
