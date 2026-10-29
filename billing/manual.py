"""
Manual M-Pesa payments, for as long as Daraja STK push isn't available.

Flow
----
1. The tenant pays your Till/Paybill and receives an SMS with a 10-character code.
2. You paste that SMS into the platform admin  -> `register_mpesa_code()`.
3. The tenant types the code on /billing/      -> `submit_manual_payment()`.
   * code is listed and unused -> its amount becomes the shop's credit. If the credit covers the
                                  invoice, it is paid and any surplus stays as credit for the next
                                  invoice; if not, the request is PARTIAL and the shop pays the rest
                                  with another code.
   * code isn't listed yet                         -> request waits as PENDING and is approved
                                                      automatically the moment you add the code
   * code is used / voided                         -> rejected, nothing is consumed

Everything funnels through `services.mark_invoice_paid`, so a manual payment extends the
subscription exactly like an STK payment does.
"""
import json
import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import conf
from .models import BillingCredit, Invoice, ManualPaymentRequest, MpesaCode, MpesaPayment, Subscription
from .services import BillingError, credit_balance, mark_invoice_paid, record_event

logger = logging.getLogger(__name__)

NAIROBI = ZoneInfo("Africa/Nairobi")
CODE_RE = re.compile(r"[A-Z0-9]{8,12}")


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def read_payload(request):
    """Accept JSON bodies and normal form posts alike. Raises ValueError on bad JSON."""
    if (request.content_type or "").startswith("application/json"):
        data = json.loads(request.body or b"{}")
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data
    return request.POST


def normalize_code(raw):
    code = re.sub(r"\s+", "", raw or "").upper()
    if not CODE_RE.fullmatch(code):
        raise BillingError("Enter the code exactly as it appears in your M-Pesa "
                           "confirmation message, e.g. SJK3L9X2QP.")
    return code


def kes(value):
    """1499 -> '1,499'; 1499.5 -> '1,499.50' (M-Pesa amounts can carry cents)."""
    value = Decimal(value)
    return f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"


def parse_amount(raw):
    try:
        amount = Decimal(str(raw).replace(",", "").replace("KES", "").replace("Ksh", "").strip())
    except (InvalidOperation, AttributeError):
        raise BillingError("Enter a valid amount.")
    if amount <= 0:
        raise BillingError("Amount must be more than zero.")
    return amount.quantize(Decimal("0.01"))


# --------------------------------------------------------------------------
# SMS parsing
# --------------------------------------------------------------------------
_SMS_CODE = re.compile(r"\b([A-Z0-9]{10})\s+Confirmed", re.I)
_SMS_AMOUNT = re.compile(r"Ksh\.?\s?([\d,]+(?:\.\d{1,2})?)\s+received", re.I)
_SMS_FROM = re.compile(r"received\s+from\s+(.+?)\s+on\s+\d{1,2}/\d{1,2}/\d{2,4}", re.I | re.S)
_SMS_WHEN = re.compile(r"on\s+(\d{1,2})/(\d{1,2})/(\d{2,4})\s+at\s+(\d{1,2}):(\d{2})\s*([AP]M)", re.I)
_PHONE = re.compile(r"(?:\+?254|0)[17][\d*]{8}")


def parse_mpesa_sms(text):
    """
    Pull code, amount, sender and time out of a "received" confirmation SMS, e.g.

      SJK3L9X2QP Confirmed. Ksh1,499.00 received from JOHN DOE 0712345678 on 24/10/26 at 2:15 PM. ...

    Anything that can't be read comes back as None so the admin can fill it in by hand.
    Raises BillingError when no code is found at all.
    """
    text = (text or "").strip()
    m = _SMS_CODE.search(text)
    if not m:
        raise BillingError("Couldn't find an M-Pesa code in that message. "
                           "Paste the full SMS, or type the code and amount in yourself.")
    out = {"code": m.group(1).upper(), "amount": None, "phone": "", "name": "", "transaction_at": None}

    if (a := _SMS_AMOUNT.search(text)):
        out["amount"] = Decimal(a.group(1).replace(",", "")).quantize(Decimal("0.01"))

    if (f := _SMS_FROM.search(text)):
        sender = " ".join(f.group(1).split())
        if (p := _PHONE.search(sender)):
            out["phone"] = p.group(0)
            sender = sender.replace(p.group(0), "")
        out["name"] = " ".join(sender.split())[:120]

    if (w := _SMS_WHEN.search(text)):
        day, month, year, hour, minute, ampm = w.groups()
        year = int(year) + (2000 if len(year) == 2 else 0)
        hour = int(hour) % 12 + (12 if ampm.upper() == "PM" else 0)
        try:
            out["transaction_at"] = datetime(year, int(month), int(day), hour, int(minute), tzinfo=NAIROBI)
        except ValueError:
            pass
    return out


# --------------------------------------------------------------------------
# Core settlement (caller holds the row locks)
# --------------------------------------------------------------------------
def _resolve(req, status, reason="", mcode=None):
    req.status = status
    req.reason = reason[:255]
    req.resolved_at = timezone.now()
    if mcode is not None:
        req.matched_code = mcode
    req.save()
    return req


def _settle(req, mcode, *, by=None):
    """Decide one request against one listed code. Never raises for business-rule failures."""
    now = timezone.now()
    tenant = req.tenant
    who = getattr(by, "username", None)
    # Lock order is always Subscription (per-shop credit lock) -> Invoice, same as
    # services.apply_credit, so two requests can't deadlock each other.
    Subscription.objects.select_for_update().get(tenant_id=tenant.pk)
    invoice = Invoice.objects.select_for_update().get(pk=req.invoice_id)

    if invoice.status == Invoice.Status.VOID:
        # The shop switched term after submitting this code, so the invoice it was entered
        # against was replaced. Settle against the shop's current invoice instead, otherwise
        # we'd quote an amount and an invoice number the shop can no longer see.
        current = Invoice.objects.select_for_update().filter(
            tenant=tenant, status=Invoice.Status.OPEN).first()
        if current is not None:
            invoice = current
            req.invoice = current

    if mcode.is_void:
        return _resolve(req, ManualPaymentRequest.Status.REJECTED,
                        "That code isn't valid. Contact support if you're sure you paid.")

    if mcode.used_at:
        record_event("manual_payment.code_reuse_attempt", tenant, code=mcode.code,
                     invoice=invoice.number, first_used_by=getattr(mcode.used_by_tenant, "slug", None))
        if mcode.used_by_tenant_id == tenant.pk:
            # Their own earlier entry (e.g. a part payment): say where the money went, and
            # don't tell them anything about other shops' payments.
            return _resolve(req, ManualPaymentRequest.Status.REJECTED,
                            f"That code has already been used: its KES {kes(mcode.amount)} "
                            "was added to your credit when you first entered it.")
        return _resolve(req, ManualPaymentRequest.Status.REJECTED,
                        "That code has already been used for a payment.")

    if invoice.status == Invoice.Status.PAID:
        return _resolve(req, ManualPaymentRequest.Status.REJECTED, "This invoice is already paid.")

    # The code is genuine and unused: bank it as credit, then see if the credit covers the invoice.
    mcode.used_at = now
    mcode.used_by_tenant = tenant
    mcode.used_invoice = invoice
    mcode.save(update_fields=["used_at", "used_by_tenant", "used_invoice"])
    BillingCredit.objects.create(tenant=tenant, amount=mcode.amount, kind=BillingCredit.Kind.TOPUP,
                                 mpesa_code=mcode, invoice=invoice)

    balance = credit_balance(tenant)
    if balance < invoice.total:
        due = invoice.total - balance
        record_event("manual_payment.partial", tenant, code=mcode.code, invoice=invoice.number,
                     received=mcode.amount, credit=balance, still_due=due, request=req.pk)
        return _resolve(req, ManualPaymentRequest.Status.PARTIAL,
                        f"We received KES {kes(mcode.amount)}. KES {kes(due)} is still due on "
                        f"{invoice.number}: pay that amount, then enter the new M-Pesa code here.", mcode)

    BillingCredit.objects.create(tenant=tenant, amount=-invoice.total, kind=BillingCredit.Kind.APPLIED,
                                 mpesa_code=mcode, invoice=invoice)
    mark_invoice_paid(invoice, paid_at=now, receipt=mcode.code)
    leftover = balance - invoice.total
    if leftover > 0:
        record_event("manual_payment.credit_carried", tenant, code=mcode.code, invoice=invoice.number,
                     credit=leftover)
    record_event("manual_payment.approved", tenant, code=mcode.code, invoice=invoice.number,
                 amount=mcode.amount, request=req.pk, approved_by=who or "automatic")
    note = (f"KES {kes(leftover)} extra is kept as credit and will be deducted from your next invoice."
            if leftover > 0 else "")
    return _resolve(req, ManualPaymentRequest.Status.APPROVED, note, mcode)


# --------------------------------------------------------------------------
# Tenant side
# --------------------------------------------------------------------------
def _check_throttle(tenant):
    """Cool-off for shops that keep sending codes we can't match (guessing)."""
    limit = conf.get("MANUAL_MAX_ATTEMPTS_PER_HOUR")
    recent = ManualPaymentRequest.objects.filter(
        tenant=tenant, created_at__gte=timezone.now() - timedelta(hours=1),
        status__in=[ManualPaymentRequest.Status.PENDING, ManualPaymentRequest.Status.REJECTED],
    ).count()
    if recent >= limit:
        raise BillingError("Too many unconfirmed attempts. Please wait an hour, "
                           "or contact support with your M-Pesa message.")


def submit_manual_payment(tenant, *, user, invoice, raw_code):
    """-> ManualPaymentRequest whose .status is APPROVED, PENDING or REJECTED."""
    if not conf.get("MANUAL_PAYMENTS_ENABLED"):
        raise BillingError("Manual payment confirmation isn't available right now.")
    code = normalize_code(raw_code)

    with transaction.atomic():
        Subscription.objects.select_for_update().get(tenant_id=tenant.pk)   # credit lock first
        invoice = Invoice.objects.select_for_update().get(pk=invoice.pk)
        if invoice.status != Invoice.Status.OPEN:
            raise BillingError("This invoice is not payable.")

        req = ManualPaymentRequest.objects.filter(
            tenant=tenant, invoice=invoice, code=code,
            status=ManualPaymentRequest.Status.PENDING).first()
        if req is None:
            _check_throttle(tenant)
            req = ManualPaymentRequest.objects.create(
                tenant=tenant, invoice=invoice, code=code,
                submitted_by=user if getattr(user, "pk", None) else None)
            record_event("manual_payment.submitted", tenant, code=code, invoice=invoice.number)

        mcode = MpesaCode.objects.select_for_update().filter(code=code).first()
        if mcode is None:
            return req            # PENDING: approved automatically when you list the code
        return _settle(req, mcode)


# --------------------------------------------------------------------------
# Platform-admin side
# --------------------------------------------------------------------------
def register_mpesa_code(*, by, code, amount, phone="", name="", transaction_at=None, raw_message=""):
    """
    List a received payment. Returns (MpesaCode, [approved ManualPaymentRequest]).
    Waiting requests for the code are settled straight away, unless the code was claimed by
    more than one shop (then nothing is automatic; resolve them by hand).
    """
    code = normalize_code(code)
    amount = parse_amount(amount)
    if MpesaPayment.objects.filter(mpesa_receipt=code).exists():
        raise BillingError("That code was already recorded through an STK push payment.")
    try:
        with transaction.atomic():
            mcode = MpesaCode.objects.create(
                code=code, amount=amount, sender_phone=(phone or "")[:20],
                sender_name=(name or "")[:120], transaction_at=transaction_at,
                raw_message=(raw_message or "")[:2000], added_by=by)
    except IntegrityError:
        raise BillingError("That code is already in your list.")
    record_event("manual_payment.code_added", None, by=by.username, code=code, amount=amount)
    return mcode, reconcile_pending(mcode)


def reconcile_pending(mcode):
    with transaction.atomic():
        mcode = MpesaCode.objects.select_for_update().get(pk=mcode.pk)
        waiting = list(ManualPaymentRequest.objects.select_for_update()
                       .select_related("tenant", "invoice")
                       .filter(code=mcode.code, status=ManualPaymentRequest.Status.PENDING)
                       .order_by("created_at"))
        if len({r.tenant_id for r in waiting}) > 1:
            record_event("manual_payment.contested", None, code=mcode.code,
                         tenants=[r.tenant.slug for r in waiting])
            return []
        approved = []
        for r in waiting:
            if _settle(r, mcode).status == ManualPaymentRequest.Status.APPROVED:
                approved.append(r)
        return approved


def approve_request(req, *, by):
    """Admin override for a PENDING request whose code is listed (e.g. a contested code)."""
    with transaction.atomic():
        req = (ManualPaymentRequest.objects.select_for_update()
               .select_related("tenant").get(pk=req.pk))
        if req.status != ManualPaymentRequest.Status.PENDING:
            raise BillingError("That request is already resolved.")
        mcode = MpesaCode.objects.select_for_update().filter(code=req.code).first()
        if mcode is None:
            raise BillingError("List this M-Pesa code first, then approve.")
        return _settle(req, mcode, by=by)


def reject_request(req, *, by, reason=""):
    with transaction.atomic():
        req = ManualPaymentRequest.objects.select_for_update().select_related("tenant").get(pk=req.pk)
        if req.status != ManualPaymentRequest.Status.PENDING:
            raise BillingError("That request is already resolved.")
        _resolve(req, ManualPaymentRequest.Status.REJECTED,
                 reason or "We couldn't confirm this payment. Contact support.")
        record_event("manual_payment.rejected", req.tenant, by=by.username, code=req.code,
                     reason=req.reason)
    return req


def void_code(mcode, *, by):
    with transaction.atomic():
        mcode = MpesaCode.objects.select_for_update().get(pk=mcode.pk)
        if mcode.used_at:
            raise BillingError("This code has already paid an invoice and can't be voided.")
        mcode.is_void = True
        mcode.save(update_fields=["is_void"])
        record_event("manual_payment.code_voided", None, by=by.username, code=mcode.code)
    return mcode
