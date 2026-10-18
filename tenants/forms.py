from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
import re

from billing import conf
from billing.mpesa import normalize_phone
from .models import Tenant


ID_RE = re.compile(r"^[A-Za-z0-9]{6,12}$")

class SignupForm(forms.Form):
    business_name = forms.CharField(max_length=150)
    business_type = forms.ChoiceField(choices=Tenant.BusinessType.choices)
    owner_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    phone = forms.CharField(max_length=20, help_text="M-Pesa number, e.g. 0712345678")
    password = forms.CharField(widget=forms.PasswordInput)
    term_months = forms.TypedChoiceField(coerce=int, choices=(), initial=1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["term_months"].choices = [
            (t, f"{t} month{'s' if t > 1 else ''}") for t in conf.get("TERM_DISCOUNT_PERCENT")
        ]

    def clean_email(self):
        email = self.cleaned_data["email"].lower().strip()
        if get_user_model().objects.filter(username__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean_phone(self):
        try:
            return normalize_phone(self.cleaned_data["phone"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc))

    def clean_password(self):
        validate_password(self.cleaned_data["password"])
        return self.cleaned_data["password"]


class StaffDetailsForm(forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    phone = forms.CharField(max_length=20)
    national_id = forms.CharField(max_length=20)
    email = forms.EmailField(required=False)          # optional

    def __init__(self, *args, tenant, instance=None, require_id=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.tenant, self.instance = tenant, instance
        self.fields["national_id"].required = require_id
        if instance is not None and not self.is_bound:
            self.initial = {
                "first_name": instance.first_name, "last_name": instance.last_name,
                "phone": instance.phone, "national_id": instance.national_id or "",
                "email": instance.email,
            }

    def clean_phone(self):
        try:
            return normalize_phone(self.cleaned_data["phone"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc))

    def clean_email(self):
        return self.cleaned_data["email"].lower().strip()

    def clean_national_id(self):
        value = self.cleaned_data["national_id"].strip().upper()
        if not value:
            return None
        if not ID_RE.match(value):
            raise forms.ValidationError("ID must be 6-12 letters or digits.")
        clash = get_user_model().objects.filter(tenant=self.tenant, national_id=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Someone in your shop already has this ID.")
        return value
