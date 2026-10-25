from decimal import Decimal, InvalidOperation

from django.dispatch import receiver

from billing.signals import billing_event
from .models import Notification
from .utils import notify, ADMINS

L = Notification.Level


def _kes(value):
    """'7.00' / Decimal('1499.5') -> '7' / '1,499.50' (event payloads store decimals as strings)."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    return f"{d:,.0f}" if d == d.to_integral_value() else f"{d:,.2f}"


RULES = {
    "invoice.payment_succeeded": (L.SUCCESS, "Payment received", lambda p:
        f"KSH {p.get('total')} received for invoice {p.get('invoice')}. "
        f"Subscription active until {str(p.get('period_end', ''))[:10]}."),
    "invoice.payment_failed": (L.WARNING, "Payment not completed", lambda p:
        f"The M-Pesa payment for invoice {p.get('invoice')} did not go through "
        f"({p.get('reason')}). You can try again from Billing."),
    "invoice.payment_underpaid": (L.WARNING, "Partial payment received", lambda p:
        f"We received KSH {p.get('received')} but invoice {p.get('invoice')} is "
        f"KSH {p.get('expected')}. The invoice is still open; contact support to settle the difference."),
    "manual_payment.partial": (L.WARNING, "Part payment received", lambda p:
        f"We received KES {_kes(p.get('received'))}. KES {_kes(p.get('still_due'))} is still due on "
        f"{p.get('invoice')}: pay that amount, then enter the new M-Pesa code on the Billing page."),
    "manual_payment.credit_carried": (L.SUCCESS, "Credit added to your account", lambda p:
        f"KES {_kes(p.get('credit'))} extra is kept as credit and will be deducted from your next invoice."),
    "invoice.paid_from_credit": (L.SUCCESS, "Invoice paid from your credit", lambda p:
        f"{p.get('invoice')} (KES {_kes(p.get('total'))}) was paid automatically from your credit."),
    "subscription.past_due": (L.WARNING, "Subscription payment overdue", lambda p:
        "Your subscription period has ended. Pay from Billing to avoid being locked out."),
    "subscription.suspended": (L.ERROR, "Subscription suspended", lambda p:
        "Access for your team is locked until payment is made. Open Billing to pay."),
    "subscription.deleted": (L.ERROR, "Subscription cancelled", lambda p:
        "Your subscription has been cancelled."),
    "subscription.cancel_scheduled": (L.INFO, "Cancellation scheduled", lambda p:
        "Your subscription will end when the current period runs out."),
}


@receiver(billing_event)
def on_billing_event(sender, event, **kwargs):
    rule = RULES.get(event.type)
    if not rule or event.tenant is None:
        return
    level, title, build = rule
    notify(event.tenant, title, build(event.payload or {}), level=level, roles=ADMINS)