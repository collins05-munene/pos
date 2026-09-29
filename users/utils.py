from tenants.context import get_current_tenant

from .models import ActivityLog


class AuditAction:
    LOGIN = "LOGIN"
    LOGIN_PIN = "PIN_LOGIN"
    LOGOUT = "LOGOUT"

    CART_ADD = "CART_ADD"
    CART_UPDATE = "CART_UPDATE"
    CART_REMOVE = "CART_REMOVE"

    SALE_CASH = "SALE_CASH_COMPLETED"
    SALE_MPESA = "SALE_MPESA_COMPLETED"
    MPESA_STK_INITIATED = "MPESA_STK_INITIATED"
    MPESA_STK_FAILED = "MPESA_STK_FAILED"
    MPESA_CALLBACK_SUCCESS = "MPESA_CALLBACK_SUCCESS"
    MPESA_CALLBACK_FAILED = "MPESA_CALLBACK_FAILED"

    RECORD_CREATE = "RECORD_CREATE"
    RECORD_UPDATE = "RECORD_UPDATE"
    RECORD_DELETE = "RECORD_DELETE"


def log_action(user, action, details="", request=None, tenant=None):
    ip = None
    if request is not None:
        ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR')

    # At login time the middleware ran before the user was authenticated, so the
    # request has no tenant yet: fall back to the user's own, then to an explicit one.
    if tenant is None:
        tenant = get_current_tenant() or getattr(user, "tenant", None)

    ActivityLog.objects.create(
        tenant=tenant,
        user=user if getattr(user, 'is_authenticated', False) else None,
        action=action,
        details=details,
        ip_address=ip,
    )
