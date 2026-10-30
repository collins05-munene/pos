import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0004_branch_slots'),
        ('tenants', '0002_discounts'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Receipt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('number', models.CharField(blank=True, max_length=20, unique=True)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('paid_at', models.DateTimeField(db_index=True)),
                ('payment_method', models.CharField(choices=[('MPESA_STK', 'M-Pesa (STK push)'), ('MPESA_MANUAL', 'M-Pesa (paid to till/paybill)'), ('CREDIT', 'Account credit'), ('OTHER', 'Other')], default='OTHER', max_length=14)),
                ('payment_reference', models.CharField(blank=True, help_text='M-Pesa code(s), comma separated.', max_length=255)),
                ('payer_name', models.CharField(blank=True, max_length=120)),
                ('payer_phone', models.CharField(blank=True, max_length=20)),
                ('customer_name', models.CharField(max_length=150)),
                ('customer_phone', models.CharField(blank=True, max_length=20)),
                ('customer_email', models.EmailField(blank=True, max_length=254)),
                ('issued_at', models.DateTimeField(auto_now_add=True)),
                ('generated_by', models.ForeignKey(blank=True, help_text='Empty = created automatically when the payment landed.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('invoice', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='receipt', to='billing.invoice')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='receipts', to='tenants.tenant')),
            ],
            options={
                'ordering': ['-paid_at', '-id'],
            },
        ),
    ]
