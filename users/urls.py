from django.urls import path
from . import views
from django.views.generic import TemplateView

urlpatterns = [
    path('login/', views.LoginView.as_view(), name='login'),
    # Old cashier PIN screen: kept only as a redirect so bookmarks / installed PWAs keep working.
    path('pos-login/', views.LegacyPosLoginRedirect.as_view(), name='pin_login'),
    path('logout/', views.LogoutView.as_view(), name='logout'),

    path('password-reset/', views.PasswordResetRequestView.as_view(), name='password-reset'),
    path('password-reset/sent/', views.PasswordResetSentView.as_view(), name='password-reset-sent'),
    path('password-reset/<str:token>/', views.PasswordResetConfirmView.as_view(), name='password-reset-confirm'),

    path('logs/', views.ActivityLogListView.as_view(), name='activity-logs'),
    path('logs/<int:pk>/', views.ActivityLogDetailView.as_view(), name='activity-log-detail'),
    path('admin/dashboard/', views.AdminDashboardView.as_view(), name='admin-dashboard'),
    path('cashier/dashboard/', views.CashierDashboardView.as_view(), name='cashier-dashboard'),
    path('dashboard/live/version/', views.DashboardVersionView.as_view(), name='dashboard-version'),
    path('dashboard/live/body/',    views.DashboardBodyView.as_view(),    name='dashboard-body'),

    path("terms/",   TemplateView.as_view(template_name="users/terms.html"),   name="terms"),
    path("privacy/", TemplateView.as_view(template_name="users/privacy.html"), name="privacy"),

    
]   