from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password

from tenants.services import compose_username


class LoginForm(AuthenticationForm):
    """
    The one login form, for everybody.

    * Owner / platform admin: username (the owner's username is their email) + password.
    * Staff (managers, cashiers): username + password, plus the shop code -- staff usernames are only
      unique inside one shop, so the code is what says WHICH shop's "njeri" is signing in.
      Typing the full `shopcode__njeri` into the username box works too.
    """
    shop_code = forms.CharField(required=False, max_length=40)

    def clean(self):
        code = (self.cleaned_data.get("shop_code") or "").strip()
        username = (self.cleaned_data.get("username") or "").strip()
        if username:
            if code:
                username = compose_username(code, username)
            elif "@" in username:
                username = username.lower()
            self.cleaned_data["username"] = username
        return super().clean()

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Wrong username or password. Staff: check your shop code too.",
    }


class PasswordResetRequestForm(forms.Form):
    shop_code = forms.CharField(required=False, max_length=40)
    username = forms.CharField(max_length=150)

    def clean(self):
        cleaned = super().clean()
        code = (cleaned.get("shop_code") or "").strip()
        username = (cleaned.get("username") or "").strip()
        if username:
            if code:
                username = compose_username(code, username)
            elif "@" in username:
                username = username.lower()
            cleaned["username"] = username
        return cleaned


class SetNewPasswordForm(forms.Form):
    password1 = forms.CharField(strip=False)
    password2 = forms.CharField(strip=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get("password1"), cleaned.get("password2")
        if p1 and p2:
            if p1 != p2:
                self.add_error("password2", "The two passwords don't match.")
            else:
                try:
                    validate_password(p1, user=self.user)
                except forms.ValidationError as exc:
                    self.add_error("password1", exc)
        return cleaned
