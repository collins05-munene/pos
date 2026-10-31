from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _user_fk(related_name):
    return models.ForeignKey(
        blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
        related_name=related_name, to=settings.AUTH_USER_MODEL)


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0011_branch_details'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='inventorytransfer',
            options={'ordering': ['-created_at']},
        ),
        migrations.AddField(model_name='inventorytransfer', name='dispatched_by',
                            field=_user_fk('dispatched_transfers')),
        migrations.AddField(model_name='inventorytransfer', name='dispatched_at',
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='inventorytransfer', name='received_by',
                            field=_user_fk('received_transfers')),
        migrations.AddField(model_name='inventorytransfer', name='received_at',
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='inventorytransfer', name='cancelled_by',
                            field=_user_fk('cancelled_transfers')),
        migrations.AddField(model_name='inventorytransfer', name='cancelled_at',
                            field=models.DateTimeField(blank=True, null=True)),
        # Whole numbers could not express 2.5 kg or half a box; match the stock columns.
        migrations.AlterField(
            model_name='inventorytransferitem', name='quantity',
            field=models.DecimalField(decimal_places=3, help_text='Quantity sent', max_digits=12),
        ),
        migrations.AddField(
            model_name='inventorytransferitem', name='quantity_received',
            field=models.DecimalField(
                decimal_places=3, default=Decimal('0'), max_digits=12,
                help_text='Quantity that actually arrived; set when the transfer is received.'),
        ),
    ]
