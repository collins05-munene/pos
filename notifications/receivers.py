from django.dispatch import receiver

from billing.signals import billing_event
from .models import Notification
from .utils import notify, ADMINS

L = Notification.Level

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