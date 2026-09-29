from django.urls import path
from . import views

app_name = "platform_admin"
urlpatterns = [
    path("", views.OverviewView.as_view(), name="overview"),
    path("tenants/", views.TenantListView.as_view(), name="tenants"),
    path("tenants/<int:pk>/", views.TenantDetailView.as_view(), name="tenant-detail"),
    path("tenants/<int:pk>/<slug:action>/", views.TenantActionView.as_view(), name="tenant-action"),
]
