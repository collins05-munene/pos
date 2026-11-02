from django.db import migrations, models


class Migration(migrations.Migration):
    """Settings switch for product images (off by default for every existing shop)."""

    dependencies = [
        ('tenants', '0003_signup_email_verification'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenantsettings',
            name='product_images_enabled',
            field=models.BooleanField(default=False, help_text='Lets staff add a photo to each product and shows it on the POS terminal. Off = no image fields or pictures anywhere; photos already added are kept.'),
        ),
    ]
