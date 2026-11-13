import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Paystack replaces M-Pesa for subscription payments. The M-Pesa tables stay (history only)."""

    dependencies = [
        ("billing", "0008_receipt_verify_code_unique"),
        ("tenants", "__first__"),
    ]

    operations = [
        migrations.AlterField(
            model_name="receipt",
            name="payment_method",
            field=models.CharField(choices=[("PAYSTACK", "Paystack"), ("MPESA_STK", "M-Pesa (STK push)"), ("MPESA_MANUAL", "M-Pesa (paid to till/paybill)"), ("CREDIT", "Account credit"), ("OTHER", "Other")], default="OTHER", max_length=14),
        ),
        migrations.AlterField(
            model_name="receipt",
            name="payment_reference",
            field=models.CharField(blank=True, help_text="Paystack reference(s) (or legacy M-Pesa codes), comma separated.", max_length=255),
        ),
        migrations.CreateModel(
            name="PaystackPayment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reference", models.CharField(max_length=64, unique=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                ("currency", models.CharField(default="KES", max_length=3)),
                ("payer_email", models.EmailField(blank=True, max_length=254)),
                ("status", models.CharField(choices=[("INITIATED", "Waiting for customer"), ("SUCCESS", "Paid"), ("FAILED", "Failed"), ("CANCELLED", "Not completed")], db_index=True, default="INITIATED", max_length=10)),
                ("authorization_url", models.URLField(blank=True, max_length=500)),
                ("access_code", models.CharField(blank=True, max_length=64)),
                ("paystack_id", models.BigIntegerField(blank=True, null=True)),
                ("channel", models.CharField(blank=True, help_text="card, mobile_money, bank, ...", max_length=30)),
                ("gateway_response", models.CharField(blank=True, max_length=255)),
                ("paid_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("raw_event", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("invoice", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="paystack_payments", to="billing.invoice")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="paystack_payments", to="tenants.tenant")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddField(
            model_name="billingcredit",
            name="paystack_payment",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="credits", to="billing.paystackpayment"),
        ),
    ]
