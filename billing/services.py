import logging
from calendar import monthrange
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from operator import sub

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import conf, mpesa
from .models import BillingEvent, Invoice, MpesaPayment, Subscription
from .pricing import quote
from .signals import billing_event

logger = logging.getLogger(__name__)


class BillingError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def add_months(dt, months):
    idx = dt.month - 1 + months
    year, month = dt.year + idx // 12, idx % 12 + 1
    return dt.replace(year=year, month=month, day=min(dt.day, monthrange(year, month)[1]))


def record_event(event_type, tenant=None, **payload):
    event = BillingEvent.objects.create(tenant=tenant, type=event_type, payload=payload)
    billing_event.send_robust(sender=BillingEvent, event=event)
    return event


# ---------------------------------------------------------- subscription
@transaction.atomic
def start_subscription(tenant, term_months=1):
    """-> event `subscription.created`; returns (subscription, first_invoice)."""
    now = timezone.now()
    trial_days = conf.get("TRIAL_DAYS")
    sub = Subscription.objects.create(
        tenant=tenant,
        term_months=term_months,
        status=Subscription.Status.TRIALING if trial_days else Subscription.Status.PENDING,
        trial_ends_at=now + timedelta(days=trial_days) if trial_days else None,
    )
    record_event("subscription.created", tenant, term_months=term_months, status=sub.status)
    invoice = issue_invoice(tenant, term_months, include_install_fee=True)
    return sub, invoice


@transaction.atomic
def issue_invoice(tenant, term_months, *, include_install_fee=None):
    """
    Open an invoice for `term_months`. The install fee is added automatically
    until an invoice containing it has been paid. Re-uses an identical open
    invoice; voids stale ones (e.g. customer switched from 1 to 12 months).
    """
    sub = tenant.subscription
    if include_install_fee is None:
        include_install_fee = not Invoice.objects.filter(
            tenant=tenant, status=Invoice.Status.PAID, install_fee_amount__gt=0
        ).exists()

    q = quote(term_months, include_install_fee=include_install_fee,
              monthly_price=sub.custom_monthly_price)

    open_invoices = list(Invoice.objects.select_for_update()
                         .filter(tenant=tenant, status=Invoice.Status.OPEN))
    for inv in open_invoices:
        if inv.term_months == term_months and inv.total == q["total"]:
            return inv
    for inv in open_invoices:
        inv.status = Invoice.Status.VOID
        inv.save(update_fields=["status"])

    start = sub.current_period_end or timezone.now()
    end = add_months(start, term_months)

    invoice = Invoice.objects.create(
        tenant=tenant, term_months=term_months,
        list_price_amount=q["list_price"], discount_amount=q["discount_amount"],
        subscription_amount=q["subscription_amount"],
        install_fee_amount=q["install_fee"], total=q["total"],
        period_start=start, period_end=end,  
    )
    record_event("invoice.created", tenant, invoice=invoice.number, total=invoice.total,
                 term_months=term_months)
    return invoice


def initiate_stk_payment(invoice, phone):
    if invoice.status != Invoice.Status.OPEN:
        raise BillingError("This invoice is not payable.")
    msisdn = mpesa.normalize_phone(phone)  # ValueError -> 400 in the view

    callback_url = settings.BILLING_CALLBACK_BASE_URL.rstrip("/") + reverse(
        "billing:mpesa-callback", args=[settings.MPESA_BILLING_CALLBACK_SECRET]
    )
    resp = mpesa.stk_push(
        phone=msisdn, amount=invoice.total, account_reference=invoice.number,
        description="Subscription", callback_url=callback_url,
    )
    return MpesaPayment.objects.create(
        invoice=invoice, tenant=invoice.tenant, phone=msisdn, amount=invoice.total,
        merchant_request_id=resp.get("MerchantRequestID", ""),
        checkout_request_id=resp["CheckoutRequestID"],
    )


def handle_stk_callback(payload):
    """
    Idempotent handler for Daraja's STK result. Safaricom retries and callbacks
    can arrive twice, so a payment is only ever processed once.
    """
    try:
        data = mpesa.parse_stk_callback(payload)
    except (KeyError, TypeError, ValueError):
        logger.warning("Malformed M-Pesa callback ignored")
        return None

    with transaction.atomic():
        payment = (MpesaPayment.objects.select_for_update(of=("self",))
                   .select_related("invoice", "tenant")
                   .filter(checkout_request_id=data["checkout_request_id"]).first())
        if payment is None:
            logger.warning("Callback for unknown CheckoutRequestID %s", data["checkout_request_id"])
            return None
        if payment.status != MpesaPayment.Status.INITIATED:
            return payment  # duplicate delivery

        now = timezone.now()
        payment.result_code = data["result_code"]
        payment.result_desc = data["result_desc"][:255]
        payment.raw_callback = payload
        payment.completed_at = now

        if data["result_code"] == 0:
            paid = Decimal(str(data["items"].get("Amount", 0)))
            payment.mpesa_receipt = data["items"].get("MpesaReceiptNumber")
            payment.status = MpesaPayment.Status.SUCCESS
            payment.save()
            if paid < payment.invoice.total:
                # Never activate on a short payment; a human resolves it.
                record_event("invoice.payment_underpaid", payment.tenant,
                             invoice=payment.invoice.number, expected=payment.invoice.total,
                             received=paid, receipt=payment.mpesa_receipt)
            else:
                mark_invoice_paid(payment.invoice, paid_at=now, receipt=payment.mpesa_receipt)
        else:
            # 1032 = customer cancelled the prompt
            payment.status = (MpesaPayment.Status.CANCELLED if data["result_code"] == 1032
                              else MpesaPayment.Status.FAILED)
            payment.save()
            record_event("invoice.payment_failed", payment.tenant, invoice=payment.invoice.number,
                         code=data["result_code"], reason=data["result_desc"])
    return payment


def mark_invoice_paid(invoice, *, paid_at=None, receipt=None):
    """
    Extend the subscription. Also accepts VOID invoices: if the customer paid a
    prompt we'd since replaced, the money is real, so honour it.
    -> events `invoice.payment_succeeded`, plus `subscription.activated` /
       `subscription.reactivated` when relevant.
    """
    paid_at = paid_at or timezone.now()
    with transaction.atomic():
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.status == Invoice.Status.PAID:
            return invoice
        sub = Subscription.objects.select_for_update().get(tenant_id=invoice.tenant_id)
        previous_status = sub.status

        still_running = bool(sub.current_period_end and sub.current_period_end > paid_at
                             and previous_status in (Subscription.Status.ACTIVE,
                                                     Subscription.Status.PAST_DUE))
        start = sub.current_period_end if still_running else paid_at   # early renewals stack
        end = add_months(start, invoice.term_months)

        invoice.status = Invoice.Status.PAID
        invoice.paid_at = paid_at
        invoice.period_start, invoice.period_end = start, end
        invoice.save()

        sub.status = Subscription.Status.ACTIVE
        sub.term_months = invoice.term_months
        if not still_running:
            sub.current_period_start = start
        sub.current_period_end = end
        sub.suspended_at = None
        sub.cancel_at_period_end = False
        sub.monthly_equivalent = (invoice.subscription_amount / invoice.term_months).quantize(Decimal("0.01"))
        sub.save()

        tenant = invoice.tenant
        record_event("invoice.payment_succeeded", tenant, invoice=invoice.number,
                     total=invoice.total, receipt=receipt, period_end=end)
        if previous_status in (Subscription.Status.PENDING, Subscription.Status.TRIALING):
            record_event("subscription.activated", tenant, period_end=end)
        elif previous_status in (Subscription.Status.SUSPENDED, Subscription.Status.CANCELLED):
            record_event("subscription.reactivated", tenant, period_end=end)
    return invoice


# -------------------------------------------------- lock-out / overrides
def cancel_subscription(tenant, *, immediate=False, by=None, note=""):
    sub = tenant.subscription
    if immediate:
        sub.status = Subscription.Status.CANCELLED
        sub.save(update_fields=["status", "updated_at"])
        record_event("subscription.deleted", tenant, by=getattr(by, "username", None), note=note)
    else:
        sub.cancel_at_period_end = True
        sub.save(update_fields=["cancel_at_period_end", "updated_at"])
        record_event("subscription.cancel_scheduled", tenant, by=getattr(by, "username", None))
    return sub


def suspend_now(tenant, *, by, note=""):
    sub = tenant.subscription
    sub.status = Subscription.Status.SUSPENDED
    sub.suspended_at = timezone.now()
    sub.comp_until = None
    sub.save()
    record_event("subscription.suspended", tenant, by=by.username, note=note, manual=True)
    return sub


def grant_complimentary_access(tenant, *, days, by, note=""):
    if days <= 0:
        raise ValueError("days must be positive")
    sub = tenant.subscription
    base = max(sub.comp_until or timezone.now(), timezone.now())
    sub.comp_until = base + timedelta(days=days)
    sub.override_note = note[:255]
    sub.save()
    record_event("override.comp_granted", tenant, by=by.username, days=days, note=note,
                 comp_until=sub.comp_until)
    return sub


def set_custom_price(tenant, *, monthly_price, by, note=""):
    """monthly_price=None clears the override. Applies to invoices issued from now on."""
    sub = tenant.subscription
    sub.custom_monthly_price = Decimal(monthly_price) if monthly_price not in (None, "") else None
    sub.override_note = note[:255]
    sub.save()
    record_event("override.price_set", tenant, by=by.username, price=sub.custom_monthly_price, note=note)
    return sub


# --------------------------------------------------------- daily cron job
def run_billing_cycle(now=None):
    """Idempotent; run daily. M-Pesa can't auto-debit, so 'renewal' = open an invoice + notify."""
    now = now or timezone.now()
    S = Subscription.Status
    stats = defaultdict(int)
    grace = timedelta(days=conf.get("GRACE_DAYS"))
    notice = timedelta(days=conf.get("RENEWAL_NOTICE_DAYS"))
    cancel_after = timedelta(days=conf.get("CANCEL_AFTER_SUSPENDED_DAYS"))

    def suspend(sub, reason):
        sub.status, sub.suspended_at = S.SUSPENDED, now
        sub.save()
        record_event("subscription.suspended", sub.tenant, reason=reason)
        stats["suspended"] += 1

    def cancel(sub):
        sub.status = S.CANCELLED
        sub.save()
        record_event("subscription.deleted", sub.tenant)
        stats["cancelled"] += 1

    base = Subscription.objects.select_related("tenant")

    for sub in list(base.filter(status=S.ACTIVE, current_period_end__lte=now)):
        if sub.cancel_at_period_end:
            cancel(sub)
        else:
            sub.status = S.PAST_DUE
            sub.save()
            record_event("subscription.past_due", sub.tenant, period_end=sub.current_period_end)
            stats["past_due"] += 1

    for sub in list(base.filter(status=S.PAST_DUE, current_period_end__lte=now - grace)):
        suspend(sub, "grace period elapsed")

    for sub in list(base.filter(status=S.TRIALING, trial_ends_at__lte=now)):
        suspend(sub, "trial ended unpaid")

    for sub in list(base.filter(status=S.SUSPENDED, suspended_at__lte=now - cancel_after)):
        cancel(sub)

    for sub in list(base.filter(status__in=[S.ACTIVE, S.PAST_DUE], cancel_at_period_end=False,
                                current_period_end__lte=now + notice)):
        if not Invoice.objects.filter(tenant=sub.tenant, status=Invoice.Status.OPEN).exists():
            issue_invoice(sub.tenant, sub.term_months, include_install_fee=False)
            stats["renewal_invoices"] += 1

    return dict(stats)
