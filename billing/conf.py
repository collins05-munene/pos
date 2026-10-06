"""Pricing/policy knobs. Override any of them with settings.BILLING = {...}."""
from decimal import Decimal

from django.conf import settings

DEFAULTS = {
    "CURRENCY": "KES",
    "MONTHLY_PRICE": Decimal("10"),
    "INSTALL_FEE": Decimal("20"),         
    "TERM_DISCOUNT_PERCENT": {1: 0, 3: 30, 6: 30, 12: 30},
    "GRACE_DAYS": 5,           
    "TRIAL_DAYS": 0,         
    "RENEWAL_NOTICE_DAYS": 7,       
    "CANCEL_AFTER_SUSPENDED_DAYS": 60,     
}


def get(name):
    return {**DEFAULTS, **getattr(settings, "BILLING", {})}[name]
