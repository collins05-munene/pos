import re

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .context import tenant_context
from .models import Tenant, generate_unique_slug

HANDLE_RE = re.compile(r"^[a-z0-9._-]{2,30}$")
PIN_RE = re.compile(r"^\d{4,6}$")


def compose_username(business_code, handle):
    """
    Staff usernames are globally unique in Django's auth table, so they are
    stored as '<business_code>__<handle>'. The login form shows two fields
    (Business code + Username) and composes this string.
    """
    return f"{business_code.strip().lower()}__{handle.strip().lower()}"


@transaction.atomic
def register_business(*, business_name, business_type, owner_name, email, phone,
                      password, term_months):
    """Public sign-up: tenant + owner + default branch + subscription + first invoice."""
    from billing.services import start_subscription  # local import: avoids app-load cycle

    User = get_user_model()
    email = email.lower().strip()
    first, _, last = owner_name.strip().partition(" ")

    tenant = Tenant.objects.create(
        name=business_name.strip(),
        slug=generate_unique_slug(business_name),
        business_type=business_type,
        phone=phone,
        email=email,
    )
    try:
        with transaction.atomic():
            owner = User.objects.create_user(
                username=email, email=email, password=password,
                first_name=first, last_name=last,
                role=User.Roles.ADMIN, is_owner=True, tenant=tenant,
                is_staff=False, is_superuser=False,   # never Django-admin capable
            )
    except IntegrityError:
        raise ValidationError("An account with this email already exists.")

    with tenant_context(tenant):
        provision_workspace(tenant, owner)

    _subscription, invoice = start_subscription(tenant, term_months)
    return tenant, owner, invoice


def provision_workspace(tenant, owner):
    """
    Runs inside tenant_context(tenant). Put every "new shop starts with..." default
    here: main branch now; default categories, walk-in customer, chart of accounts,
    pharmacy settings later (branch on tenant.business_type).
    """
    from inventory.models import Branch

    Branch.objects.create(name="Main Branch")


def create_staff_user(*, tenant, handle, role, first_name="", password=None, pin=None):
    """Owner/admin creates a cashier or manager inside their own tenant."""
    User = get_user_model()
    handle = handle.strip().lower()
    if not HANDLE_RE.match(handle):
        raise ValidationError("Username: 2-30 chars of a-z, 0-9, dot, dash, underscore.")
    if pin is not None and not PIN_RE.match(pin):
        raise ValidationError("PIN must be 4 to 6 digits.")
    if role not in User.Roles.values:
        raise ValidationError("Unknown role.")

    user = User(username=compose_username(tenant.slug, handle), first_name=first_name,
                role=role, tenant=tenant)
    if password:
        user.set_password(password)
    else:
        user.set_unusable_password()
    if pin:
        user.pin = pin  # hashed by User.save()
    try:
        user.save()
    except IntegrityError:
        raise ValidationError("That username is already taken in your business.")
    return user
