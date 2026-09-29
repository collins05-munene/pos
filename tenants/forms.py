from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password

from billing import conf
from billing.mpesa import normalize_phone
from .models import Tenant


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
