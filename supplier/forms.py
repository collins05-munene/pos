from django import apps
from django import forms

from .models import Supplier, SupplierProduct

class SupplierForm(forms.ModelForm):
    class Meta:
        model = Supplier
        fields = [
            'company_name', 'contact_name', 'email', 'phone', 'address', 'payment_terms', 'tax_number', 'notes', 'is_active'
        ]

        widgets = {
            'company_name': forms.TextInput(attrs={'class': 'form-control'}),
            'contact_name': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'supplier@gmail.com'}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '0712 345 678'}),
            'address': forms.Textarea(attrs={'class': 'form-control', 'rows': 2, 'placeholder': 'Embu, Kenya or P.O. Box 98765 - 00100, Nairobi'}),
            'payment_terms': forms.Select(attrs={'class': 'form-control'}),
            'tax_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'VAT / Tax Registration No.'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'})
        }

SupplierProductFormset = forms.inlineformset_factory (
    Supplier,
    SupplierProduct,
    fields = ['product', 'supplier_sku', 'lead_time_days', 'is_primary'],
    extra=1,
    can_delete=True,
    widgets={
        'product': forms.Select(attrs={'class': 'form-control'}),
        'supplier_sku': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Supplier sku...'}),
        'lead_time_days': forms.NumberInput(attrs={'class': 'form-control', 'min': '0'}),
        'is_primary': forms.CheckboxInput(attrs={'class': 'form-check-input'})
    }
)