# views.py
from django.http import JsonResponse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.decorators.cache import never_cache
from django.utils.decorators import method_decorator
from django.db import models

from .models import Notification

@method_decorator(never_cache, name='dispatch')
class UnreadNotificationsView(LoginRequiredMixin, View):
    def get(self, request):
        tenant = getattr(request, 'tenant', None)
        if not tenant:
            return JsonResponse({'unread_count': 0, 'notifications': []})

        # Fetch notifications for this tenant, either specifically for this user or tenant-wide
        qs = Notification.objects.filter(
            tenant=tenant,
            is_read=False
        ).filter(
            models.Q(user=request.user) | models.Q(user__isnull=True)
        )[:10]

        data = [
            {
                'id': n.id,
                'title': n.title,
                'message': n.message,
                'level': n.level,
                'created_at': n.created_at.strftime('%H:%M %b %d'),
            }
            for n in qs
        ]

        return JsonResponse({
            'unread_count': qs.count(),
            'notifications': data
        })


class MarkNotificationReadView(LoginRequiredMixin, View):
    def post(self, request, pk):
        tenant = getattr(request, 'tenant', None)
        Notification.objects.filter(pk=pk, tenant=tenant).update(is_read=True)
        return JsonResponse({'status': 'ok'})