from django.urls import path
from .views import SignupView

app_name = "tenants"
urlpatterns = [path("", SignupView.as_view(), name="signup")]
