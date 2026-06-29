from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password

User = get_user_model()

class PINAuthenticationBackend(ModelBackend):
    def authenticate(self, request, username=None, pin=None, **kwargs):
        if pin is None:
            return None
            
        try:
            user = User.objects.get(username=username)
            if user.role == User.Roles.CASHIER and user.pin:
                if check_password(pin, user.pin):
                    return user
        except User.DoesNotExist:
            return None
        return None

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
