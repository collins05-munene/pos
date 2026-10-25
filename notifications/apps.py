from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = 'notifications'

    def ready(self):
        # Connects the billing_event receivers (payment received, part payment, credit, overdue...).
        # Without this import the rules in receivers.py never run and no billing notification is sent.
        from . import receivers  # noqa: F401
