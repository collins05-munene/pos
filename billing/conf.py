"""Pricing/policy knobs. Override any of them with settings.BILLING = {...}."""
from decimal import Decimal

from django.conf import settings

DEFAULTS = {
    "CURRENCY": "KES",
    "MONTHLY_PRICE": Decimal("10"),
    "INSTALL_FEE": Decimal("20"),          # one-off, added to the first invoice
    # % off the (monthly x months) list price when paying ahead
    "TERM_DISCOUNT_PERCENT": {1: 0, 3: 30, 6: 30, 12: 30},
    "GRACE_DAYS": 5,                          # access continues this long after period end
    "TRIAL_DAYS": 0,                          # 0 = must pay before first use
    "RENEWAL_NOTICE_DAYS": 7,                 # open the renewal invoice this early
    "CANCEL_AFTER_SUSPENDED_DAYS": 60,        # suspended this long -> cancelled
}


def get(name):
    return {**DEFAULTS, **getattr(settings, "BILLING", {})}[name]
