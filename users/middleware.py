from datetime import timedelta
from django.utils import timezone
from .models import User

TOUCH_EVERY = timedelta(seconds=60)


class LastSeenMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            now = timezone.now()
            if user.last_seen is None or now - user.last_seen > TOUCH_EVERY:
                # .update() skips signals and model save hooks, and costs one tiny query a minute
                User.objects.filter(pk=user.pk).update(last_seen=now)
        return response