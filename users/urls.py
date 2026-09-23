from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.StandardLoginView.as_view(), name='login'),
    path('pos-login/', views.CashierPINLoginView.as_view(), name='pin_login'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
    path('logs/', views.ActivityLogListView.as_view(), name='activity-logs'),
    path('logs/<int:pk>/', views.activity_log_detail, name='activity-log-detail'),
    path('admin/dashboard/', views.AdminDashboardView.as_view(), name='admin-dashboard'),
    path('cashier/dashboard/', views.CashierDashboardView.as_view(), name='cashier-dashboard')
    
]   