import products.models
from django.db import migrations, models


class Migration(migrations.Migration):
    """Optional per-variant photo: file paths only (the compressed files live in storage)."""

    dependencies = [
        ('products', '0009_product_images'),
    ]

    operations = [
        migrations.AddField(
            model_name='productvariant',
            name='image',
            field=models.FileField(blank=True, max_length=255, upload_to=products.models.product_image_path),
        ),
        migrations.AddField(
            model_name='productvariant',
            name='thumb',
            field=models.FileField(blank=True, max_length=255, upload_to=products.models.product_image_path),
        ),
        migrations.AddField(
            model_name='productvariant',
            name='image_token',
            field=models.CharField(blank=True, help_text='Content hash of the current image; busts browser caches.', max_length=16),
        ),
    ]
