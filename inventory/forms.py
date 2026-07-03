from django import forms
from django.forms import inlineformset_factory

from . import models
class PurchaseOrderForm(forms.ModelForm):
    class Meta:
        model = models.PurchaseOrder
        fields = ['po_number', 'supplier', 'branch', 'notes']
        widgets = {
            'po_number': forms.TextInput(attrs={'class': 'form-control'}),
            'supplier': forms.Select(attrs={'class': 'form-control'}),
            'branch': forms.Select(attrs={'class': 'form-control'}),
            'notes':forms.Textarea(attrs={'class': 'form-control', 'rows': 2})
        }

PurchaseOrderItemFormSet = inlineformset_factory(
    models.PurchaseOrder,
    models.PurchaseOrderItem,
    fields=['variant', 'quantity_ordered', 'unit_cost'],
    extra=1,
    can_delete=True,
    widgets={
        'variant': forms.Select(attrs={'class': 'form-control'}),
        'quantity_ordered': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.001'}),
        'unit_cost': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.001'})
        }
)

class StockAdjustmentForm(forms.ModelForm):
    class Meta:
        model = models.StockAdjustment
        fields = ['branch', 'variant', 'adjustment_type', 'quantity_changed', 'reason']
        widgets = {
            'branch': forms.Select(attrs={'class': 'form-control'}),
            'variant': forms.Select(attrs={'class': 'form-control'}),
            'adjustment_type': forms.Select(attrs={'class': 'form-control'}),
            'quantity_changed': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.001'}),
            'reason': forms.Textarea(attrs={'class': 'form-control', 'rows': 2})
        }
