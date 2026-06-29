from django import forms
from django.contrib.auth.forms import AuthenticationForm

class StandardLoginForm(AuthenticationForm):
    pass

class CashierPinLoginForm(forms.Form):
    username = forms.CharField(max_length=150, widget=forms.TextInput(attrs={'placeholder': 'Cashier Username', 'autofocus': True}))
    pin = forms.CharField(widget=forms.PasswordInput(attrs={'placeholder': 'Enter PIN'}), max_length=6)