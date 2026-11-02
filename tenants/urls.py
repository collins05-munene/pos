from django.urls import path
from .views import CheckEmailView, ProfileView, ResendVerificationView, SignupView, VerifyEmailView

app_name = "tenants"
urlpatterns = [
    path("", SignupView.as_view(), name="signup"),
    path("check-email/", CheckEmailView.as_view(), name="check-email"),
    path("resend/", ResendVerificationView.as_view(), name="resend-verification"),
    path("verify/<str:token>/", VerifyEmailView.as_view(), name="verify-email"),
    path("profile/", ProfileView.as_view(), name="my-profile"),
]
