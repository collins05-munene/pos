from django.db import migrations


def pin_hash_becomes_password(apps, schema_editor):
    """
    Cashiers used to sign in with a 4-6 digit PIN (stored hashed in `pin`) and had NO usable password.
    Everyone now signs in with username + password, so carry each cashier's existing PIN hash over
    as their password. They keep signing in exactly as before (their PIN is now their password) and
    nobody is locked out on deploy. Both fields hold Django-format hashes, so the copy is lossless.

    The owner should replace these weak, PIN-strength passwords from the Staff page.
    """
    User = apps.get_model("users", "User")
    for user in User.objects.filter(role="CASHIER").exclude(pin__isnull=True).exclude(pin=""):
        unusable = (not user.password) or user.password.startswith("!")
        if unusable:
            user.password = user.pin
            user.save(update_fields=["password"])


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0006_user_email_verified_at"),
    ]

    operations = [
        migrations.RunPython(pin_hash_becomes_password, migrations.RunPython.noop),
    ]
