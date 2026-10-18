from django.urls import path
from .views import ProfileView, SignupView

app_name = "tenants"
urlpatterns = [
    path("", SignupView.as_view(), name="signup"),
    path("profile/", ProfileView.as_view(), name="my-profile"),
]