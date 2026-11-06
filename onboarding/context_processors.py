from django.utils.functional import SimpleLazyObject

from users.mixins import MANAGEMENT_ROLES


def onboarding(request):
    """
    Feeds the sidebar link and the dashboard banner. Lazy, so pages that never mention `onboarding`
    pay nothing, and only owners/admins/managers of a shop ever get it.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or user.is_superuser:
        return {}
    if getattr(request, "tenant", None) is None or getattr(user, "role", None) not in MANAGEMENT_ROLES:
        return {}

    def build():
        from .services import get_progress
        return get_progress(request.tenant)

    return {"onboarding": SimpleLazyObject(build)}
