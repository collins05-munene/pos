import products.models
from django.db import migrations, models


class Migration(migrations.Migration):
    """Optional product image: file paths only (the compressed files live in storage)."""

    dependencies = [
        ('products', '0008_brand_is_active_category_is_active_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='image',
            field=models.FileField(blank=True, max_length=255, upload_to=products.models.product_image_path),
        ),
        migrations.AddField(
            model_name='product',
            name='thumb',
            field=models.FileField(blank=True, max_length=255, upload_to=products.models.product_image_path),
        ),
        migrations.AddField(
            model_name='product',
            name='image_token',
            field=models.CharField(blank=True, help_text='Content hash of the current image; busts browser caches.', max_length=16),
        ),
    ]
