from django.db import migrations, models


class Migration(migrations.Migration):
    """Adds the WAITER and KITCHEN roles (restaurants / hotels). Choices only: no schema change.

    Chained after 0009_alter_user_role_accountant so the role list keeps ACCOUNTANT.
    """

    dependencies = [
        ("users", "0009_alter_user_role_accountant"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="role",
            field=models.CharField(
                choices=[("ADMIN", "Admin"), ("MANAGER", "Manager"), ("CASHIER", "Cashier"),
                         ("ACCOUNTANT", "Accountant"), ("WAITER", "Waiter"), ("KITCHEN", "Kitchen")],
                default="CASHIER", max_length=15),
        ),
    ]
