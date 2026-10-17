import os

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from . import conf
from .models import IMAGE_EXTS, VIDEO_EXTS, FeatureRequest, Severity, Ticket

MB = 1024 * 1024
ALLOWED_EXTS = IMAGE_EXTS | VIDEO_EXTS | {".pdf", ".txt", ".csv"}
ACCEPT = ",".join(sorted(ALLOWED_EXTS))

SEVERITY_CHOICES = [
    (Severity.LOW, "Low: a small problem and I can work around it"),
    (Severity.MEDIUM, "Medium: it slows my work down"),
    (Severity.HIGH, "High: part of my shop can't work properly"),
    (Severity.CRITICAL, "Critical: I can't sell, or the shop is down"),
]


def validate_attachments(files):
    if len(files) > conf.MAX_ATTACHMENTS:
        raise ValidationError(f"Attach up to {conf.MAX_ATTACHMENTS} files at a time.")
    total = 0
    for f in files:
        ext = os.path.splitext(f.name)[1].lower()
        if ext not in ALLOWED_EXTS:
            raise ValidationError(
                f"{f.name} isn't a supported file type. Use images, MP4, WebM or MOV video, PDF, TXT or CSV."
            )
        limit = conf.MAX_VIDEO_MB if ext in VIDEO_EXTS else conf.MAX_FILE_MB
        if f.size > limit * MB:
            raise ValidationError(f"{f.name} is larger than {limit} MB.")
        total += f.size
    if total > conf.MAX_TOTAL_MB * MB:
        raise ValidationError(f"Files add up to more than {conf.MAX_TOTAL_MB} MB. Attach fewer or smaller files.")


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

    def value_from_datadict(self, data, files, name):
        return files.getlist(name)


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        if not data:
            if self.required:
                raise ValidationError(self.error_messages["required"], code="required")
            return []
        single = super().clean
        files = data if isinstance(data, (list, tuple)) else [data]
        return [single(f, initial) for f in files]


class SupportForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        skip = (forms.RadioSelect, forms.CheckboxInput, forms.HiddenInput, forms.FileInput)
        for field in self.fields.values():
            if not isinstance(field.widget, skip):
                field.widget.attrs.setdefault("class", "sp-input")


class AttachmentForm(SupportForm):
    attachments = MultipleFileField(
        required=False,
        label="Screenshots or a short screen recording",
        widget=MultipleFileInput(attrs={"accept": ACCEPT, "multiple": True}),
        help_text=f"Up to {conf.MAX_ATTACHMENTS} files. Images and PDFs up to {conf.MAX_FILE_MB} MB, videos up to {conf.MAX_VIDEO_MB} MB.",
    )

    def clean_attachments(self):
        files = self.cleaned_data.get("attachments") or []
        validate_attachments(files)
        return files


class IssueReportForm(AttachmentForm):
    category = forms.ChoiceField(choices=Ticket.Category.choices, label="What part of the POS is affected?")
    severity = forms.ChoiceField(
        choices=SEVERITY_CHOICES, widget=forms.RadioSelect, initial=Severity.MEDIUM,
        label="How serious is it?",
    )
    subject = forms.CharField(
        max_length=200, label="Short summary",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Receipt prints blank after a cash sale", "data-suggest-source": ""}),
    )
    description = forms.CharField(
        label="What happened?",
        widget=forms.Textarea(attrs={"rows": 5, "data-suggest-source": ""}),
    )
    steps_to_reproduce = forms.CharField(
        required=False, label="How can we see it happen?",
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="List the steps you took, one per line, and what you expected to happen instead.",
    )
    client_env = forms.CharField(required=False, widget=forms.HiddenInput)


class ContactForm(AttachmentForm):
    subject = forms.CharField(max_length=200, label="Subject")
    message = forms.CharField(label="Your message", widget=forms.Textarea(attrs={"rows": 6}))
    client_env = forms.CharField(required=False, widget=forms.HiddenInput)


class ReplyForm(AttachmentForm):
    body = forms.CharField(label="Reply", widget=forms.Textarea(attrs={"rows": 4}))


class FeatureRequestForm(SupportForm):
    title = forms.CharField(
        max_length=120, label="Feature title",
        widget=forms.TextInput(attrs={"placeholder": "e.g. Expiry date alerts for batches", "data-suggest-source": ""}),
    )
    module = forms.ChoiceField(choices=FeatureRequest.Module.choices, label="Which part of the POS is it for?")
    description = forms.CharField(
        label="Describe it and how it would help your business",
        widget=forms.Textarea(attrs={"rows": 6, "data-suggest-source": ""}),
    )


class StaffReplyForm(AttachmentForm):
    body = forms.CharField(label="Reply", widget=forms.Textarea(attrs={"rows": 5}))
    is_internal = forms.BooleanField(required=False, label="Internal note: customers can't see this")


class TicketManageForm(SupportForm):
    status = forms.ChoiceField(choices=Ticket.Status.choices)
    priority = forms.ChoiceField(choices=Severity.choices)
    assigned_to = forms.ModelChoiceField(
        queryset=get_user_model().objects.none(), required=False, empty_label="Unassigned",
    )
    resolution_summary = forms.CharField(
        required=False, label="Resolution summary",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Shown to the customer when the ticket is resolved or closed.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assigned_to"].queryset = get_user_model().objects.filter(
            is_staff=True, is_active=True
        ).order_by("first_name", "username")

    def clean(self):
        data = super().clean()
        if data.get("status") in (Ticket.Status.RESOLVED, Ticket.Status.CLOSED) and not (
            data.get("resolution_summary") or ""
        ).strip():
            self.add_error("resolution_summary", "Add a short summary before resolving or closing the ticket.")
        return data
