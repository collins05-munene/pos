from django import forms
from django.contrib.auth.forms import AuthenticationForm

from tenants.services import compose_username


class StandardLoginForm(AuthenticationForm):
    """
    Owners log in with their email (leave business code blank).
    Managers log in with business code + username.
    """
    business_code = forms.CharField(
        required=False, max_length=40,
        widget=forms.TextInput(attrs={'placeholder': 'Business code (staff only)'}),
    )

    def clean(self):
        code = (self.cleaned_data.get("business_code") or "").strip()
        username = self.cleaned_data.get("username")
        if username:
            if code:
                self.cleaned_data["username"] = compose_username(code, username)
            elif "@" in username:
                self.cleaned_data["username"] = username.strip().lower()
        return super().clean()


class CashierPinLoginForm(forms.Form):
    business_code = forms.CharField(
        max_length=40,
        widget=forms.TextInput(attrs={'placeholder': 'Business code', 'autofocus': True,
                                      'autocapitalize': 'none'}))
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={'placeholder': 'Cashier Username', 'autocapitalize': 'none'}))
    pin = forms.CharField(widget=forms.PasswordInput(attrs={'placeholder': 'Enter PIN'}), max_length=6)

    def clean(self):
        cleaned = super().clean()
        code, handle = cleaned.get("business_code"), cleaned.get("username")
        if code and handle:
            # The view's `form.cleaned_data.get('username')` now receives the composed name.
            cleaned["username"] = compose_username(code, handle)
        return cleaned
