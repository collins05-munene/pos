from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinLengthValidator, RegexValidator
from django.contrib.auth.hashers import make_password


# Create your models here.
class User(AbstractUser):
    class Roles(models.TextChoices):
        ADMIN = "ADMIN", "Admin"
        MANAGER = "MANAGER", "Manager"
        CASHIER = "CASHIER", "Cashier"

    role = models.CharField(max_length=15, choices=Roles.choices, default=Roles.CASHIER)
    pin = models.CharField(max_length=28, blank=True, null=True, validators=[RegexValidator(r'^\d{4,6}$', 'Pin must be 4  to 6 digits')], help_text="Hashed 4-6 digit PIN for cashier login.")

    def save(self, *store_instance, **kwargs):
        if self.pin and not self.pin.startswith('pbkdf2_'):
            self.pin = make_password(self.pin)
        super().save(*store_instance, **kwargs)
    
    @property
    def is_admin(self):
        return self.role == self.Roles.ADMIN or self.is_superuser
    @property
    def is_manager(self):
        return self.role == self.Roles.MANAGER
    

class ActivityLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=255)
    details = models.TextField(blank=True, null=True)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
    
    def __str__(self):
        return f"User: {self.user} IP: {self.ip_address} Action: {self.action} at {self.timestamp}"
