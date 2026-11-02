from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0005_user_branch"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="email_verified_at",
            field=models.DateTimeField(
                blank=True, null=True,
                help_text="When the owner confirmed their email address from the sign-up link."),
        ),
    ]
