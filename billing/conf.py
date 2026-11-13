"""Pricing/policy knobs. Override any of them with settings.BILLING = {...}."""
from decimal import Decimal

from django.conf import settings

DEFAULTS = {
    "CURRENCY": "KES",
    "MONTHLY_PRICE": Decimal("1499"),
    "INSTALL_FEE": Decimal("3000"),
    "BRANCH_MONTHLY_PRICE": Decimal("0"),
    "MAX_BRANCHES": 10,              
    "TERM_DISCOUNT_PERCENT": {1: 0, 3: 30, 6: 30, 12: 30},
    "GRACE_DAYS": 5,           
    "TRIAL_DAYS": 0,         
    "RENEWAL_NOTICE_DAYS": 7,       
    "CANCEL_AFTER_SUSPENDED_DAYS": 60,
    "COMPANY_NAME": "UniSale Systems",
    "COMPANY_ADDRESS": "187-60100 Embu, Kenya",
    "COMPANY_PHONE": "0790342156",
    "COMPANY_EMAIL": "unisalesystems@gmail.com",
    "RECEIPT_FOOTER": "Thank you for your business.",
    "SITE_URL": "unisalesystems.store",
}


def get(name):
    return {**DEFAULTS, **getattr(settings, "BILLING", {})}[name]
