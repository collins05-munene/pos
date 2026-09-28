from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.hashers import check_password, make_password


class PINAuthenticationBackend(ModelBackend):
    """
    Cashier PIN login. `username` is the COMPOSED '<business_code>__<handle>'
    (CashierPinLoginForm builds it), which is what makes PIN login tenant-safe:
    a PIN is only ever checked against the one user that code+handle names.
    """

    def authenticate(self, request, username=None, pin=None, **kwargs):
        if pin is None or not username:
            return None
        User = get_user_model()
        try:
            user = User.objects.select_related("tenant").get(username=username)
        except User.DoesNotExist:
            make_password(pin)   # burn similar time so usernames can't be probed
            return None

        if (user.role == User.Roles.CASHIER and user.pin
                and user.tenant_id and user.tenant.is_active
                and check_password(pin, user.pin)
                and self.user_can_authenticate(user)):   # is_active
            return user
        return None

    def get_user(self, user_id):
        User = get_user_model()
        try:
            # select_related: TenantMiddleware reads user.tenant on every request
            user = User.objects.select_related("tenant").get(pk=user_id)
        except User.DoesNotExist:
            return None
        return user if self.user_can_authenticate(user) else None
