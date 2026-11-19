import datetime

from django import forms

from inventory.models import Branch

from .models import (PAYMENT_METHODS, Account, AccountingSettings, Expense, ExpenseCategory, FixedAsset, Loan,
                     OtherIncome, OwnerTransaction, RecurringExpense)
from .scope import allowed_branch_ids

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
METHOD = [("", "Select...")] + list(PAYMENT_METHODS)
PAY_METHOD = list(PAYMENT_METHODS)


def _branches(tenant, user=None, blank="Head office"):
    qs = Branch.all_objects.filter(tenant=tenant).order_by("name")
    allowed = allowed_branch_ids(user) if user is not None else None
    if allowed is not None:
        qs = qs.filter(pk__in=allowed)
        blank = None
    return qs, blank


class TenantForm(forms.Form):
    def __init__(self, *args, tenant=None, user=None, **kwargs):
        self.tenant, self.user = tenant, user
        super().__init__(*args, **kwargs)


class ExpenseForm(TenantForm):
    title = forms.CharField(max_length=150)
    category = forms.ModelChoiceField(ExpenseCategory.objects.none())
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01)
    expense_date = forms.DateField(widget=DATE, initial=datetime.date.today, help_text="When the cost was incurred.")
    due_date = forms.DateField(widget=DATE, required=False, help_text="When it must be paid by (optional).")
    branch = forms.ModelChoiceField(Branch.objects.none(), required=False)
    vendor = forms.CharField(max_length=150, required=False, label="Vendor / payee")
    payment_method = forms.ChoiceField(choices=METHOD, required=False, label="Expected payment method")
    reference = forms.CharField(max_length=100, required=False, label="Reference number")
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)
    receipt = forms.FileField(required=False, help_text="Photo or PDF of the receipt (max 5 MB).")

    def __init__(self, *args, show_recurring=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ExpenseCategory.all_objects.filter(tenant=self.tenant, is_active=True).order_by("name")
        qs, blank = _branches(self.tenant, self.user)
        self.fields["branch"].queryset = qs
        self.fields["branch"].empty_label = blank
        self.fields["branch"].required = blank is None
        self.branch_list = list(qs) if blank else []
        if show_recurring:
            self.fields["repeat"] = forms.ChoiceField(
                choices=[("", "Does not repeat")] + list(RecurringExpense.Frequency.choices), required=False,
                label="Repeat this expense", help_text="Creates an unpaid expense each period. Nothing is paid automatically.")
            self.fields["repeat_until"] = forms.DateField(widget=DATE, required=False, label="Stop repeating after")
        if blank:   # head-office users may split an expense across branches
            for b in self.branch_list:
                self.fields[f"alloc_{b.pk}"] = forms.DecimalField(
                    required=False, min_value=0, max_value=100, max_digits=5, decimal_places=2,
                    label=f"% to {b.name}", help_text="Head-office expenses only. Percentages must total 100." if b == self.branch_list[0] else "")

    def clean_receipt(self):
        f = self.cleaned_data.get("receipt")
        if f:
            if f.size > 5 * 1024 * 1024:
                raise forms.ValidationError("The receipt is larger than 5 MB.")
            ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
            if ext not in ("jpg", "jpeg", "png", "pdf", "webp", "heic"):
                raise forms.ValidationError("Upload a photo (JPG, PNG, WEBP) or a PDF.")
        return f

    def allocations(self):
        out = []
        for b in self.branch_list:
            v = self.cleaned_data.get(f"alloc_{b.pk}")
            if v:
                out.append((b, v))
        return out


class PaymentForm(TenantForm):
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01)
    payment_date = forms.DateField(widget=DATE, initial=datetime.date.today)
    payment_method = forms.ChoiceField(choices=PAY_METHOD)
    reference = forms.CharField(max_length=100, required=False, label="Reference (M-Pesa code, cheque no.)")
    account = forms.ModelChoiceField(Account.objects.none(), required=False, label="Paid from account",
                                     help_text="Leave blank to use the account that matches the method.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.all_objects.filter(
            tenant=self.tenant, account_type="ASSET", is_header=False, is_active=True, code__startswith="10")


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=255)


class CategoryForm(TenantForm):
    name = forms.CharField(max_length=80)
    account = forms.ModelChoiceField(Account.objects.none(), label="Ledger account",
                                     help_text="Where this category's costs appear in the profit and loss.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.all_objects.filter(
            tenant=self.tenant, is_header=False, is_active=True, account_type__in=["OPEX", "OTHER_EXPENSE"]).order_by("code")


class IncomeForm(TenantForm):
    title = forms.CharField(max_length=150)
    kind = forms.ChoiceField(choices=OtherIncome.Kind.choices, label="Type of income")
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01)
    income_date = forms.DateField(widget=DATE, initial=datetime.date.today, label="Date earned")
    received_on = forms.DateField(widget=DATE, required=False, label="Date received", help_text="Leave blank if you have not been paid yet.")
    payment_method = forms.ChoiceField(choices=METHOD, required=False, label="Received by")
    branch = forms.ModelChoiceField(Branch.objects.none(), required=False)
    payer = forms.CharField(max_length=150, required=False, label="Paid by")
    reference = forms.CharField(max_length=100, required=False)
    description = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs, blank = _branches(self.tenant, self.user)
        self.fields["branch"].queryset, self.fields["branch"].empty_label = qs, blank

    def clean(self):
        d = super().clean()
        if d.get("received_on") and not d.get("payment_method"):
            self.add_error("payment_method", "Say how the money was received.")
        return d


class ReceiveIncomeForm(forms.Form):
    received_on = forms.DateField(widget=DATE, initial=datetime.date.today)
    payment_method = forms.ChoiceField(choices=PAY_METHOD)


class OwnerForm(TenantForm):
    kind = forms.ChoiceField(choices=OwnerTransaction.Kind.choices)
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01)
    txn_date = forms.DateField(widget=DATE, initial=datetime.date.today, label="Date")
    payment_method = forms.ChoiceField(choices=PAY_METHOD, label="Paid in / out by")
    branch = forms.ModelChoiceField(Branch.objects.none(), required=False)
    reference = forms.CharField(max_length=100, required=False)
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs, blank = _branches(self.tenant, self.user)
        self.fields["branch"].queryset, self.fields["branch"].empty_label = qs, blank


class AssetForm(TenantForm):
    name = forms.CharField(max_length=150)
    category = forms.ChoiceField(choices=FixedAsset.Category.choices)
    cost = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01, label="Purchase cost")
    purchase_date = forms.DateField(widget=DATE, initial=datetime.date.today)
    useful_life_months = forms.IntegerField(min_value=1, max_value=600, label="Useful life (months)")
    residual_value = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, initial=0, label="Residual value")
    branch = forms.ModelChoiceField(Branch.objects.none(), required=False)
    paid_with = forms.ChoiceField(choices=[("", "Select...")] + list(PAYMENT_METHODS) + [("CREDIT", "On credit (unpaid)")], required=False)
    existing_asset = forms.BooleanField(required=False, label="Bought before my books started",
                                        help_text="Tick this if its cost is already inside your opening balances.")
    opening_accumulated_depreciation = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False, initial=0,
                                                          label="Depreciation already taken (existing assets)")
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs, blank = _branches(self.tenant, self.user)
        self.fields["branch"].queryset, self.fields["branch"].empty_label = qs, blank


class DisposeForm(forms.Form):
    disposal_date = forms.DateField(widget=DATE, initial=datetime.date.today)
    proceeds = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, initial=0, label="Sale proceeds")
    method = forms.ChoiceField(choices=METHOD, required=False, label="Received by")


class LoanForm(TenantForm):
    lender = forms.CharField(max_length=150)
    principal = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01)
    interest_rate_percent = forms.DecimalField(max_digits=6, decimal_places=3, min_value=0, label="Interest rate (% per year)")
    start_date = forms.DateField(widget=DATE, initial=datetime.date.today)
    installments = forms.IntegerField(min_value=1, max_value=600, initial=12, label="Number of repayments")
    frequency = forms.ChoiceField(choices=Loan.Frequency.choices, initial="MONTHLY")
    received_via = forms.ChoiceField(choices=METHOD, required=False, label="Money received by")
    branch = forms.ModelChoiceField(Branch.objects.none(), required=False)
    existing_loan = forms.BooleanField(required=False, label="Taken before my books started",
                                       help_text="Tick this if the balance is already in your opening balances.")
    opening_outstanding = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, required=False,
                                             label="Still owed when my books started")
    notes = forms.CharField(max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs, blank = _branches(self.tenant, self.user)
        self.fields["branch"].queryset, self.fields["branch"].empty_label = qs, blank


class RepayForm(forms.Form):
    paid_on = forms.DateField(widget=DATE, initial=datetime.date.today)
    principal_paid = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, initial=0, label="Principal paid")
    interest_paid = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0, initial=0, label="Interest paid")
    payment_method = forms.ChoiceField(choices=PAY_METHOD)
    reference = forms.CharField(max_length=100, required=False)


class AccountForm(TenantForm):
    code = forms.CharField(max_length=10, help_text="1xxx assets, 2xxx liabilities, 3xxx equity, 4xxx revenue, 5xxx cost of sales, 6xxx expenses, 7xxx finance costs.")
    name = forms.CharField(max_length=120)
    account_type = forms.ChoiceField(choices=Account.Type.choices)
    description = forms.CharField(max_length=255, required=False)


class SettingsForm(forms.ModelForm):
    class Meta:
        model = AccountingSettings
        fields = ["reporting_basis", "currency_code", "fiscal_year_start_month", "default_tax_rate_percent", "managers_can_manage_expenses"]
        labels = {"reporting_basis": "Default reporting basis", "currency_code": "Currency code",
                  "fiscal_year_start_month": "Financial year starts in month (1-12)",
                  "default_tax_rate_percent": "Tax rate % (optional)", "managers_can_manage_expenses": "Branch managers can record expenses for their branch"}
        help_texts = {"default_tax_rate_percent": "Left blank, no tax rate is assumed. Tax is only what you record.",
                      "reporting_basis": "Changes how reports are built. It never changes recorded entries."}

    def clean_fiscal_year_start_month(self):
        v = self.cleaned_data["fiscal_year_start_month"]
        if not 1 <= v <= 12:
            raise forms.ValidationError("Choose a month from 1 to 12.")
        return v

    def clean_currency_code(self):
        v = self.cleaned_data["currency_code"].strip().upper()
        if len(v) != 3 or not v.isalpha():
            raise forms.ValidationError("Use a three-letter code such as KES.")
        return v
