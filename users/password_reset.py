"""
Password resets that the PLATFORM TEAM approves.

    forgot-password form  ->  PENDING request  ->  platform_admin approves  ->  one-time link emailed
                                  |                       |                          |
                         owner/admin notified     owner/admin notified        user sets new password
                                                                              owner/admin notified

Nobody can reset a password just by knowing a username: a human at the platform has to say yes, and
the link only ever goes to the email address already on the account.
"""
import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from notifications.utils import ADMINS, notify, who
from tenants.context import tenant_context

from .models import PasswordResetRequest
from .utils import AuditAction, log_action

log = logging.getLogger(__name__)
Status = PasswordResetRequest.Status


class NoEmailOnFile(Exception):
    """Neither the account nor its shop owner has an email address to send the link to."""


class ResetEmailFailed(Exception):
    """The mail server refused or timed out; nothing reached the person."""


class ResetLinkInvalid(Exception):
    """Unknown, already-used, rejected or tampered link."""


class ResetLinkExpired(Exception):
    """The link was real but is older than PASSWORD_RESET_LINK_HOURS."""


class RequestNotPending(Exception):
    """Somebody else already approved/rejected this request."""


def _hours():
    return getattr(settings, "PASSWORD_RESET_LINK_HOURS", 24)


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _reset_url(raw):
    return settings.SITE_URL.rstrip("/") + reverse("password-reset-confirm", args=[raw])


def mask_email(email):
    """jane.doe@gmail.com -> j*******@gmail.com (what the platform dashboard shows)."""
    name, _, domain = (email or "").partition("@")
    if not domain:
        return ""
    return f"{name[:1]}{'*' * max(len(name) - 1, 1)}@{domain}"


def staff_label(user):
    """The name they type at login: owner's email, or the staff handle without the shop prefix."""
    return user.username.split("__", 1)[-1] if user.tenant_id else user.username


def _owner_email(tenant):
    User = get_user_model()
    owner = User.objects.filter(tenant=tenant, is_owner=True).exclude(email="").first()
    return owner.email if owner else (tenant.email or "")


def _email_for(user):
    """The account's own address, else (staff without one) the shop owner's, else ''."""
    return user.email or _owner_email(user.tenant)


def _best_effort_mail(subject, body, to):
    to = [a for a in to if a]
    if not to:
        return
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, to, fail_silently=False)
    except Exception:
        log.exception("Could not send '%s' to %s", subject, to)


def find_user(username):
    """Resolve the (already composed) username to a user who is allowed to use this flow."""
    User = get_user_model()
    user = (User.objects.select_related("tenant").filter(username=username).first()
            if username else None)
    if (user is None or not user.is_active or user.is_superuser
            or user.tenant_id is None or not user.tenant.is_active):
        return None
    return user


def _audit(user, action, details, request=None):
    """
    Write the audit row for the user's own shop. tenant_context matters when a platform admin is
    in a support session for a DIFFERENT shop: without it the write would be refused as cross-tenant.
    """
    try:
        with tenant_context(user.tenant):
            log_action(user, action, details, request, tenant=user.tenant)
    except Exception:
        log.exception("Could not write audit log %s", action)


def _notify_shop(tenant, title, message, level=Notification.Level.WARNING):
    notify(tenant, title, message, level=level, roles=ADMINS)


# --------------------------------------------------------------------------- 1. someone asks
def request_reset(*, username, ip=None, request=None):
    """
    Called from the public forgot-password form. Returns the PasswordResetRequest that was
    created, or None when nothing was created (unknown account, or a request is already open).
    The caller shows the SAME neutral message either way so usernames can't be probed.
    """
    user = find_user(username)
    if user is None:
        return None

    now = timezone.now()
    # Already waiting for approval, or approved and the emailed link is still good: don't spam
    # the owner and the platform team with duplicates.
    if PasswordResetRequest.objects.filter(user=user, status=Status.PENDING).exists():
        return None
    if PasswordResetRequest.objects.filter(user=user, status=Status.APPROVED,
                                           link_expires_at__gt=now).exists():
        return None

    try:
        with transaction.atomic():
            req = PasswordResetRequest.objects.create(user=user, tenant=user.tenant, requested_ip=ip)
    except IntegrityError:          # two clicks at once; the partial unique index caught the second
        return None

    _audit(user, AuditAction.PASSWORD_RESET_REQUESTED,
           f"Password reset requested for {who(user)}; waiting for platform approval.", request)

    _notify_shop(
        user.tenant, "Password reset requested",
        f"{who(user)} asked to reset their password. Our team must approve it before any reset link "
        "is sent. If nobody in your shop asked for this, tell us right away.")
    _alert_platform(req)
    _email_shop_owner_requested(req)
    return req


def _alert_platform(req):
    """Tell the platform team a request is waiting (they also see it on the dashboard)."""
    User = get_user_model()
    to = list(User.objects.filter(is_superuser=True, is_active=True)
              .exclude(email="").values_list("email", flat=True))
    to = to or [getattr(settings, "SUPPORT_EMAIL", "")]
    link = settings.SITE_URL.rstrip("/") + reverse("platform_admin:resets")
    _best_effort_mail(
        "Password reset waiting for approval",
        f"{who(req.user)} at {req.tenant.name} ({req.tenant.slug}) asked to reset their password.\n\n"
        f"Review it here: {link}\n",
        to)


def _email_shop_owner_requested(req):
    owner_email = _owner_email(req.tenant)
    if not owner_email:
        return
    _best_effort_mail(
        f"Password reset requested for {req.tenant.name}",
        f"Hello,\n\n{who(req.user)} asked to reset their password on {req.tenant.name}.\n\n"
        "Nothing has changed yet: our team has to approve the request first, and the reset link "
        "will only be sent to the email address on that account.\n\n"
        "If nobody in your shop asked for this, please contact support.\n",
        [owner_email])


# --------------------------------------------------------------------------- 2. platform decides
@transaction.atomic
def approve(request_id, *, by, note=""):
    """
    Platform approves: mint a one-time token, email the link, tell the shop owner/admins.
    Raises RequestNotPending, NoEmailOnFile or ResetEmailFailed (in which case NOTHING is saved,
    so the request simply stays pending and can be retried or rejected).
    """
    req = (PasswordResetRequest.objects.select_for_update()
           .select_related("user", "user__tenant", "tenant").get(pk=request_id))
    if req.status != Status.PENDING:
        raise RequestNotPending()

    to = _email_for(req.user)
    if not to:
        raise NoEmailOnFile()

    raw = secrets.token_urlsafe(32)
    now = timezone.now()
    req.status = Status.APPROVED
    req.decided_by, req.decided_at, req.decision_note = by, now, (note or "")[:255]
    req.token_hash = _hash(raw)
    req.link_expires_at = now + timedelta(hours=_hours())
    req.link_sent_to = to
    req.save()

    _send_link_email(req, raw, to)       # last: if it fails the whole transaction rolls back

    _audit(req.user, AuditAction.PASSWORD_RESET_APPROVED,
           f"Password reset for {who(req.user)} approved by {by.username}; link emailed to {mask_email(to)}.")
    _notify_shop(
        req.tenant, "Password reset approved",
        f"The password reset for {who(req.user)} was approved. A reset link was emailed to "
        f"{mask_email(to)} and works for {_hours()} hours.",
        level=Notification.Level.INFO)
    return req


def _send_link_email(req, raw, to):
    ctx = {"name": req.user.first_name or who(req.user), "shop": req.tenant.name,
           "url": _reset_url(raw), "hours": _hours(), "for_someone_else": to != req.user.email,
           "person": who(req.user)}
    msg = EmailMultiAlternatives(
        subject="Your UniSale password reset link",
        body=render_to_string("users/emails/reset_link.txt", ctx),
        from_email=settings.DEFAULT_FROM_EMAIL, to=[to])
    msg.attach_alternative(render_to_string("users/emails/reset_link.html", ctx), "text/html")
    try:
        msg.send(fail_silently=False)
    except Exception as exc:
        log.exception("Could not send password reset email to %s", to)
        raise ResetEmailFailed(str(exc)) from exc


@transaction.atomic
def reject(request_id, *, by, note=""):
    req = (PasswordResetRequest.objects.select_for_update()
           .select_related("user", "user__tenant", "tenant").get(pk=request_id))
    if req.status != Status.PENDING:
        raise RequestNotPending()
    req.status = Status.REJECTED
    req.decided_by, req.decided_at, req.decision_note = by, timezone.now(), (note or "")[:255]
    req.save()

    _audit(req.user, AuditAction.PASSWORD_RESET_REJECTED,
           f"Password reset for {who(req.user)} rejected by {by.username}.")
    _notify_shop(
        req.tenant, "Password reset declined",
        f"The password reset request for {who(req.user)} was declined by our team. "
        "Contact support if you think this is a mistake.",
        level=Notification.Level.INFO)
    return req


# --------------------------------------------------------------------------- 3. user sets the password
def get_request_for_token(raw_token):
    """The APPROVED request this link belongs to. Raises ResetLinkInvalid / ResetLinkExpired."""
    req = (PasswordResetRequest.objects.select_related("user", "user__tenant", "tenant")
           .filter(token_hash=_hash(raw_token or ""), status=Status.APPROVED).first())
    if req is None or not req.user.is_active:
        raise ResetLinkInvalid()
    if req.link_expires_at is None or req.link_expires_at < timezone.now():
        raise ResetLinkExpired()
    return req


@transaction.atomic
def complete_reset(raw_token, new_password, *, request=None):
    """Validate + set the password and burn the link. Raises ResetLinkInvalid/Expired or ValidationError."""
    req = get_request_for_token(raw_token)
    req = (PasswordResetRequest.objects.select_for_update()
           .select_related("user", "tenant").get(pk=req.pk))
    if req.status != Status.APPROVED:        # lost a race with a second click
        raise ResetLinkInvalid()

    user = req.user
    validate_password(new_password, user=user)
    user.set_password(new_password)          # also signs out every other session of this user
    user.save(update_fields=["password"])

    req.status, req.completed_at, req.token_hash = Status.COMPLETED, timezone.now(), None
    req.save(update_fields=["status", "completed_at", "token_hash"])

    _audit(user, AuditAction.PASSWORD_RESET_COMPLETED,
           f"{who(user)} set a new password using an approved reset link.", request)
    _notify_shop(
        req.tenant, "Password was changed",
        f"{who(user)} set a new password using the reset link. If that wasn't them, contact support now.",
        level=Notification.Level.WARNING)
    return user
