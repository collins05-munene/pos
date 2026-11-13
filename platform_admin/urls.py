from django.urls import path
from . import payment_views, receipt_views, reset_views, views

app_name = "platform_admin"
urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("tenants/", views.TenantListView.as_view(), name="tenants"),
    path("tenants/<int:pk>/", views.TenantDetailView.as_view(), name="tenant-detail"),
    path("tenants/<int:pk>/<slug:action>/", views.TenantActionView.as_view(), name="tenant-action"),

    path("payments/", payment_views.PaymentsPageView.as_view(), name="payments"),
    path("payments/api/", payment_views.PaymentsListView.as_view(), name="payments-api"),
    path("payments/api/<int:pk>/recheck/", payment_views.PaymentRecheckView.as_view(), name="payments-recheck"),

    path("resets/", reset_views.ResetsPageView.as_view(), name="resets"),
    path("resets/api/", reset_views.ResetsListView.as_view(), name="resets-api"),
    path("resets/api/<int:pk>/<slug:action>/", reset_views.ResetActionView.as_view(), name="resets-action"),

    path("receipts/", receipt_views.ReceiptsPageView.as_view(), name="receipts"),
    path("receipts/api/", receipt_views.ReceiptsListView.as_view(), name="receipts-api"),
    path("receipts/api/generate/", receipt_views.ReceiptGenerateView.as_view(), name="receipts-generate"),
    path("receipts/invoices/<str:number>/", receipt_views.InvoiceReceiptView.as_view(), name="invoice-receipt"),
    path("receipts/<str:number>/download/", receipt_views.ReceiptDownloadView.as_view(), name="receipt-download"),
    # keep after the download route: its <slug:action> would otherwise swallow 'download'
    path("receipts/<str:number>/<slug:action>/", receipt_views.ReceiptVoidView.as_view(), name="receipt-void"),
]
