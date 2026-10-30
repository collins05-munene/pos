import secrets

from django.db import migrations

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def fill_codes(apps, schema_editor):
    """Step 2 of 3: every receipt that already exists gets its own random verification code."""
    Receipt = apps.get_model("billing", "Receipt")
    used = set()
    for r in Receipt.objects.filter(verify_code=""):
        code = "".join(secrets.choice(ALPHABET) for _ in range(12))
        while code in used:
            code = "".join(secrets.choice(ALPHABET) for _ in range(12))
        used.add(code)
        Receipt.objects.filter(pk=r.pk).update(verify_code=code)


class Migration(migrations.Migration):

    dependencies = [('billing', '0006_receipt_verification')]

    operations = [migrations.RunPython(fill_codes, migrations.RunPython.noop)]
