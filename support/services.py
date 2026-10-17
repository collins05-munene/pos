"""
All support business logic lives here so views, admin and management commands
behave the same way. Notifications and emails are sent on_commit and never
raise: a mail-server hiccup must not lose a customer's ticket.
(When you add Celery, wrap _send_email / _in_app in .delay() tasks.)
"""
import json
import logging
import os
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail, send_mass_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification

from . import conf
from .models import (
    FeatureRequest, FeatureVote, Severity, Ticket, TicketAttachment,
    TicketEvent, TicketMessage,
)

log = logging.getLogger(__name__)
_UNSET = object()


# ---------------------------------------------------------------- environment
_BROWSERS = [
    ("Edge", r"Edg(?:e|A|iOS)?/([\d.]+)"),
    ("Opera", r"OPR/([\d.]+)"),
    ("Samsung Internet", r"SamsungBrowser/([\d.]+)"),
    ("Firefox", r"(?:Firefox|FxiOS)/([\d.]+)"),
    ("Chrome", r"(?:Chrome|CriOS)/([\d.]+)"),
    ("Safari", r"Version/([\d.]+).*Safari/"),
]
_OSES = [
    ("Windows", r"Windows NT ([\d.]+)"),
    ("Android", r"Android ([\d.]+)"),
    ("iOS", r"(?:iPhone|iPad).*? OS ([\d_]+)"),
    ("macOS", r"Mac OS X ([\d_.]+)"),
    ("ChromeOS", r"CrOS"),
    ("Linux", r"Linux"),
]
_CLIENT_KEYS = ("screen", "viewport", "pixel_ratio", "language", "timezone", "online", "touch", "platform", "page")


def _match(table, ua):
    for name, pattern in table:
        m = re.search(pattern, ua)
        if m:
            ver = m.group(1).split(".")[0].replace("_", ".") if m.groups() else ""
            return f"{name} {ver}".strip()
    return "Unknown"


def parse_user_agent(ua):
    ua = ua or ""
    if "iPad" in ua or ("Android" in ua and "Mobi" not in ua):
        device = "Tablet"
    elif re.search(r"Mobi|iPhone|Android", ua):
        device = "Phone"
    else:
        device = "Computer"
    return {"browser": _match(_BROWSERS, ua), "os": _match(_OSES, ua), "device": device}


def capture_environment(request, raw_client=""):
    """
    Server-side facts (user agent, app version) are authoritative. The
    browser-reported extras are optional, whitelisted and length-capped.
    """
    ua = request.META.get("HTTP_USER_AGENT", "")[:400]
    env = parse_user_agent(ua)
    env["user_agent"] = ua
    try:
        client = json.loads(raw_client or "{}")
    except ValueError:
        client = {}
    if isinstance(client, dict):
        for key in _CLIENT_KEYS:
            val = client.get(key)
            if isinstance(val, (bool, int, float)):
                env[key] = val
            elif isinstance(val, str):
                env[key] = val[:200]
    env["app_version"] = conf.APP_VERSION
    return env


# -------------------------------------------------------------------- helpers
def _clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "..."


def _abs(path):
    return conf.SITE_URL.rstrip("/") + path


def _send_email(recipients, subject, body):
    recipients = sorted({r for r in recipients if r})
    if not recipients:
        return
    subject = " ".join(subject.split())[:200]  # no newlines in headers
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
    except Exception:
        log.exception("Support email failed: %s", subject)


def _in_app(tenant, user, title, message, level):
    if tenant is None or user is None:
        return
    try:
        Notification.objects.create(
            tenant=tenant, user=user, title=_clip(title, 100),
            message=_clip(message, 250), level=level,
        )
    except Exception:
        log.exception("Support in-app notification failed for user %s", getattr(user, "pk", None))


def _add_event(ticket, actor, kind, detail, visible=True):
    return TicketEvent.objects.create(
        ticket=ticket, actor=actor, kind=kind, detail=detail[:255], visible_to_customer=visible,
    )


def _person(user):
    if user is None:
        return "Support"
    return user.get_full_name() or user.get_username()


def _save_attachments(ticket, message, user, files):
    for f in files or ():
        TicketAttachment.objects.create(
            ticket=ticket, message=message, uploaded_by=user, file=f,
            original_name=os.path.basename(f.name)[:200],
            content_type=(getattr(f, "content_type", "") or "")[:100],
            size=f.size,
        )


# ------------------------------------------------------------- notifications
def _notify_customer(ticket, title, body, level=None):
    ticket_id = ticket.pk

    def run():
        t = Ticket._base_manager.select_related("created_by", "tenant").get(pk=ticket_id)
        user = t.created_by
        if user is None:
            return
        _in_app(t.tenant, user, title, body, level or Notification.Level.INFO)
        _send_email(
            [getattr(user, "email", "")],
            f"[{t.reference}] {title}",
            f"{body}\n\nOpen the conversation:\n{_abs(t.get_absolute_url())}\n\n{conf.PRODUCT_NAME} support",
        )

    transaction.on_commit(run)


def _notify_team(ticket, headline, extra=""):
    ticket_id = ticket.pk

    def run():
        t = Ticket._base_manager.select_related("created_by", "tenant", "assigned_to").get(pk=ticket_id)
        to = [conf.SUPPORT_EMAIL] if conf.SUPPORT_EMAIL else []
        if t.assigned_to and t.assigned_to.email:
            to.append(t.assigned_to.email)
        tag = f"[{t.get_priority_display().upper()}] " if t.priority in (Severity.HIGH, Severity.CRITICAL) else ""
        lines = [
            f"{headline}",
            f"Business: {t.tenant}",
            f"From: {_person(t.created_by)}",
            f"Type: {t.get_kind_display()}" + (f" / {t.get_category_display()}" if t.category else ""),
            "",
            _clip(extra or t.description, 1500),
            "",
            _abs(t.get_staff_url()),
        ]
        _send_email(to, f"{tag}{t.reference} {headline}: {t.subject}", "\n".join(lines))

    transaction.on_commit(run)


# --------------------------------------------------------------------- tickets
@transaction.atomic
def create_ticket(*, tenant, user, kind, subject, description, category="",
                  severity=Severity.MEDIUM, steps="", environment=None, files=()):
    environment = environment or {}
    ticket = Ticket.objects.create(
        tenant=tenant, created_by=user, kind=kind, category=category, subject=subject,
        description=description, steps_to_reproduce=steps, severity=severity,
        environment=environment, app_version=str(environment.get("app_version", ""))[:40],
    )
    _add_event(ticket, user, TicketEvent.Kind.CREATED, f"{ticket.get_kind_display()} submitted")
    _save_attachments(ticket, None, user, files)
    _notify_team(ticket, "New ticket")
    return ticket


def _apply_status(ticket, new_status, actor, detail=None):
    """Mutates the ticket and logs the event. Returns the changed field names."""
    old = ticket.status
    ticket.status = new_status
    fields = {"status"}
    now = timezone.now()
    if new_status == Ticket.Status.RESOLVED:
        ticket.resolved_at = now
        fields.add("resolved_at")
    elif new_status == Ticket.Status.CLOSED:
        ticket.closed_at = now
        fields.add("closed_at")
        if ticket.resolved_at is None:
            ticket.resolved_at = now
            fields.add("resolved_at")
    elif old in (Ticket.Status.RESOLVED, Ticket.Status.CLOSED):
        ticket.resolved_at = None
        ticket.closed_at = None
        fields |= {"resolved_at", "closed_at"}
    _add_event(
        ticket, actor, TicketEvent.Kind.STATUS,
        detail or f"Status changed from {Ticket.Status(old).label} to {Ticket.Status(new_status).label}",
    )
    return fields


@transaction.atomic
def add_message(ticket, *, author, body, files=(), internal=False, from_staff=False):
    if not from_staff and ticket.status == Ticket.Status.CLOSED:
        raise ValueError("This ticket is closed.")
    if internal and not from_staff:
        raise ValueError("Only staff can write internal notes.")

    msg = TicketMessage.objects.create(
        ticket=ticket, author=author, body=body, is_staff_reply=from_staff, is_internal=internal,
    )
    _save_attachments(ticket, msg, author, files)

    now = timezone.now()
    fields = {"updated_at"}
    if from_staff and not internal:
        ticket.last_staff_reply_at = now
        fields.add("last_staff_reply_at")
        if ticket.first_response_at is None:
            ticket.first_response_at = now
            fields.add("first_response_at")
        if ticket.status == Ticket.Status.OPEN:
            fields |= _apply_status(ticket, Ticket.Status.IN_PROGRESS, author)
    elif not from_staff:
        ticket.last_customer_message_at = now
        fields.add("last_customer_message_at")
        if ticket.status in (Ticket.Status.WAITING, Ticket.Status.RESOLVED):
            detail = "Reopened by customer" if ticket.status == Ticket.Status.RESOLVED else "Customer replied"
            fields |= _apply_status(ticket, Ticket.Status.OPEN, author, detail)
    ticket.save(update_fields=list(fields))

    if from_staff and not internal:
        _notify_customer(ticket, f"{_person(author).split(' ')[0]} replied to {ticket.reference}", _clip(body, 600))
    elif not from_staff:
        _notify_team(ticket, "Customer replied", body)
    return msg


@transaction.atomic
def update_ticket(ticket, *, actor, status=_UNSET, priority=_UNSET, assigned_to=_UNSET,
                  resolution_summary=_UNSET, notify=True):
    fields = {"updated_at"}
    if resolution_summary is not _UNSET and resolution_summary != ticket.resolution_summary:
        ticket.resolution_summary = resolution_summary
        fields.add("resolution_summary")
    if priority is not _UNSET and priority != ticket.priority:
        _add_event(
            ticket, actor, TicketEvent.Kind.PRIORITY,
            f"Priority changed from {ticket.get_priority_display() or 'none'} to {Severity(priority).label}",
            visible=False,
        )
        ticket.priority = priority
        fields.add("priority")
    if assigned_to is not _UNSET and assigned_to != ticket.assigned_to:
        ticket.assigned_to = assigned_to
        fields.add("assigned_to")
        _add_event(
            ticket, actor, TicketEvent.Kind.ASSIGNED,
            f"Assigned to {_person(assigned_to)}" if assigned_to else "Unassigned",
        )
    status_changed = False
    if status is not _UNSET and status != ticket.status:
        fields |= _apply_status(ticket, status, actor)
        status_changed = True
    ticket.save(update_fields=list(fields))

    if notify and status_changed and ticket.status in (Ticket.Status.RESOLVED, Ticket.Status.CLOSED):
        summary = ticket.resolution_summary or "Your ticket has been closed."
        _notify_customer(
            ticket, f"{ticket.reference} is {ticket.get_status_display().lower()}",
            f"Summary: {_clip(summary, 600)}\n\nIf this isn't fixed, reply on the ticket and we'll reopen it.",
            Notification.Level.SUCCESS,
        )
    return ticket


# ------------------------------------------------------------ feature requests
@transaction.atomic
def create_feature_request(*, tenant, user, title, description, module):
    fr = FeatureRequest.objects.create(
        title=title, description=description, module=module, submitted_by=user, tenant=tenant,
    )
    FeatureVote.objects.create(feature=fr, user=user)
    fr.vote_count = 1
    ticket = create_ticket(
        tenant=tenant, user=user, kind=Ticket.Kind.FEATURE,
        subject=f"Feature request: {title}",
        description=f"Module: {fr.get_module_display()}\n\n{description}",
    )
    fr.ticket = ticket
    fr.save(update_fields=["vote_count", "ticket"])
    return fr


def toggle_vote(feature_id, user):
    """Returns (now_voted, feature). Raises ValueError if voting is closed."""
    with transaction.atomic():
        fr = FeatureRequest.objects.select_for_update().get(pk=feature_id, is_visible=True)
        if not fr.voting_open:
            raise ValueError("Voting is closed for this request.")
        deleted, _ = FeatureVote.objects.filter(feature=fr, user=user).delete()
        if not deleted:
            FeatureVote.objects.create(feature=fr, user=user)
        fr.vote_count = fr.votes.count()  # recount, so the number can't drift
        fr.save(update_fields=["vote_count"])
    return (not deleted), fr


def feature_status_changed(fr):
    """
    Call after a staff member changes a request's status (the admin does).
    Released: stamp the date, tell every voter once, and resolve the
    author's ticket. Not planned: just resolve the author's ticket.
    """
    changed = []
    if fr.status == FeatureRequest.Status.RELEASED:
        if fr.released_at is None:
            fr.released_at = timezone.now()
            changed.append("released_at")
        if fr.release_notified_at is None:
            fr.release_notified_at = timezone.now()
            changed.append("release_notified_at")
            _notify_release(fr.pk)
    if changed:
        fr.save(update_fields=changed)

    if fr.status in (FeatureRequest.Status.RELEASED, FeatureRequest.Status.DECLINED):
        t = fr.ticket
        if t is not None and t.is_open:
            if fr.status == FeatureRequest.Status.RELEASED:
                summary = fr.release_notes or "This feature has been released."
            else:
                summary = fr.status_note or "We've decided not to build this for now."
            update_ticket(t, actor=None, status=Ticket.Status.RESOLVED,
                          resolution_summary=summary, notify=False)


def _notify_release(feature_id):
    def run():
        fr = FeatureRequest.objects.get(pk=feature_id)
        User = get_user_model()
        title = f"Now available: {fr.title}"
        body = fr.release_notes or "A feature you asked for has just been released."
        link = _abs(reverse("support:feature-list") + "?status=RELEASED")
        messages = []
        for u in User.objects.filter(feature_votes__feature_id=feature_id, is_active=True).distinct():
            _in_app(getattr(u, "tenant", None), u, title, body, Notification.Level.SUCCESS)
            if getattr(u, "email", ""):
                messages.append((
                    " ".join(title.split())[:200],
                    f"{_clip(body, 800)}\n\nSee what's new:\n{link}\n\nThanks for helping shape {conf.PRODUCT_NAME}.",
                    settings.DEFAULT_FROM_EMAIL, [u.email],
                ))
        if messages:
            try:
                send_mass_mail(tuple(messages), fail_silently=False)
            except Exception:
                log.exception("Release emails failed for feature %s", feature_id)

    transaction.on_commit(run)
