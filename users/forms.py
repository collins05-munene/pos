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
    shop_code = forms.CharField(label="Shop Code", required=True)
    username = forms.CharField(label="Cashier Username", required=True)
    pin = forms.CharField(label="PIN", widget=forms.PasswordInput, required=True)

    def clean(self):
        cleaned_data = super().clean()
        shop_code = (cleaned_data.get("shop_code") or "").strip().lower()
        handle = (cleaned_data.get("username") or "").strip().lower()

        if shop_code and handle:
            # Build the composed username expected by PINAuthenticationBackend
            if "__" in handle:
                cleaned_data["composed_username"] = handle
            else:
                cleaned_data["composed_username"] = f"{shop_code}__{handle}"
        return cleaned_data
