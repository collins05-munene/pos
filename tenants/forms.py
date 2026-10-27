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
    # How many branches, and their names (one per line / JSON list). Branch 1 is included in the base price.
    branch_count = forms.IntegerField(min_value=1, initial=1, required=False)
    branch_names = forms.JSONField(required=False)

    def clean_branch_count(self):
        n = self.cleaned_data.get("branch_count") or 1
        if n > conf.get("MAX_BRANCHES"):
            raise forms.ValidationError(f"At most {conf.get('MAX_BRANCHES')} branches at signup.")
        return n

    def clean_branch_names(self):
        raw = self.cleaned_data.get("branch_names") or []
        if not isinstance(raw, list):
            raise forms.ValidationError("Branch names must be a list.")
        return [str(x).strip() for x in raw]

    def clean(self):
        data = super().clean()
        n = data.get("branch_count") or 1
        names = list(data.get("branch_names") or [])
        if "branch_count" in self.errors or "branch_names" in self.errors:
            return data
        names = (names + [""] * n)[:n]
        names = [nm or ("Main Branch" if i == 0 else f"Branch {i + 1}") for i, nm in enumerate(names)]
        if len({nm.lower() for nm in names}) != len(names):
            self.add_error("branch_names", "Give each branch a different name.")
        if any(len(nm) > 100 for nm in names):
            self.add_error("branch_names", "Branch names must be under 100 characters.")
        data["branch_count"], data["branch_names"] = n, names
        return data

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
