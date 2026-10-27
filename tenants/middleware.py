from django.http import JsonResponse

from .branch_context import reset_current_branch, set_current_branch
from .context import reset_current_tenant, set_current_tenant
from .models import Tenant

SUPPORT_SESSION_KEY = "platform_support_tenant_id"


class TenantMiddleware:
    """
    Place AFTER AuthenticationMiddleware.

    The tenant is derived from the authenticated user, never from anything the
    client can freely choose. An optional `X-Tenant: <slug>` header is accepted
    only as a consistency check (useful for API clients / the PWA).

    Platform superusers have no tenant; they only get one while in an explicit,
    audited "support session" (see platform_admin views).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = self._resolve(request)
        request.tenant = tenant

        hinted = request.headers.get("X-Tenant")
        if hinted and tenant is not None and hinted != tenant.slug:
            return JsonResponse({"error": "tenant_mismatch"}, status=403)

        token = set_current_tenant(tenant)
        branch_token = None
        try:
            request.branch = self._resolve_branch(request, tenant)
            branch_token = set_current_branch(request.branch)
            return self.get_response(request)
        finally:
            if branch_token is not None:
                reset_current_branch(branch_token)
            reset_current_tenant(token)

    @staticmethod
    def _resolve_branch(request, tenant):
        """Needs the tenant context already set (branch lookups are tenant-scoped)."""
        if tenant is None:
            return None
        from .branches import resolve_active_branch
        user = request.user
        if user.is_superuser:           # support session: no home branch
            return None
        return resolve_active_branch(user, request.session)

    @staticmethod
    def _resolve(request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return None
        if user.is_superuser:
            tenant_id = request.session.get(SUPPORT_SESSION_KEY)
            return Tenant.objects.filter(pk=tenant_id).first() if tenant_id else None
        return user.tenant  # None for orphaned users -> scoped managers return nothing
