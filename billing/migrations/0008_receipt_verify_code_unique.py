from django.db import migrations, models

import billing.models


class Migration(migrations.Migration):
    """Step 3 of 3: now that every row has a code, make it unique."""

    dependencies = [('billing', '0007_receipt_backfill_codes')]

    operations = [
        migrations.AlterField(model_name='receipt', name='verify_code',
                              field=models.CharField(blank=True, default=billing.models.new_verify_code, max_length=12, unique=True)),
    ]
