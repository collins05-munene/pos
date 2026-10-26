import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Customer refunds: Refund + RefundItem."""

    dependencies = [
        ('inventory', '0010_alter_purchaseorder_supplier_and_more'),
        ('products', '0008_brand_is_active_category_is_active_and_more'),
        ('sales', '0014_order_offline_sync'),
        ('tenants', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Refund',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('refund_number', models.CharField(max_length=30)),
                ('reason', models.CharField(choices=[('WRONG_ITEM', 'Wrong item sold'), ('DEFECTIVE', 'Damaged / defective'), ('EXPIRED', 'Expired or near-expiry product'), ('CHANGED_MIND', 'Customer changed their mind'), ('OVERCHARGED', 'Overcharged / price error'), ('OTHER', 'Other')], max_length=20)),
                ('notes', models.TextField(blank=True, default='')),
                ('total_amount', models.DecimalField(decimal_places=2, help_text='Cash paid back to the customer', max_digits=12)),
                ('cogs_reversed', models.DecimalField(decimal_places=2, default=0, help_text='Cost of the goods that went back on the shelf', max_digits=12)),
                ('profit_reversed', models.DecimalField(decimal_places=2, default=0, help_text='Profit given up: refund minus cost recovered', max_digits=12)),
                ('shop_name', models.CharField(max_length=150)),
                ('branch_name', models.CharField(blank=True, default='', max_length=150)),
                ('processed_by_name', models.CharField(max_length=150)),
                ('client_uuid', models.UUIDField(blank=True, editable=False, null=True)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('branch', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, related_name='refunds', to='inventory.branch')),
                ('cash_session', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='refunds', to='sales.cashregistersession')),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='refunds', to='sales.order')),
                ('processed_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='refunds_processed', to=settings.AUTH_USER_MODEL)),
                ('tenant', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='%(app_label)s_%(class)s_set', to='tenants.tenant')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['tenant', '-created_at'], name='sales_refun_tenant__88e825_idx')],
                'constraints': [
                    models.UniqueConstraint(fields=('tenant', 'refund_number'), name='uniq_tenant_refund_number'),
                    models.UniqueConstraint(condition=models.Q(('client_uuid__isnull', False)), fields=('tenant', 'client_uuid'), name='uniq_tenant_refund_client_uuid'),
                ],
            },
        ),
        migrations.CreateModel(
            name='RefundItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(max_length=255)),
                ('sku', models.CharField(blank=True, default='', max_length=100)),
                ('quantity', models.PositiveIntegerField()),
                ('unit_price', models.DecimalField(decimal_places=2, help_text='What the customer paid per unit', max_digits=12)),
                ('unit_cost', models.DecimalField(decimal_places=2, max_digits=12)),
                ('restocked', models.BooleanField(default=True, help_text='Went back into stock (False = written off)')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('cogs_reversed', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('order_item', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='refund_items', to='sales.orderitem')),
                ('refund', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='sales.refund')),
                ('tenant', models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='%(app_label)s_%(class)s_set', to='tenants.tenant')),
                ('variant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='products.productvariant')),
            ],
            options={},
        ),
    ]
