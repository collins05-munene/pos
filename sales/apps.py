from django.apps import AppConfig


class SalesConfig(AppConfig):
    name = 'sales'

    def ready(self):
        from django.db.models.signals import post_save
        from users.dashboard_live import bump_on_commit
        from .models import Order, OrderItem            # your sale models
        from inventory.models import StockLevel
        for m in (Order, OrderItem, StockLevel):
            post_save.connect(bump_on_commit, sender=m, dispatch_uid=f"dash-bump-{m.__name__}")
