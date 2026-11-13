from django.urls import path
from . import views

app_name = "billing"
urlpatterns = [
    path("", views.BillingOverviewView.as_view(), name="billing-overview"),
    path("checkout/", views.CheckoutView.as_view(), name="checkout"),
    path("payments/<str:reference>/", views.PaymentStatusView.as_view(), name="payment-status"),
    path("paystack/callback/", views.PaystackCallbackView.as_view(), name="paystack-callback"),
    path("paystack/webhook/", views.PaystackWebhookView.as_view(), name="paystack-webhook"),
    path("invoices/<str:number>/receipt/", views.InvoiceReceiptView.as_view(), name="invoice-receipt"),
    path("verify/<str:code>/", views.VerifyReceiptView.as_view(), name="verify"),
    path("locked/", views.locked_view, name="locked"),
]
