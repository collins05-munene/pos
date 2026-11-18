from django.db import migrations, models


class Migration(migrations.Migration):
    """Adds the ACCOUNTANT role. Choices only: no schema change, existing users untouched."""

    dependencies = [
        ("users", "0008_remove_pin_add_password_reset"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("ADMIN", "Admin"), ("MANAGER", "Manager"), ("CASHIER", "Cashier"),
                         ("ACCOUNTANT", "Accountant")],
                default="CASHIER", max_length=15),
        ),
    ]
