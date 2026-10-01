from django.urls import path
from . import views

app_name = "billing"
urlpatterns = [
    path("", views.BillingOverviewView.as_view(), name="billing-overview"),
    path("checkout/", views.CheckoutView.as_view(), name="checkout"),
    path("payments/<int:pk>/", views.PaymentStatusView.as_view(), name="payment-status"),
    path("locked/", views.locked_view, name="locked"),
    path("mpesa/callback/", views.MpesaCallbackView.as_view(), name="mpesa-callback"),
]
