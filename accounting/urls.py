from django.urls import path

from . import views as v

app_name = "accounting"

urlpatterns = [
    path("", v.OverviewView.as_view(), name="overview"),
    path("setup/", v.SetupView.as_view(), name="setup"),

    # expenses
    path("expenses/", v.ExpenseListView.as_view(), name="expenses"),
    path("expenses/new/", v.ExpenseCreateView.as_view(), name="expense-new"),
    path("expenses/categories/", v.CategoryView.as_view(), name="categories"),
    path("expenses/categories/<int:pk>/toggle/", v.CategoryToggleView.as_view(), name="category-toggle"),
    path("expenses/recurring/", v.RecurringListView.as_view(), name="recurring"),
    path("expenses/recurring/<int:pk>/toggle/", v.RecurringToggleView.as_view(), name="recurring-toggle"),
    path("expenses/<int:pk>/", v.ExpenseDetailView.as_view(), name="expense-detail"),
    path("expenses/<int:pk>/edit/", v.ExpenseEditView.as_view(), name="expense-edit"),
    path("expenses/<int:pk>/pay/", v.ExpensePayView.as_view(), name="expense-pay"),
    path("expenses/<int:pk>/cancel/", v.ExpenseCancelView.as_view(), name="expense-cancel"),
    path("expenses/<int:pk>/receipt/", v.ReceiptView.as_view(), name="receipt"),
    path("payments/<int:pk>/void/", v.PaymentVoidView.as_view(), name="payment-void"),

    # other income
    path("income/", v.IncomeListView.as_view(), name="income"),
    path("income/new/", v.IncomeCreateView.as_view(), name="income-new"),
    path("income/<int:pk>/receive/", v.IncomeReceiveView.as_view(), name="income-receive"),
    path("income/<int:pk>/cancel/", v.IncomeCancelView.as_view(), name="income-cancel"),

    # statements
    path("profit-loss/", v.ReportView.as_view(key="profit-loss"), name="profit-loss"),
    path("cash-flow/", v.ReportView.as_view(key="cash-flow"), name="cash-flow"),
    path("balance-sheet/", v.ReportView.as_view(key="balance-sheet"), name="balance-sheet"),

    # assets and liabilities
    path("assets/", v.AssetsView.as_view(), name="assets"),
    path("assets/new/", v.AssetCreateView.as_view(), name="asset-new"),
    path("assets/depreciation/", v.DepreciationRunView.as_view(), name="depreciation-run"),
    path("assets/<int:pk>/dispose/", v.AssetDisposeView.as_view(), name="asset-dispose"),
    path("loans/new/", v.LoanCreateView.as_view(), name="loan-new"),
    path("loans/<int:pk>/", v.LoanDetailView.as_view(), name="loan"),
    path("owner/new/", v.OwnerCreateView.as_view(), name="owner-new"),

    # ledger
    path("ledger/", v.LedgerView.as_view(), name="ledger"),
    path("ledger/journal/new/", v.JournalCreateView.as_view(), name="journal-new"),
    path("ledger/journal/<int:pk>/", v.JournalDetailView.as_view(), name="journal"),
    path("ledger/journal/<int:pk>/reverse/", v.JournalReverseView.as_view(), name="journal-reverse"),
    path("ledger/accounts/new/", v.AccountCreateView.as_view(), name="account-new"),
    path("ledger/accounts/<int:pk>/toggle/", v.AccountToggleView.as_view(), name="account-toggle"),
    path("ledger/catch-up/", v.CatchUpView.as_view(), name="catch-up"),
    path("ledger/classify/<int:pk>/", v.ClassifyView.as_view(), name="classify"),
    path("ledger/revalue/", v.RevalueView.as_view(), name="revalue"),
    path("settings/", v.SettingsView.as_view(), name="settings"),

    # analysis and reports
    path("analysis/", v.AnalysisView.as_view(), name="analysis"),
    path("reports/", v.ReportsHubView.as_view(), name="reports"),
    path("reports/<slug:key>/", v.ReportView.as_view(), name="report"),
    path("reports/<slug:key>/<str:fmt>/", v.ReportView.as_view(), name="report-export"),
]
