import hashlib
import logging
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .context import tenant_context
from .models import PendingSignup, Tenant, generate_unique_slug

log = logging.getLogger(__name__)

HANDLE_RE = re.compile(r"^[a-z0-9._-]{2,30}$")
PIN_RE = re.compile(r"^\d{4,6}$")


def compose_username(business_code, handle):
    """
    Staff usernames are globally unique in Django's auth table, so they are
    stored as '<business_code>__<handle>'. The login form shows two fields
    (Business code + Username) and composes this string.
    """
    return f"{business_code.strip().lower()}__{handle.strip().lower()}"

class SignupLinkInvalid(Exception):
    """Unknown, already-used or tampered verification link."""


class SignupLinkExpired(Exception):
    """The link was real but is older than SIGNUP_VERIFY_HOURS."""


class VerificationEmailFailed(Exception):
    """The mail server refused or timed out; nothing reached the person."""


class ResendTooSoon(Exception):
    def __init__(self, seconds):
        super().__init__(f"Wait {seconds}s before asking for another email.")
        self.seconds = seconds


def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _verify_url(raw_token):
    return settings.SITE_URL.rstrip("/") + reverse("tenants:verify-email", args=[raw_token])


def _send_verification_email(pending, raw_token):
    hours = settings.SIGNUP_VERIFY_HOURS
    ctx = {
        "owner_name": (pending.data.get("owner_name") or "").split(" ")[0] or "there",
        "business_name": pending.data.get("business_name", ""),
        "url": _verify_url(raw_token),
        "hours": hours,
    }
    msg = EmailMultiAlternatives(
        subject="Confirm your email to open your UniSale shop",
        body=render_to_string("tenants/emails/verify_email.txt", ctx),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[pending.email],
    )
    msg.attach_alternative(render_to_string("tenants/emails/verify_email.html", ctx), "text/html")
    try:
        msg.send(fail_silently=False)
    except Exception as exc:             
        log.exception("Could not send sign-up verification email to %s", pending.email)
        raise VerificationEmailFailed(str(exc)) from exc


def purge_expired_signups():
    """Abandoned sign-ups are just rows; clear the ones whose link is long dead."""
    PendingSignup.objects.filter(expires_at__lt=timezone.now() - timedelta(days=7)).delete()


def start_signup(*, business_name, business_type, owner_name, email, phone, password,
                 term_months, branch_count=1, branch_names=None):
    """
    Step 1 of public sign-up. Stores the answers (password hashed) and emails a link.
    Nothing real is created yet. Signing up again with the same email replaces the earlier
    attempt and invalidates its link. Raises ResendTooSoon if that address was emailed seconds ago,
    VerificationEmailFailed if the email can't go out.
    """
    email = email.lower().strip()
    purge_expired_signups()
    now = timezone.now()
    earlier = PendingSignup.objects.filter(email=email).first()
    if earlier is not None:    
        wait = settings.SIGNUP_RESEND_SECONDS - (now - earlier.last_sent_at).total_seconds()
        if wait > 0:
            raise ResendTooSoon(int(wait) + 1)
    raw = secrets.token_urlsafe(32)
    pending, _ = PendingSignup.objects.update_or_create(
        email=email,
        defaults=dict(
            token_hash=_hash_token(raw),
            password_hash=make_password(password),
            data=dict(
                business_name=business_name.strip(), business_type=business_type,
                owner_name=owner_name.strip(), phone=phone, term_months=int(term_months),
                branch_count=int(branch_count or 1), branch_names=list(branch_names or []),
            ),
            expires_at=now + timedelta(hours=settings.SIGNUP_VERIFY_HOURS),
            last_sent_at=now,
        ),
    )
    _send_verification_email(pending, raw)
    return pending


def resend_verification(email):
    """
    Send a fresh link (the previous one stops working). Returns True if an email went out,
    False if there is nothing pending for that address. Raises ResendTooSoon inside the cooldown.
    """
    pending = PendingSignup.objects.filter(email=(email or "").lower().strip()).first()
    if pending is None:
        return False
    now = timezone.now()
    wait = settings.SIGNUP_RESEND_SECONDS - (now - pending.last_sent_at).total_seconds()
    if wait > 0:
        raise ResendTooSoon(int(wait) + 1)
    raw = secrets.token_urlsafe(32)
    pending.token_hash = _hash_token(raw)
    pending.expires_at = now + timedelta(hours=settings.SIGNUP_VERIFY_HOURS)
    pending.last_sent_at = now
    pending.save(update_fields=["token_hash", "expires_at", "last_sent_at"])
    _send_verification_email(pending, raw)
    return True


@transaction.atomic
def complete_signup(raw_token):
    """
    Step 2: the owner clicked the link. Creates tenant + verified owner + branches + subscription +
    first invoice, then discards the pending row (so the link works exactly once).
    Returns (tenant, owner, invoice). Raises SignupLinkInvalid / SignupLinkExpired.
    """
    pending = (PendingSignup.objects.select_for_update()
               .filter(token_hash=_hash_token(raw_token or "")).first())
    if pending is None:
        raise SignupLinkInvalid()
    if pending.expires_at < timezone.now():
        raise SignupLinkExpired()
    try:
        tenant, owner, invoice = register_business(
            email=pending.email, password_hash=pending.password_hash, email_verified=True,
            **pending.data)
    except ValidationError:         
        pending.delete()
        raise SignupLinkInvalid()
    pending.delete()
    return tenant, owner, invoice


@transaction.atomic
def register_business(*, business_name, business_type, owner_name, email, phone,
                      password=None, password_hash=None, email_verified=False,
                      term_months, branch_count=1, branch_names=None):
    """
    Tenant + owner + branches + subscription + first invoice. The public flow reaches this only
    through complete_signup() (after the email link); pass either `password` or a ready `password_hash`.
    """
    from billing.services import start_subscription 
    if not (password or password_hash):
        raise ValueError("register_business needs a password or a password_hash.")
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
                first_name=first, last_name=last, phone=phone,
                role=User.Roles.ADMIN, is_owner=True, tenant=tenant,
                is_staff=False, is_superuser=False,   
                email_verified_at=timezone.now() if email_verified else None,
            )
            if password_hash:
                owner.password = password_hash
                owner.save(update_fields=["password"])
    except IntegrityError:
        raise ValidationError("An account with this email already exists.")

    branch_count = max(int(branch_count or 1), 1)
    names = list(branch_names or [])
    names = (names + [""] * branch_count)[:branch_count]
    names = [nm or ("Main Branch" if i == 0 else f"Branch {i + 1}") for i, nm in enumerate(names)]

    with tenant_context(tenant):
        provision_workspace(tenant, owner, branch_names=names)

    _subscription, invoice = start_subscription(tenant, term_months, branch_slots=branch_count)
    return tenant, owner, invoice


def provision_workspace(tenant, owner, branch_names=None):
    """
    Runs inside tenant_context(tenant). Everything a brand-new shop starts with.
    Reference data (units, categories) is per-tenant, so it must be seeded here.
    Branch on tenant.business_type as pharmacy/other needs grow.
    """
    from django.apps import apps
    from .branches import create_branch
    names = branch_names or ["Main Branch"]
    for i, name in enumerate(names):
        branch = create_branch(tenant, name=name, is_main=(i == 0), active=True)
        if i == 0:
            owner.branch = branch
            owner.save(update_fields=["branch"])

    if apps.is_installed("products"):
        from products.models import Category, UnitOfMeasure

        units = [("Piece", "pcs"), ("Kilogram", "kg"), ("Litre", "l"), ("Pack", "pk"), ("Box", "box")]
        if tenant.business_type == Tenant.BusinessType.PHARMACY:
            units += [("Tablet", "tab"), ("Bottle", "btl"), ("Strip", "strip")]
        for name, short in units:
            UnitOfMeasure.objects.create(name=name, short_name=short)
        Category.objects.create(name="General", slug="general")


def staff_handle(tenant, username):
    prefix = f"{tenant.slug}__"
    if username.startswith(prefix):
        return username[len(prefix):]
    return username


def list_staff(tenant):
    """Everyone except the owner -- the owner manages staff, not themselves, from here."""
    User = get_user_model()
    return (User.objects.filter(tenant=tenant, is_owner=False)
            .order_by("role", "username"))


def set_staff_credential(user, *, value):
    """
    CASHIER -> PIN (4-6 digits, hashed same as at creation).
    MANAGER (or any non-cashier role) -> password, run through Django's validators.
    Raises ValidationError on a bad value.
    """
    if user.role == user.Roles.CASHIER:
        if not PIN_RE.match(value or ""):
            raise ValidationError("PIN must be 4 to 6 digits.")
        user.pin = value  # hashed by User.save()
    else:
        from django.contrib.auth.password_validation import validate_password
        validate_password(value, user=user)
        user.set_password(value)
    user.save()
    return user


def set_staff_active(user, *, is_active):
    user.is_active = is_active
    user.save(update_fields=["is_active"])
    return user


def create_staff_user(*, tenant, handle, role, first_name="", last_name="", email="", phone="", national_id=None, password=None, pin=None, branch=None):
    """Owner/admin creates a cashier or manager inside their own tenant."""
    User = get_user_model()
    handle = handle.strip().lower()
    if not HANDLE_RE.match(handle):
        raise ValidationError("Username: 2-30 chars of a-z, 0-9, dot, dash, underscore.")
    if pin is not None and not PIN_RE.match(pin):
        raise ValidationError("PIN must be 4 to 6 digits.")
    if role not in User.Roles.values:
        raise ValidationError("Unknown role.")

    user = User(username=compose_username(tenant.slug, handle), first_name=first_name, last_name=last_name, email=email, phone=phone,national_id=national_id or None, role=role, tenant=tenant, branch=branch)

    if password:
        from django.contrib.auth.password_validation import validate_password
        validate_password(password, user=user)  # raises ValidationError -- caller handles it
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

def update_staff_details(user, *, first_name, last_name, email, phone, national_id=None):
    user.first_name, user.last_name = first_name, last_name
    user.email, user.phone = email, phone
    user.national_id = national_id or None
    try:
        user.save(update_fields=["first_name", "last_name", "email", "phone", "national_id"])
    except IntegrityError:
        raise ValidationError("Those details clash with another user in your shop.")
    return user