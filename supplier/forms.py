from django import forms
from .models import Supplier

class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ['name', 'contact_person', 'email', 'phone', 'address', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Supplier Company Name'}),
            'contact_person': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Contact Name'}),
            'email': forms.EmailInput(attrs={'class': 'form-input', 'placeholder': 'email@supplier.com'}),
            'phone': forms.TextInput(attrs={'class': 'form-input', 'placeholder': '+123456789'}),
            'address': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3, 'placeholder': 'Street address...'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
        }