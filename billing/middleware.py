from django.conf import settings
from django.http import HttpResponseRedirect, JsonResponse
from django.urls import reverse


def _exempt_prefixes():
    return tuple(getattr(settings, "BILLING_EXEMPT_PATH_PREFIXES", ())) + (
        "/billing/", "/signup/", "/static/", "/media/", settings.LOGIN_URL,
        "/service-worker.js", "/manifest.json", "/offline/",
    )


class SubscriptionGateMiddleware:
    """
    Place AFTER TenantMiddleware. Logged-in tenant users of an unpaid, expired or
    disabled tenant can reach only the exempt paths (billing, login, static, PWA files).
    Platform superusers are never gated. Add your logout URL to
    settings.BILLING_EXEMPT_PATH_PREFIXES.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (user is not None and user.is_authenticated and not user.is_superuser
                and not request.path.startswith(_exempt_prefixes())):
            tenant = getattr(request, "tenant", None)
            if tenant is None or not tenant.has_access:
                wants_json = (request.path.startswith("/api/")
                              or "application/json" in request.headers.get("Accept", ""))
                if wants_json:
                    return JsonResponse({"error": "subscription_inactive"}, status=402)
                return HttpResponseRedirect(reverse("billing:locked"))
        return self.get_response(request)
