# urls.py
from django.urls import path
from .views import UnreadNotificationsView, MarkNotificationReadView

urlpatterns = [
    path('api/notifications/unread/', UnreadNotificationsView.as_view(), name='unread-notifications'),
    path('api/notifications/<int:pk>/read/', MarkNotificationReadView.as_view(), name='mark-notification-read'),
]