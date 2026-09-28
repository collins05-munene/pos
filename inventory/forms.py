from decimal import Decimal

from django import forms

from .models import PurchaseOrder, PurchaseOrderItem, StockAdjustment, PurchasePayment


class PurchaseOrderForm(forms.ModelForm):
    """
    Records a stock purchase. By default it is an immediate, real-world
    purchase: inventory is increased right away, and any payment made
    now is recorded against the branch cash pool / payables. Uncheck
    "receive now" for an advance order to be received later.
    """
    receive_immediately = forms.BooleanField(
        required=False,
        initial=True,
        label="Stock has arrived - receive it now",
        help_text="Increases inventory immediately. Uncheck for an advance order to receive later.",
    )
    payment_method = forms.ChoiceField(
        choices=(('', 'Not paying now (credit / unpaid)'),) + PurchasePayment.PAYMENT_METHODS,
        required=False,
        label="Pay with",
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    amount_paid = forms.DecimalField(
        required=False,
        min_value=Decimal('0'),
        initial=Decimal('0.00'),
        max_digits=12,
        decimal_places=2,
        label="Amount paid now (KSH)",
        help_text="Leave at 0 to record this purchase fully on credit.",
        widget=forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01', 'placeholder': '0.00'}),
    )
    payment_reference = forms.CharField(
        required=False,
        max_length=100,
        label="Payment reference",
        help_text="Bank/M-Pesa transaction code or receipt number, if any.",
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Optional'}),
    )

    class Meta:
        model = PurchaseOrder
        fields = ['supplier', 'branch', 'notes']
        widgets = {
            'supplier': forms.Select(attrs={'class': 'form-select'}),
            'branch': forms.Select(attrs={'class': 'form-select'}),
            'notes': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3, 'placeholder': 'Optional notes'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['supplier'].required = False
        self.fields['supplier'].empty_label = "No supplier (market / individual / other source)"

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get('payment_method')
        amount = cleaned.get('amount_paid') or Decimal('0.00')

        if amount > 0 and not method:
            self.add_error('payment_method', "Select a payment method for the amount you're paying now.")
        if method and amount <= 0:
            self.add_error('amount_paid', "Enter an amount, or clear the payment method to record this on credit.")
        return cleaned


class PurchaseOrderItemForm(forms.ModelForm):
    class Meta:
        model = PurchaseOrderItem
        fields = ['variant', 'quantity_ordered', 'unit_cost']
        widgets = {
            'variant': forms.Select(attrs={'class': 'form-select'}),
            'quantity_ordered': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.001', 'min': '0.001', 'placeholder': 'Qty'}),
            'unit_cost': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01', 'min': '0', 'placeholder': 'Cost / unit'}),
        }


PurchaseOrderItemFormSet = forms.inlineformset_factory(
    PurchaseOrder,
    PurchaseOrderItem,
    form=PurchaseOrderItemForm,
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class StockAdjustmentForm(forms.ModelForm):
    class Meta:
        model = StockAdjustment
        fields = ['branch', 'variant', 'adjustment_type', 'quantity_changed', 'reason']
        widgets = {
            'branch': forms.Select(attrs={'class': 'form-select'}),
            'variant': forms.Select(attrs={'class': 'form-select'}),
            'adjustment_type': forms.Select(attrs={'class': 'form-select'}),
            'quantity_changed': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.001'}),
            'reason': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 2}),
        }


class PurchasePaymentForm(forms.Form):
    """Additional payment against an existing purchase (settling credit / balance)."""
    payment_method = forms.ChoiceField(choices=PurchasePayment.PAYMENT_METHODS)
    amount = forms.DecimalField(
        min_value=Decimal('0.01'),
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(attrs={'class': 'form-input', 'step': '0.01'}),
    )
    reference = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Optional reference'}),
    )