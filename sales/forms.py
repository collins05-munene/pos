from django import forms
from decimal import Decimal

REASON_CHOICES = [
    ('BANK_DEPOSIT', 'Bank Deposit / Skim'),
    ('SAFE_DROP', 'Safe Drop (Vault Transfer)'),
    ('PETTY_CASH', 'Petty Cash Expense'),
    ('SUPPLIER_PAYMENT', 'Direct Cash Supplier Payment'),
    ('OTHER', 'Other Cash Outflow'),
]

class CashOutForm(forms.Form):
    amount = forms.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        min_value=Decimal('0.01'),
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': '0.00'})
    )
    reason = forms.ChoiceField(
        choices=REASON_CHOICES,
        widget=forms.Select(attrs={'class': 'form-select'})
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3, 'placeholder': 'Optional details...'})
    )

    def __init__(self, *args, session=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = session

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if self.session and amount:
            # Check if user is attempting to take out more cash than estimated in register
            if amount > self.session.expected_closing_balance:
                raise forms.ValidationError(
                    f"Cannot remove KSH {amount:.2f}. The estimated available drawer balance is only KSH {self.session.expected_closing_balance:.2f}."
                )
        return amount