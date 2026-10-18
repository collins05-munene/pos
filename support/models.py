import os
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse

from tenants.models import Tenant

from .storage import attachment_path, get_private_storage

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov"}


class Severity(models.TextChoices):
    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"
    CRITICAL = "CRITICAL", "Critical"


class Ticket(models.Model):
    class Kind(models.TextChoices):
        ISSUE = "ISSUE", "Issue report"
        FEATURE = "FEATURE", "Feature request"
        QUESTION = "QUESTION", "Message to support"

    class Category(models.TextChoices):
        SALES = "SALES", "Sales and POS terminal"
        INVENTORY = "INVENTORY", "Inventory and stock"
        PAYMENTS = "PAYMENTS", "Payments and cash"
        REGISTER = "REGISTER", "Cash register"
        REPORTS = "REPORTS", "Reports"
        LOGIN = "LOGIN", "Login and access"
        PRINTING = "PRINTING", "Printing and receipts"
        BILLING = "BILLING", "Billing and subscription"
        OTHER = "OTHER", "Something else"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        WAITING = "WAITING", "Waiting for customer"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"

    number = models.PositiveIntegerField(unique=True, null=True, blank=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="support_tickets")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="support_tickets",
    )

    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.QUESTION)
    category = models.CharField(max_length=12, choices=Category.choices, blank=True)
    subject = models.CharField(max_length=200)
    description = models.TextField()
    steps_to_reproduce = models.TextField(blank=True)

    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.MEDIUM)
    priority = models.CharField(max_length=10, choices=Severity.choices, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.OPEN)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="assigned_tickets",
        limit_choices_to={"is_staff": True},
    )

    environment = models.JSONField(default=dict, blank=True)
    app_version = models.CharField(max_length=40, blank=True)
    resolution_summary = models.TextField(blank=True)

    first_response_at = models.DateTimeField(null=True, blank=True)
    last_staff_reply_at = models.DateTimeField(null=True, blank=True)
    last_customer_message_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["tenant", "-updated_at"]),
            models.Index(fields=["status", "priority"]),
        ]

    def __str__(self):
        return f"{self.reference}: {self.subject}"

    def save(self, *args, **kwargs):
        creating = self.pk is None
        if creating and not self.priority:
            self.priority = self.severity
        super().save(*args, **kwargs)
        if creating and self.number is None:
            self.number = 1000 + self.pk
            type(self)._base_manager.filter(pk=self.pk).update(number=self.number)

    @property
    def reference(self):
        return f"TKT-{self.number}" if self.number else "TKT-?"

    
    @property
    def customer_name(self):
        u = self.created_by
        return (u.get_full_name() or u.get_username()) if u else "Former user"

    @property
    def customer_first_name(self):
        u = self.created_by
        return (u.first_name or u.get_username()) if u else "Former user"

    @property
    def agent_name(self):
        u = self.assigned_to
        return (u.first_name or u.get_username()) if u else "Unassigned"

    @property
    def is_open(self):
        return self.status not in (self.Status.RESOLVED, self.Status.CLOSED)

    def get_absolute_url(self):
        return reverse("support:ticket-detail", args=[self.number])

    def get_staff_url(self):
        return reverse("support:staff-ticket", args=[self.number])


class TicketMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    body = models.TextField()
    is_staff_reply = models.BooleanField(default=False)
    is_internal = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return f"{self.ticket.reference} message {self.pk}"

    @property
    def author_name(self):
        u = self.author
        if u is None:
            return "Support" if self.is_staff_reply else "Former user"
        return u.first_name or u.get_username()

    @property
    def author_full_name(self):
        u = self.author
        return (u.get_full_name() or u.get_username()) if u else "Former user"


class TicketAttachment(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="attachments")
    message = models.ForeignKey(
        TicketMessage, null=True, blank=True,
        on_delete=models.CASCADE, related_name="attachments",
    )
    file = models.FileField(upload_to=attachment_path, storage=get_private_storage)
    original_name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self):
        return self.original_name

    @property
    def ext(self):
        return os.path.splitext(self.original_name)[1].lower()

    @property
    def is_image(self):
        return self.ext in IMAGE_EXTS

    @property
    def is_video(self):
        return self.ext in VIDEO_EXTS


class TicketEvent(models.Model):
    """The ticket history: status changes, assignment, priority."""

    class Kind(models.TextChoices):
        CREATED = "CREATED", "Created"
        STATUS = "STATUS", "Status"
        PRIORITY = "PRIORITY", "Priority"
        ASSIGNED = "ASSIGNED", "Assigned"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    detail = models.CharField(max_length=255)
    visible_to_customer = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class FeatureRequest(models.Model):
    class Module(models.TextChoices):
        SALES = "SALES", "Sales and POS terminal"
        INVENTORY = "INVENTORY", "Inventory"
        PURCHASING = "PURCHASING", "Purchasing and suppliers"
        ACCOUNTING = "ACCOUNTING", "Accounting"
        REPORTS = "REPORTS", "Reports"
        STAFF = "STAFF", "Staff and permissions"
        BILLING = "BILLING", "Billing"
        INTEGRATIONS = "INTEGRATIONS", "Integrations"
        OTHER = "OTHER", "Something else"

    class Status(models.TextChoices):
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        PLANNED = "PLANNED", "Planned"
        IN_DEVELOPMENT = "IN_DEVELOPMENT", "In development"
        RELEASED = "RELEASED", "Released"
        DECLINED = "DECLINED", "Not planned"

    title = models.CharField(max_length=120)
    description = models.TextField()
    module = models.CharField(max_length=14, choices=Module.choices, default=Module.OTHER)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.UNDER_REVIEW)
    status_note = models.CharField(max_length=200, blank=True)
    release_notes = models.TextField(blank=True)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="feature_requests",
    )
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    ticket = models.OneToOneField(
        Ticket, null=True, blank=True, on_delete=models.SET_NULL, related_name="feature_request",
    )
    vote_count = models.PositiveIntegerField(default=0)
    is_visible = models.BooleanField(default=True, help_text="Untick to hide spam or duplicates.")

    released_at = models.DateTimeField(null=True, blank=True)
    release_notified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-vote_count", "-created_at"]

    def __str__(self):
        return self.title

    @property
    def voting_open(self):
        return self.status not in (self.Status.RELEASED, self.Status.DECLINED)


class FeatureVote(models.Model):
    feature = models.ForeignKey(FeatureRequest, on_delete=models.CASCADE, related_name="votes")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="feature_votes",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["feature", "user"], name="one_vote_per_user"),
        ]


_YT = re.compile(r"^https://(?:www\.)?(?:youtube\.com/(?:watch\?v=|embed/)|youtu\.be/)([\w-]{11})")
_VIMEO = re.compile(r"^https://(?:player\.)?vimeo\.com/(?:video/)?(\d+)")


def to_embed_url(url):
    url = url or ""
    m = _YT.match(url)
    if m:
        return f"https://www.youtube-nocookie.com/embed/{m.group(1)}"
    m = _VIMEO.match(url)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"
    return None


def validate_video_url(value):
    if value and not to_embed_url(value):
        raise ValidationError("Use a full https link to a YouTube or Vimeo video.")


class HelpArticle(models.Model):
    class Topic(models.TextChoices):
        GETTING_STARTED = "GETTING_STARTED", "Getting started"
        SALES = "SALES", "Sales and POS terminal"
        MPESA = "MPESA", "M-PESA and payments"
        RECEIPTS = "RECEIPTS", "Receipts and printing"
        STOCK = "STOCK", "Stock management"
        REPORTS = "REPORTS", "Reports"
        ACCOUNT = "ACCOUNT", "Account and billing"

    class Kind(models.TextChoices):
        FAQ = "FAQ", "FAQ"
        GUIDE = "GUIDE", "Step-by-step guide"
        VIDEO = "VIDEO", "Video tutorial"
        TROUBLESHOOTING = "TROUBLESHOOTING", "Troubleshooting"

    title = models.CharField(max_length=160)
    slug = models.SlugField(max_length=170, unique=True)
    topic = models.CharField(max_length=16, choices=Topic.choices)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.GUIDE)
    summary = models.CharField(max_length=300, help_text="One or two sentences shown in search results.")
    body = models.TextField(
        blank=True,
        help_text="HTML. Written by your team only, so it is rendered as-is. Use <ol> for steps.",
    )
    video_url = models.URLField(blank=True, validators=[validate_video_url])
    keywords = models.CharField(
        max_length=300, blank=True,
        help_text="Words customers might type that aren't in the title, e.g. 'till, cashup, mpesa'.",
    )
    is_published = models.BooleanField(default=False)
    is_featured = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "title"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("support:help-article", args=[self.slug])

    @property
    def embed_url(self):
        return to_embed_url(self.video_url)
