from django.urls import path
from . import payment_views, receipt_views, views

app_name = "platform_admin"
urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("tenants/", views.TenantListView.as_view(), name="tenants"),
    path("tenants/<int:pk>/", views.TenantDetailView.as_view(), name="tenant-detail"),
    path("tenants/<int:pk>/<slug:action>/", views.TenantActionView.as_view(), name="tenant-action"),

    path("payments/", payment_views.PaymentsPageView.as_view(), name="payments"),
    path("payments/api/", payment_views.PaymentsListView.as_view(), name="payments-api"),
    path("payments/api/parse/", payment_views.PaymentParseView.as_view(), name="payments-parse"),
    path("payments/api/add/", payment_views.PaymentAddView.as_view(), name="payments-add"),
    path("payments/api/codes/<int:pk>/void/", payment_views.PaymentVoidView.as_view(), name="payments-void"),
    path("payments/api/requests/<int:pk>/<slug:action>/", payment_views.RequestActionView.as_view(),
         name="payments-request-action"),

    path("receipts/", receipt_views.ReceiptsPageView.as_view(), name="receipts"),
    path("receipts/api/", receipt_views.ReceiptsListView.as_view(), name="receipts-api"),
    path("receipts/api/generate/", receipt_views.ReceiptGenerateView.as_view(), name="receipts-generate"),
    path("receipts/invoices/<str:number>/", receipt_views.InvoiceReceiptView.as_view(), name="invoice-receipt"),
    path("receipts/<str:number>/download/", receipt_views.ReceiptDownloadView.as_view(), name="receipt-download"),
    # keep after the download route: its <slug:action> would otherwise swallow 'download'
    path("receipts/<str:number>/<slug:action>/", receipt_views.ReceiptVoidView.as_view(), name="receipt-void"),
]
