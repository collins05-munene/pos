from django.urls import path
from . import views
from django.views.generic import TemplateView

urlpatterns = [
    path('login/', views.StandardLoginView.as_view(), name='login'),
    path('pos-login/', views.CashierPINLoginView.as_view(), name='pin_login'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
    path('logs/', views.ActivityLogListView.as_view(), name='activity-logs'),
    path('logs/<int:pk>/', views.ActivityLogDetailView.as_view(), name='activity-log-detail'),
    path('admin/dashboard/', views.AdminDashboardView.as_view(), name='admin-dashboard'),
    path('cashier/dashboard/', views.CashierDashboardView.as_view(), name='cashier-dashboard'),
    path('dashboard/live/version/', views.DashboardVersionView.as_view(), name='dashboard-version'),
    path('dashboard/live/body/',    views.DashboardBodyView.as_view(),    name='dashboard-body'),

    path("terms/",   TemplateView.as_view(template_name="users/terms.html"),   name="terms"),
    path("privacy/", TemplateView.as_view(template_name="users/privacy.html"), name="privacy"),

    
]   