from django.dispatch import Signal

# Sent for every BillingEvent. Hook email/SMS/Slack here:
#   @receiver(billing_event)
#   def notify(sender, event, **kw): ...
billing_event = Signal()
