import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Step 1 of 3: add the columns (verify_code not unique yet, so existing rows can be filled in)."""

    dependencies = [
        ('billing', '0005_receipt'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(model_name='receipt', name='verify_code',
                            field=models.CharField(blank=True, default='', max_length=12)),
        migrations.AddField(model_name='receipt', name='is_void',
                            field=models.BooleanField(default=False, help_text='Receipt withdrawn (e.g. payment reversed). The verify page says so.')),
        migrations.AddField(model_name='receipt', name='void_reason',
                            field=models.CharField(blank=True, max_length=255)),
        migrations.AddField(model_name='receipt', name='voided_at',
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='receipt', name='voided_by',
                            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
    ]
