import hashlib
import hmac
import json
import unittest
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from billing import paystack, receipts, services
from billing.models import (BillingCredit, BillingEvent, Invoice, PaystackPayment, Receipt,
                            Subscription)
from tenants.models import Tenant

try:
    import reportlab  # noqa: F401
    HAVE_REPORTLAB = True
except ImportError:                                              # pragma: no cover
    HAVE_REPORTLAB = False

User = get_user_model()

KEY = "sk_test_secret"
# KES 1 a month + KES 2 install fee = a first invoice of KES 3: small numbers keep the arithmetic readable.
PRICES = {"MONTHLY_PRICE": Decimal("1"), "INSTALL_FEE": Decimal("2")}
SETTINGS = dict(BILLING=PRICES, PAYSTACK_SECRET_KEY=KEY)
CALLBACK = "https://testserver/billing/paystack/callback/"


def make_shop(name="Shop A", email="a@example.com", term=1):
    tenant = Tenant.objects.create(name=name, slug=name.lower().replace(" ", "-"),
                                   phone="0712000000", email=email)
    owner = User.objects.create_user(username=email, email=email, password="pw", tenant=tenant,
                                     is_owner=True, role="ADMIN")
    _sub, invoice = services.start_subscription(tenant, term)
    return tenant, owner, invoice


def charge(reference, amount, status="success", currency="KES"):
    """What Paystack returns from /transaction/verify (and sends as webhook `data`)."""
    return {"id": 4099260516, "reference": reference, "status": status, "currency": currency,
            "amount": int(Decimal(str(amount)) * 100), "channel": "mobile_money",
            "gateway_response": "Successful" if status == "success" else "Declined"}


def start_payment(tenant, invoice, email="a@example.com"):
    """Open a (mocked) Paystack checkout for what the shop owes on `invoice`."""
    def fake_init(**kw):
        return {"authorization_url": f"https://checkout.paystack.com/{kw['reference']}",
                "access_code": "acc_1", "reference": kw["reference"]}
    with mock.patch("billing.paystack.initialize_transaction", side_effect=fake_init):
        return services.initiate_paystack_payment(invoice, email=email, callback_url=CALLBACK)


def pay(tenant, invoice, amount=None, status="success"):
    """Checkout + Paystack reporting `status` for `amount` (default: exactly what was asked)."""
    payment = start_payment(tenant, invoice)
    services.settle_paystack_transaction(charge(payment.reference, amount or payment.amount, status))
    payment.refresh_from_db()
    return payment


def sign(body, key=KEY):
    return hmac.new(key.encode(), body, hashlib.sha512).hexdigest()


# --------------------------------------------------------------------------------------------
class PaystackClientTests(TestCase):
    def test_amounts_are_converted_to_and_from_cents(self):
        self.assertEqual(paystack.to_subunit(Decimal("1499")), 149900)
        self.assertEqual(paystack.to_subunit(Decimal("1499.50")), 149950)
        self.assertEqual(paystack.to_subunit("0.01"), 1)
        self.assertEqual(paystack.from_subunit(149950), Decimal("1499.50"))

    @override_settings(PAYSTACK_SECRET_KEY=KEY)
    def test_signature_check(self):
        body = b'{"event":"charge.success"}'
        self.assertTrue(paystack.valid_signature(body, sign(body)))
        self.assertTrue(paystack.valid_signature(body, sign(body).upper()))
        self.assertFalse(paystack.valid_signature(body, sign(body, key="other")))
        self.assertFalse(paystack.valid_signature(body + b" ", sign(body)))     # body was altered
        self.assertFalse(paystack.valid_signature(body, ""))
        self.assertFalse(paystack.valid_signature(body, None))

    @override_settings(PAYSTACK_SECRET_KEY="")
    def test_nothing_is_valid_when_no_key_is_configured(self):
        body = b"{}"
        self.assertFalse(paystack.valid_signature(body, sign(body, key="")))
        self.assertFalse(paystack.is_configured())
        with self.assertRaises(paystack.PaystackError):
            paystack.verify_transaction("ref")

    @override_settings(PAYSTACK_SECRET_KEY=KEY)
    def test_initialize_sends_the_right_request(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"status": True, "data": {"authorization_url": "https://x", "reference": "R1"}}
        with mock.patch("billing.paystack.requests.request", return_value=resp) as call:
            out = paystack.initialize_transaction(email="a@b.co", amount=Decimal("1499"), reference="R1",
                                                  callback_url=CALLBACK, currency="KES", metadata={"invoice": "INV-1"})
        self.assertEqual(out["authorization_url"], "https://x")
        (method, url), kw = call.call_args
        self.assertEqual((method, url), ("POST", "https://api.paystack.co/transaction/initialize"))
        self.assertEqual(kw["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual(kw["json"]["amount"], 149900)
        self.assertEqual(kw["json"]["currency"], "KES")
        self.assertEqual(kw["json"]["callback_url"], CALLBACK)

    @override_settings(PAYSTACK_SECRET_KEY=KEY)
    def test_errors_become_friendly_messages(self):
        bad_key = mock.Mock(status_code=401)
        bad_key.json.return_value = {"status": False, "message": "Invalid key"}
        with mock.patch("billing.paystack.requests.request", return_value=bad_key):
            with self.assertRaises(paystack.PaystackError) as cm:
                paystack.verify_transaction("R1")
        self.assertNotIn("Invalid key", str(cm.exception))          # config problems aren't shown to shops
        with mock.patch("billing.paystack.requests.request", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(paystack.PaystackError):
                paystack.verify_transaction("R1")


# --------------------------------------------------------------------------------------------
@override_settings(**SETTINGS)
class SettlementTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, self.invoice = make_shop()
        self.assertEqual(self.invoice.total, Decimal("3"))

    def fresh(self):
        return Tenant.objects.get(pk=self.tenant.pk)

    def test_successful_payment_activates_the_subscription(self):
        payment = pay(self.tenant, self.invoice)
        self.assertEqual(payment.status, PaystackPayment.Status.SUCCESS)
        self.assertEqual(payment.paid_amount, Decimal("3"))
        self.assertEqual(payment.channel, "mobile_money")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        sub = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(sub.status, Subscription.Status.ACTIVE)
        self.assertTrue(self.fresh().has_access)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("0"))

    def test_repeated_webhooks_and_callbacks_credit_only_once(self):
        payment = pay(self.tenant, self.invoice)
        for _ in range(3):
            services.settle_paystack_transaction(charge(payment.reference, "3"))
        self.assertEqual(BillingCredit.objects.filter(kind="TOPUP", tenant=self.tenant).count(), 1)
        self.assertEqual(Receipt.objects.filter(invoice=self.invoice).count(), 1)
        self.assertEqual(self.tenant.billing_events.filter(type="invoice.payment_succeeded").count(), 1)

    def test_the_amount_asked_for_is_the_amount_due_after_credit(self):
        BillingCredit.objects.create(tenant=self.tenant, amount=Decimal("1"), kind="ADJUSTMENT")
        payment = start_payment(self.tenant, self.invoice)
        self.assertEqual(payment.amount, Decimal("2"))
        self.assertTrue(payment.reference.startswith(self.invoice.number))
        self.assertEqual(payment.currency, "KES")

    def test_part_payment_is_kept_as_credit_until_the_rest_arrives(self):
        first = start_payment(self.tenant, self.invoice)
        services.settle_paystack_transaction(charge(first.reference, "1"))      # Paystack collected only 1
        self.assertFalse(self.fresh().has_access)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("1"))
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.OPEN)
        self.assertEqual(services.amount_due(self.invoice), Decimal("2"))
        self.assertTrue(self.tenant.billing_events.filter(type="paystack.partial").exists())

        second = start_payment(self.tenant, self.invoice)
        self.assertEqual(second.amount, Decimal("2"))
        services.settle_paystack_transaction(charge(second.reference, "2"))
        self.assertTrue(self.fresh().has_access)
        self.assertEqual(Receipt.objects.get(invoice=self.invoice).payment_reference,
                         f"{first.reference}, {second.reference}")

    def test_overpayment_pays_the_invoice_and_keeps_the_surplus(self):
        payment = start_payment(self.tenant, self.invoice)
        services.settle_paystack_transaction(charge(payment.reference, "10"))
        self.assertTrue(self.fresh().has_access)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("7"))
        event = self.tenant.billing_events.get(type="paystack.credit_carried")
        self.assertEqual(Decimal(str(event.payload["credit"])), Decimal("7"))

    def test_surplus_credit_pays_the_next_invoice_automatically(self):
        payment = start_payment(self.tenant, self.invoice)
        services.settle_paystack_transaction(charge(payment.reference, "10"))
        nxt = services.issue_invoice(self.tenant, 1, include_install_fee=False)
        self.assertEqual(nxt.status, Invoice.Status.PAID)
        self.assertEqual(Receipt.objects.get(invoice=nxt).payment_method, Receipt.Method.CREDIT)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("6"))

    def test_declined_payment_changes_nothing_and_tells_the_shop(self):
        payment = pay(self.tenant, self.invoice, status="failed")
        self.assertEqual(payment.status, PaystackPayment.Status.FAILED)
        self.assertFalse(self.fresh().has_access)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("0"))
        self.assertTrue(self.tenant.billing_events.filter(type="invoice.payment_failed").exists())

    def test_abandoned_checkout_is_quiet_but_can_still_succeed_later(self):
        payment = pay(self.tenant, self.invoice, status="abandoned")
        self.assertEqual(payment.status, PaystackPayment.Status.CANCELLED)
        self.assertFalse(self.tenant.billing_events.filter(type="invoice.payment_failed").exists())
        services.settle_paystack_transaction(charge(payment.reference, "3"))    # customer paid after all
        self.assertTrue(self.fresh().has_access)

    def test_unfinished_payment_stays_waiting(self):
        payment = pay(self.tenant, self.invoice, status="ongoing")
        self.assertEqual(payment.status, PaystackPayment.Status.INITIATED)
        self.assertFalse(self.fresh().has_access)

    def test_unknown_reference_is_ignored(self):
        self.assertIsNone(services.settle_paystack_transaction(charge("NOT-OURS", "3")))
        self.assertFalse(self.fresh().has_access)

    def test_wrong_currency_is_never_credited(self):
        payment = start_payment(self.tenant, self.invoice)
        services.settle_paystack_transaction(charge(payment.reference, "3", currency="USD"))
        payment.refresh_from_db()
        self.assertEqual(payment.status, PaystackPayment.Status.FAILED)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("0"))
        self.assertTrue(self.tenant.billing_events.filter(type="paystack.mismatch").exists())

    def test_payment_started_on_a_replaced_invoice_still_pays_the_current_one(self):
        payment = start_payment(self.tenant, self.invoice)                      # owner is on Paystack's page...
        new = services.issue_invoice(self.tenant, 1, include_install_fee=True)  # ...meanwhile the term changes
        three = services.issue_invoice(self.tenant, 3)
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, Invoice.Status.VOID)
        services.settle_paystack_transaction(charge(payment.reference, "3"))    # money is real
        self.assertEqual(services.credit_balance(self.tenant), Decimal("3"))
        three.refresh_from_db()
        self.assertEqual(three.status, Invoice.Status.OPEN)                     # 3 months costs more than 3
        self.assertEqual(services.amount_due(three), three.total - 3)

    def test_renewal_extends_from_the_current_period_end(self):
        pay(self.tenant, self.invoice)
        sub = Subscription.objects.get(tenant=self.tenant)
        end = sub.current_period_end
        renewal = services.issue_invoice(self.tenant, 1, include_install_fee=False)
        pay(self.tenant, renewal)
        sub.refresh_from_db()
        self.assertEqual(sub.current_period_end, services.add_months(end, 1))

    def test_payment_after_lockout_reactivates(self):
        pay(self.tenant, self.invoice)
        sub = Subscription.objects.get(tenant=self.tenant)
        end = sub.current_period_end
        services.run_billing_cycle(now=end + timedelta(hours=1))        # past due
        services.run_billing_cycle(now=end + timedelta(days=6))         # grace over: locked
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.Status.SUSPENDED)
        renewal = Invoice.objects.get(tenant=self.tenant, status=Invoice.Status.OPEN)
        pay(self.tenant, renewal)
        sub.refresh_from_db()
        self.assertEqual(sub.status, Subscription.Status.ACTIVE)
        self.assertTrue(self.tenant.billing_events.filter(type="subscription.reactivated").exists())

    # ---- starting a checkout
    def test_cannot_start_checkout_for_a_paid_invoice(self):
        pay(self.tenant, self.invoice)
        self.invoice.refresh_from_db()
        with self.assertRaises(services.BillingError):
            start_payment(self.tenant, self.invoice)

    def test_checkout_needs_an_email_for_paystack(self):
        with self.assertRaises(services.BillingError):
            services.initiate_paystack_payment(self.invoice, email="", callback_url=CALLBACK)

    def test_nothing_to_pay_when_credit_covers_the_invoice(self):
        BillingCredit.objects.create(tenant=self.tenant, amount=Decimal("50"), kind="ADJUSTMENT")
        invoice = services.issue_invoice(self.tenant, 1)             # paid from credit on issue
        self.assertEqual(invoice.status, Invoice.Status.PAID)

    def test_paystack_errors_surface_as_billing_errors(self):
        with mock.patch("billing.paystack.initialize_transaction",
                        side_effect=paystack.PaystackError("We couldn't reach the payment provider.")):
            with self.assertRaises(services.BillingError):
                services.initiate_paystack_payment(self.invoice, email="a@example.com", callback_url=CALLBACK)
        self.assertEqual(PaystackPayment.objects.count(), 0)

    # ---- reconciliation
    def test_reconcile_settles_payments_whose_webhook_never_came(self):
        payment = start_payment(self.tenant, self.invoice)
        PaystackPayment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(hours=1))
        with mock.patch("billing.paystack.verify_transaction",
                        return_value=charge(payment.reference, "3")):
            self.assertEqual(services.reconcile_paystack_payments(), 1)
        self.assertTrue(self.fresh().has_access)

    def test_reconcile_leaves_fresh_payments_alone(self):
        start_payment(self.tenant, self.invoice)                     # just now: the owner may still be paying
        with mock.patch("billing.paystack.verify_transaction") as verify:
            self.assertEqual(services.reconcile_paystack_payments(), 0)
        verify.assert_not_called()


# --------------------------------------------------------------------------------------------
@override_settings(**SETTINGS)
class ViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="root", email="r@x.com", password="pw")
        self.tenant, self.owner, self.invoice = make_shop()

    def call(self, view, method, path="/x/", user=None, tenant=None, data=None, json_body=None, **kwargs):
        rf = RequestFactory()
        if json_body is not None:
            req = getattr(rf, method)(path, json.dumps(json_body), content_type="application/json")
        else:
            req = getattr(rf, method)(path, data or {})
        req.user = user or self.owner
        req.tenant = tenant or self.tenant
        return view.as_view()(req, **kwargs)

    # ---- checkout
    def test_checkout_returns_the_paystack_page_to_open(self):
        from billing.views import CheckoutView
        def fake_init(**kw):
            self.assertEqual(kw["email"], "a@example.com")
            self.assertEqual(kw["amount"], Decimal("3"))
            self.assertTrue(kw["callback_url"].endswith("/billing/paystack/callback/"))
            return {"authorization_url": "https://checkout.paystack.com/abc", "access_code": "abc",
                    "reference": kw["reference"]}
        with mock.patch("billing.paystack.initialize_transaction", side_effect=fake_init):
            r = self.call(CheckoutView, "post", data={"term_months": "1"})
        body = json.loads(r.content)
        self.assertEqual(r.status_code, 201, body)
        self.assertEqual(body["authorization_url"], "https://checkout.paystack.com/abc")
        self.assertEqual(body["invoice"], self.invoice.number)
        self.assertEqual(PaystackPayment.objects.get(reference=body["reference"]).tenant, self.tenant)

    def test_checkout_accepts_json_and_defaults_to_the_open_invoice_term(self):
        from billing.views import CheckoutView
        with mock.patch("billing.paystack.initialize_transaction",
                        side_effect=lambda **kw: {"authorization_url": "https://checkout.paystack.com/z",
                                                  "reference": kw["reference"]}):
            r = self.call(CheckoutView, "post", json_body={})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(json.loads(r.content)["invoice"], self.invoice.number)

    def test_checkout_without_a_secret_key_is_a_clear_error(self):
        from billing.views import CheckoutView
        with override_settings(PAYSTACK_SECRET_KEY=""):
            r = self.call(CheckoutView, "post", data={"term_months": "1"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(json.loads(r.content)["code"], "paystack_unconfigured")

    def test_checkout_bad_term_is_a_400(self):
        from billing.views import CheckoutView
        r = self.call(CheckoutView, "post", data={"term_months": "7"})
        self.assertEqual(r.status_code, 400)

    def test_checkout_pays_straight_from_credit_when_it_covers_the_term(self):
        from billing.views import CheckoutView
        BillingCredit.objects.create(tenant=self.tenant, amount=Decimal("50"), kind="ADJUSTMENT")
        with mock.patch("billing.paystack.initialize_transaction") as init:
            r = self.call(CheckoutView, "post", data={"term_months": "1"})
        init.assert_not_called()
        body = json.loads(r.content)
        self.assertEqual((r.status_code, body["status"]), (200, "PAID"))
        self.assertTrue(body["receipt_url"].endswith("/receipt/"))

    def test_only_the_shop_owner_can_start_a_payment(self):
        from billing.views import CheckoutView
        cashier = User.objects.create_user(username="c@example.com", email="c@example.com", password="pw",
                                           tenant=self.tenant, role="CASHIER")
        with self.assertRaises(PermissionDenied):
            self.call(CheckoutView, "post", user=cashier, data={"term_months": "1"})

    # ---- webhook
    def webhook(self, event, signature="valid", client=None):
        body = json.dumps(event).encode()
        sig = sign(body) if signature == "valid" else signature
        return (client or Client()).post("/billing/paystack/webhook/", body, content_type="application/json",
                                         HTTP_X_PAYSTACK_SIGNATURE=sig)

    def test_signed_webhook_activates_the_shop(self):
        payment = start_payment(self.tenant, self.invoice)
        r = self.webhook({"event": "charge.success", "data": charge(payment.reference, "3")})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Tenant.objects.get(pk=self.tenant.pk).has_access)

    def test_unsigned_or_badly_signed_webhooks_are_refused_and_do_nothing(self):
        payment = start_payment(self.tenant, self.invoice)
        event = {"event": "charge.success", "data": charge(payment.reference, "3")}
        for sig in ("", "deadbeef", sign(b"something else")):
            self.assertEqual(self.webhook(event, signature=sig).status_code, 401)
        self.assertFalse(Tenant.objects.get(pk=self.tenant.pk).has_access)
        PaystackPayment.objects.get(pk=payment.pk)                              # untouched
        self.assertEqual(PaystackPayment.objects.get(pk=payment.pk).status, PaystackPayment.Status.INITIATED)

    def test_webhook_ignores_other_events_and_bad_json(self):
        payment = start_payment(self.tenant, self.invoice)
        r = self.webhook({"event": "transfer.success", "data": charge(payment.reference, "3")})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Tenant.objects.get(pk=self.tenant.pk).has_access)
        body = b"not json"
        r = Client().post("/billing/paystack/webhook/", body, content_type="application/json",
                          HTTP_X_PAYSTACK_SIGNATURE=sign(body))
        self.assertEqual(r.status_code, 400)

    def test_webhook_for_an_unknown_reference_is_acknowledged(self):
        r = self.webhook({"event": "charge.success", "data": charge("SOMEONE-ELSES", "3")})
        self.assertEqual(r.status_code, 200)

    def test_webhook_works_without_a_csrf_token_or_login(self):
        client = Client(enforce_csrf_checks=True)
        payment = start_payment(self.tenant, self.invoice)
        r = self.webhook({"event": "charge.success", "data": charge(payment.reference, "3")}, client=client)
        self.assertEqual(r.status_code, 200)

    # ---- return from Paystack
    def test_callback_verifies_with_paystack_instead_of_trusting_the_url(self):
        from billing.views import PaystackCallbackView
        payment = start_payment(self.tenant, self.invoice)
        with mock.patch("billing.paystack.verify_transaction", return_value=charge(payment.reference, "3")):
            r = self.call(PaystackCallbackView, "get", path=f"/billing/paystack/callback/?reference={payment.reference}")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], f"/billing/?paystack=success&ref={payment.reference}")
        self.assertTrue(Tenant.objects.get(pk=self.tenant.pk).has_access)

    def test_callback_with_a_cancelled_checkout_reports_failure(self):
        from billing.views import PaystackCallbackView
        payment = start_payment(self.tenant, self.invoice)
        with mock.patch("billing.paystack.verify_transaction",
                        return_value=charge(payment.reference, "3", status="abandoned")):
            r = self.call(PaystackCallbackView, "get", path=f"/x/?trxref={payment.reference}")
        self.assertIn("paystack=failed", r["Location"])
        self.assertFalse(Tenant.objects.get(pk=self.tenant.pk).has_access)

    def test_callback_when_paystack_is_unreachable_says_pending(self):
        from billing.views import PaystackCallbackView
        payment = start_payment(self.tenant, self.invoice)
        with mock.patch("billing.paystack.verify_transaction", side_effect=paystack.PaystackError("down")):
            r = self.call(PaystackCallbackView, "get", path=f"/x/?reference={payment.reference}")
        self.assertIn("paystack=pending", r["Location"])

    def test_callback_ignores_other_shops_references(self):
        from billing.views import PaystackCallbackView
        other, _owner, other_invoice = make_shop("Shop B", "b@example.com")
        theirs = start_payment(other, other_invoice, email="b@example.com")
        with mock.patch("billing.paystack.verify_transaction") as verify:
            r = self.call(PaystackCallbackView, "get", path=f"/x/?reference={theirs.reference}")
        verify.assert_not_called()
        self.assertIn("paystack=unknown", r["Location"])

    def test_status_endpoint_rechecks_a_waiting_payment(self):
        from billing.views import PaymentStatusView
        payment = start_payment(self.tenant, self.invoice)
        with mock.patch("billing.paystack.verify_transaction", return_value=charge(payment.reference, "3")):
            r = self.call(PaymentStatusView, "get", reference=payment.reference)
        body = json.loads(r.content)
        self.assertEqual((body["outcome"], body["has_access"]), ("success", True))
        self.assertEqual(body["receipt_url"], f"/billing/invoices/{self.invoice.number}/receipt/")

    def test_status_endpoint_is_private_to_the_shop(self):
        from billing.views import PaymentStatusView
        other, _owner, other_invoice = make_shop("Shop B", "b@example.com")
        theirs = start_payment(other, other_invoice, email="b@example.com")
        with self.assertRaises(Http404):
            self.call(PaymentStatusView, "get", reference=theirs.reference)

    # ---- billing page
    def overview(self):
        from billing.views import BillingOverviewView
        req = RequestFactory().get("/billing/", headers={"Accept": "application/json"})
        req.user, req.tenant = self.owner, self.tenant
        return json.loads(BillingOverviewView.as_view()(req).content)

    def test_overview_json_is_paystack_only(self):
        d = self.overview()
        self.assertTrue(d["paystack_enabled"])
        self.assertFalse(d["pending_payment"])
        self.assertEqual(d["open_invoice"]["number"], self.invoice.number)
        for retired in ("stk_enabled", "stk_message", "manual_pay", "manual_requests"):
            self.assertNotIn(retired, d)
        start_payment(self.tenant, self.invoice)
        self.assertTrue(self.overview()["pending_payment"])

    def test_overview_reports_live_amount_due_and_credit_entries(self):
        payment = start_payment(self.tenant, self.invoice)
        services.settle_paystack_transaction(charge(payment.reference, "1"))
        d = self.overview()
        self.assertEqual(Decimal(d["open_invoice"]["amount_due"]), Decimal("2"))
        self.assertEqual(Decimal(d["open_invoice"]["credit_applied"]), Decimal("1"))
        self.assertEqual(d["credit_entries"][0]["paystack_payment__reference"], payment.reference)

    def test_billing_template_compiles_and_has_no_mpesa_leftovers(self):
        from django.template.loader import get_template
        html = get_template("billing/overview.html").template.source
        self.assertIn("Pay with Paystack", html)
        self.assertNotIn("manual-pay.js", html)
        self.assertNotIn("M-Pesa confirmation code", html)

    def test_notifications_for_part_payment_and_extra_credit(self):
        from notifications.models import Notification
        first = start_payment(self.tenant, self.invoice)
        services.settle_paystack_transaction(charge(first.reference, "1"))
        second = start_payment(self.tenant, self.invoice)
        services.settle_paystack_transaction(charge(second.reference, "10"))
        titles = set(Notification.objects.filter(tenant=self.tenant).values_list("title", flat=True))
        self.assertIn("Part payment received", titles)
        self.assertIn("Credit added to your account", titles)

    # ---- platform admin
    def test_platform_payments_page_and_list(self):
        c = Client(); c.force_login(self.admin)
        page = c.get("/platform/payments/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "platform-payments.js")
        self.assertNotContains(page, "Add a received payment")
        pay(self.tenant, self.invoice)
        data = c.get("/platform/payments/api/").json()
        self.assertTrue(data["configured"])
        self.assertEqual(Decimal(str(data["summary"]["all_time_total"])), Decimal("3"))
        self.assertEqual(data["payments"][0]["tenant"], "Shop A")

    def test_platform_recheck_settles_a_stuck_payment(self):
        c = Client(); c.force_login(self.admin)
        payment = start_payment(self.tenant, self.invoice)
        with mock.patch("billing.paystack.verify_transaction", return_value=charge(payment.reference, "3")):
            r = c.post(f"/platform/payments/api/{payment.pk}/recheck/")
        self.assertEqual((r.status_code, r.json()["status"]), (200, "SUCCESS"))
        self.assertTrue(Tenant.objects.get(pk=self.tenant.pk).has_access)

    def test_platform_payment_pages_need_a_superuser(self):
        pay(self.tenant, self.invoice)                              # an active shop owner, but not a superuser
        c = Client(); c.force_login(self.owner)
        self.assertEqual(c.get("/platform/payments/").status_code, 403)
        self.assertEqual(c.get("/platform/payments/api/").status_code, 403)
        self.assertEqual(c.post("/platform/payments/api/1/recheck/").status_code, 403)


# --------------------------------------------------------------------------------------------
# Subscription receipts
# --------------------------------------------------------------------------------------------
@override_settings(**SETTINGS)
class ReceiptTestBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="root", email="r@x.com", password="pw")
        self.tenant, self.owner, self.invoice = make_shop()          # 1 month = KES 3 incl. install fee

    def pay(self, amount=None):
        return pay(self.tenant, self.invoice, amount=amount)

    def get(self, view, user, path="/x/", **kwargs):
        req = RequestFactory().get(path)
        req.user, req.tenant = user, self.tenant
        return view.as_view()(req, **kwargs)


class ReceiptTests(ReceiptTestBase):

    # ---- creation
    def test_paying_creates_one_receipt_with_the_payment_details(self):
        payment = self.pay()
        r = Receipt.objects.get(invoice=self.invoice)
        self.assertTrue(r.number.startswith("RCT-"))
        self.assertEqual((r.tenant, r.amount), (self.tenant, Decimal("3")))
        self.assertEqual(r.payment_method, Receipt.Method.PAYSTACK)
        self.assertEqual(r.payment_reference, payment.reference)
        self.assertEqual(r.customer_name, "Shop A")
        self.assertIsNone(r.generated_by)

    def test_marking_paid_again_never_makes_a_second_receipt(self):
        payment = self.pay()
        self.invoice.refresh_from_db()
        services.mark_invoice_paid(self.invoice, receipt=payment.reference)
        receipts.issue_receipt(self.invoice)
        self.assertEqual(Receipt.objects.filter(invoice=self.invoice).count(), 1)

    def test_invoice_paid_from_credit_is_marked_as_credit(self):
        payment = start_payment(self.tenant, self.invoice)
        services.settle_paystack_transaction(charge(payment.reference, "20"))
        nxt = services.issue_invoice(self.tenant, 1, include_install_fee=False)
        self.assertEqual(nxt.status, Invoice.Status.PAID)
        self.assertEqual(Receipt.objects.get(invoice=nxt).payment_method, Receipt.Method.CREDIT)

    def test_shop_name_is_frozen_on_the_receipt(self):
        self.pay()
        Tenant.objects.filter(pk=self.tenant.pk).update(name="Renamed Ltd")
        self.assertEqual(Receipt.objects.get(invoice=self.invoice).customer_name, "Shop A")

    def test_unpaid_invoice_has_no_receipt(self):
        with self.assertRaises(ValueError):
            receipts.issue_receipt(self.invoice)

    def test_old_paid_invoice_gets_its_receipt_on_demand(self):
        payment = self.pay()
        Receipt.objects.all().delete()
        self.invoice.refresh_from_db()
        r = receipts.issue_receipt(self.invoice, generated_by=self.admin)
        self.assertEqual((r.payment_method, r.payment_reference, r.generated_by),
                         (Receipt.Method.PAYSTACK, payment.reference, self.admin))

    def test_receipts_for_invoices_paid_with_legacy_mpesa_still_work(self):
        from billing.models import MpesaCode
        code = MpesaCode.objects.create(code="SJK3L9X2QP", amount=Decimal("3"), sender_name="JOHN DOE",
                                        sender_phone="0712345678")
        BillingCredit.objects.create(tenant=self.tenant, amount=Decimal("3"), kind="TOPUP",
                                     mpesa_code=code, invoice=self.invoice)
        services.mark_invoice_paid(self.invoice, receipt="SJK3L9X2QP")
        r = Receipt.objects.get(invoice=self.invoice)
        self.assertEqual((r.payment_method, r.payment_reference),
                         (Receipt.Method.MPESA_MANUAL, "SJK3L9X2QP"))

    # ---- shop download
    @unittest.skipUnless(HAVE_REPORTLAB, "reportlab not installed")
    def test_owner_downloads_a_pdf(self):
        from billing.views import InvoiceReceiptView
        self.pay()
        resp = self.get(InvoiceReceiptView, self.owner, number=self.invoice.number)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertTrue(resp.content.startswith(b"%PDF"))

    def test_shop_cannot_download_unpaid_or_other_shops_receipts(self):
        from billing.views import InvoiceReceiptView
        with self.assertRaises(Http404):                                  # unpaid
            self.get(InvoiceReceiptView, self.owner, number=self.invoice.number)
        self.pay()
        other, other_owner, _inv = make_shop("Shop B", "b@example.com")
        req = RequestFactory().get("/x/")
        req.user, req.tenant = other_owner, other
        with self.assertRaises(Http404):                                  # someone else's invoice
            InvoiceReceiptView.as_view()(req, number=self.invoice.number)

    # ---- platform admin
    @unittest.skipUnless(HAVE_REPORTLAB, "reportlab not installed")
    def test_platform_admin_downloads_and_generates(self):
        from platform_admin import receipt_views as rv
        self.pay()
        r = Receipt.objects.get(invoice=self.invoice)
        resp = self.get(rv.ReceiptDownloadView, self.admin, number=r.number)
        self.assertTrue(resp.content.startswith(b"%PDF"))

        Receipt.objects.all().delete()
        resp = self.get(rv.InvoiceReceiptView, self.admin, number=self.invoice.number)
        self.assertTrue(resp.content.startswith(b"%PDF"))
        self.assertEqual(Receipt.objects.get(invoice=self.invoice).generated_by, self.admin)
        self.assertTrue(BillingEvent.objects.filter(type="receipt.generated").exists())

    def test_platform_admin_lists_missing_and_bulk_generates(self):
        from platform_admin import receipt_views as rv
        self.pay()
        Receipt.objects.all().delete()
        data = json.loads(self.get(rv.ReceiptsListView, self.admin).content)
        self.assertEqual(data["summary"]["missing"], 1)
        self.assertEqual(data["missing"][0]["invoice"], self.invoice.number)

        req = RequestFactory().post("/x/", json.dumps({"all": True}), content_type="application/json")
        req.user = self.admin
        out = json.loads(rv.ReceiptGenerateView.as_view()(req).content)
        self.assertEqual((out["generated"], out["remaining"]), (1, 0))
        self.assertEqual(Receipt.objects.count(), 1)

    def test_only_superusers_reach_platform_receipts(self):
        from platform_admin import receipt_views as rv
        self.pay()
        r = Receipt.objects.get(invoice=self.invoice)
        for view, kw in ((rv.ReceiptsListView, {}), (rv.ReceiptDownloadView, {"number": r.number})):
            with self.assertRaises(PermissionDenied):
                self.get(view, self.owner, **kw)

    @unittest.skipUnless(HAVE_REPORTLAB, "reportlab not installed")
    def test_pdf_renders_for_every_payment_kind(self):
        self.pay()
        for method in Receipt.Method.values:
            Receipt.objects.filter(invoice=self.invoice).update(payment_method=method)
            r = Receipt.objects.get(invoice=self.invoice)
            self.assertTrue(receipts.render_receipt_pdf(r).startswith(b"%PDF"))


class ReceiptVerificationTests(ReceiptTestBase):
    """Public verify page, verification codes and void/restore."""

    def verify(self, code):
        from billing.views import VerifyReceiptView
        req = RequestFactory().get("/billing/verify/x/")
        return VerifyReceiptView.as_view()(req, code=code)

    def test_every_receipt_gets_its_own_unguessable_code(self):
        self.pay()
        r = Receipt.objects.get(invoice=self.invoice)
        self.assertRegex(r.verify_code, r"^[A-HJ-NP-Z2-9]{12}$")
        self.assertRegex(r.verify_code_display, r"^\w{4}-\w{4}-\w{4}$")
        self.assertNotIn(r.number, r.verify_code)
        from billing.models import new_verify_code
        self.assertEqual(len({new_verify_code() for _ in range(200)}), 200)

    def test_verify_page_shows_the_record_but_not_private_details(self):
        payment = self.pay()
        r = Receipt.objects.get(invoice=self.invoice)
        for typed in (r.verify_code, r.verify_code_display, r.verify_code.lower()):
            resp = self.verify(typed)
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode()
            self.assertIn("Genuine receipt", html)
            self.assertIn(r.number, html)
            self.assertIn("Shop A", html)                             # two short words: nothing to mask
            self.assertNotIn("0712000000", html)                      # shop phone
            self.assertNotIn(payment.reference, html)                 # payment reference

    def test_unknown_code_is_a_404_page(self):
        resp = self.verify("ABCDEFGHJKLM")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("No receipt found", resp.content.decode())
        self.assertEqual(self.verify("short").status_code, 404)

    def test_mask_name(self):
        self.assertEqual(receipts.mask_name("Mama Njeri Mini Mart"), "Mama N**** M*** M***")
        self.assertEqual(receipts.mask_name("Duka"), "Duk*")
        self.assertEqual(receipts.mask_name(""), "")

    def test_voiding_changes_the_verify_page_and_can_be_undone(self):
        from platform_admin.receipt_views import ReceiptVoidView
        self.pay()
        r = Receipt.objects.get(invoice=self.invoice)

        def post(action, payload):
            req = RequestFactory().post("/x/", json.dumps(payload), content_type="application/json")
            req.user = self.admin
            return ReceiptVoidView.as_view()(req, number=r.number, action=action)

        self.assertEqual(post("void", {}).status_code, 400)                # a reason is required
        self.assertEqual(post("void", {"reason": "Payment reversed"}).status_code, 200)
        r.refresh_from_db()
        self.assertTrue(r.is_void)
        self.assertEqual((r.void_reason, r.voided_by), ("Payment reversed", self.admin))
        self.assertIn("Receipt withdrawn", self.verify(r.verify_code).content.decode())
        self.assertTrue(BillingEvent.objects.filter(type="receipt.voided").exists())

        self.assertEqual(post("restore", {}).status_code, 200)
        r.refresh_from_db()
        self.assertFalse(r.is_void)
        self.assertIn("Genuine receipt", self.verify(r.verify_code).content.decode())
        with self.assertRaises(Http404):
            post("explode", {})

    @unittest.skipUnless(HAVE_REPORTLAB, "reportlab not installed")
    def test_pdf_with_qr_and_void_stamp_renders(self):
        self.pay()
        r = Receipt.objects.get(invoice=self.invoice)
        self.assertTrue(receipts.render_receipt_pdf(r, base_url="https://app.example.com").startswith(b"%PDF"))
        r.is_void, r.void_reason = True, "Payment reversed"
        self.assertTrue(receipts.render_receipt_pdf(r).startswith(b"%PDF"))      # no site URL: no QR, still fine
