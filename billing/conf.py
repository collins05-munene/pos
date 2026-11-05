"""Pricing/policy knobs. Override any of them with settings.BILLING = {...}."""
from decimal import Decimal

from django.conf import settings

DEFAULTS = {
    "CURRENCY": "KES",
    "MONTHLY_PRICE": Decimal("1499"),
    "INSTALL_FEE": Decimal("3000 "),       
    "BRANCH_MONTHLY_PRICE": Decimal("500"),
    "MAX_BRANCHES": 10,              
    "TERM_DISCOUNT_PERCENT": {1: 0, 3: 30, 6: 30, 12: 30},
    "GRACE_DAYS": 5,           
    "TRIAL_DAYS": 0,         
    "RENEWAL_NOTICE_DAYS": 7,       
    "CANCEL_AFTER_SUSPENDED_DAYS": 60,
    "STK_PUSH_ENABLED": False,
    "STK_PUSH_MESSAGE": ("STK push payment is still in development. We'll make it available as soon as it's ready. For now, pay to the number shown below and enter your M-Pesa code to activate your subscription."),
    "MANUAL_PAYMENTS_ENABLED": True,
    "MANUAL_PAY_LABEL": "Till number",    
    "MANUAL_PAY_NUMBER": "1764388",              
    "MANUAL_PAY_NAME": "MUNENE COLLINS GITONGA",               
    "MANUAL_MAX_ATTEMPTS_PER_HOUR": 8,
    "COMPANY_NAME": "UniSale Systems",
    "COMPANY_ADDRESS": "187-60100 Embu, Kenya",
    "COMPANY_PHONE": "0790342156",
    "COMPANY_EMAIL": "unisalesystems@gmail.com",
    "RECEIPT_FOOTER": "Thank you for your business.",
    "SITE_URL": "unisalesystems.store",
}


def get(name):
    return {**DEFAULTS, **getattr(settings, "BILLING", {})}[name]
