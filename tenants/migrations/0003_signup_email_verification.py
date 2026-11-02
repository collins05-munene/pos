from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0002_discounts"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tenant",
            name="business_type",
            field=models.CharField(
                choices=[
                    ("MINI_MART", "Mini Mart"), ("PHARMACY", "Pharmacy"),
                    ("HARDWARE", "Hardware"), ("AGROVET", "Agrovet"), ("OTHER", "Other"),
                ],
                default="MINI_MART", max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="PendingSignup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254, unique=True)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("password_hash", models.CharField(max_length=128)),
                ("data", models.JSONField(help_text="Business name/type, owner name, phone, term, branches.")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("last_sent_at", models.DateTimeField()),
            ],
        ),
    ]
