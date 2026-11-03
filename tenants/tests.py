import json
import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from billing import services
from billing.models import Invoice, MpesaPayment, Subscription
from billing.pricing import quote
from inventory.models import Branch
from tenants.context import tenant_context
from tenants.models import CrossTenantWrite, PendingSignup, Tenant, TenantNotSet
from tenants.services import compose_username, create_staff_user, register_business

User = get_user_model()
SIGNUP = dict(business_name="Mama Njeri Mart", business_type="MINI_MART", owner_name="Njeri W",
              email="njeri@example.com", phone="254712345678", password="S3cure-pass-99", term_months=3)


def signup(**over):
    return register_business(**{**SIGNUP, **over})


def callback(checkout_id, amount, code=0, receipt="RCP123"):
    body = {"MerchantRequestID": "m1", "CheckoutRequestID": checkout_id, "ResultCode": code,
            "ResultDesc": "ok" if code == 0 else "Request cancelled by user"}
    if code == 0:
        body["CallbackMetadata"] = {"Item": [{"Name": "Amount", "Value": float(amount)},
                                             {"Name": "MpesaReceiptNumber", "Value": receipt}]}
    return {"Body": {"stkCallback": body}}


class IsolationTests(TestCase):
    def setUp(self):
        self.t1, self.o1, _ = signup()
        self.t2, self.o2, _ = signup(business_name="Kamau Pharmacy", email="kamau@example.com")

    def test_queries_are_scoped_and_fail_closed(self):
        with tenant_context(self.t1):
            Branch.objects.create(name="Westlands")
            self.assertEqual(Branch.objects.count(), 2)  # Main Branch + Westlands
        with tenant_context(self.t2):
            self.assertEqual(Branch.objects.count(), 1)
            Branch.objects.create(name="Westlands")      # same name allowed in another tenant
        self.assertEqual(Branch.objects.count(), 0)      # no tenant -> nothing
        self.assertEqual(Branch.all_objects.count(), 4)

    def test_cannot_save_without_or_across_tenant(self):
        with self.assertRaises(TenantNotSet):
            Branch.objects.create(name="Ghost")
        with tenant_context(self.t1):
            b = Branch.objects.first()
        with tenant_context(self.t2):
            with self.assertRaises(CrossTenantWrite):
                b.save()

    def test_staff_usernames_are_namespaced(self):
        u = create_staff_user(tenant=self.t1, handle="john", role="CASHIER", password="Pw-12345-xx")
        self.assertEqual(u.username, compose_username(self.t1.slug, "john"))
        create_staff_user(tenant=self.t2, handle="john", role="CASHIER", password="Pw-54321-xx")  # no clash


class PricingTests(TestCase):
    def test_amounts(self):
        self.assertEqual(quote(1, include_install_fee=True)["total"], Decimal("4499"))
        self.assertEqual(quote(3)["subscription_amount"], Decimal("3148"))
        self.assertEqual(quote(6)["subscription_amount"], Decimal("6296"))
        self.assertEqual(quote(12)["subscription_amount"], Decimal("12592"))
        self.assertEqual(quote(12, include_install_fee=True)["total"], Decimal("15592"))


@override_settings(TENANCY_ALLOW_NULL_TENANT=False)
class BillingFlowTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, self.invoice = signup()

    @property
    def fresh(self):
        from tenants.models import Tenant
        return Tenant.objects.get(pk=self.tenant.pk)

    def _pay(self, amount=None, code=0, receipt="RCP123"):
        with mock.patch("billing.mpesa.stk_push",
                        return_value={"MerchantRequestID": "m1", "CheckoutRequestID": f"ws_CO_{receipt}"}):
            payment = services.initiate_stk_payment(self.invoice, "0712345678")
        services.handle_stk_callback(callback(payment.checkout_request_id,
                                              amount or self.invoice.total, code, receipt))
        return payment

    def test_first_invoice_includes_install_fee(self):
        self.assertEqual(self.invoice.total, Decimal("6148"))   # 3148 + 3000
        self.assertEqual(self.tenant.subscription.status, "PENDING")
        self.assertFalse(self.tenant.has_access)

    def test_payment_activates_and_is_idempotent(self):
        p = self._pay()
        services.handle_stk_callback(callback(p.checkout_request_id, self.invoice.total))  # duplicate
        sub = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(sub.status, "ACTIVE")
        self.assertTrue(self.fresh.has_access)
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, "PAID")
        self.assertEqual(sub.monthly_equivalent, Decimal("1049.33"))
        self.assertEqual(self.tenant.billing_events.filter(type="invoice.payment_succeeded").count(), 1)
        # next invoice has NO install fee
        nxt = services.issue_invoice(self.tenant, 1)
        self.assertEqual(nxt.total, Decimal("1499"))

    def test_cancelled_prompt_and_underpayment_do_not_activate(self):
        self._pay(code=1032, receipt="A")
        self.assertFalse(self.fresh.has_access)
        self._pay(amount=100, receipt="B")
        self.assertFalse(self.fresh.has_access)
        self.assertTrue(self.tenant.billing_events.filter(type="invoice.payment_underpaid").exists())

    def test_lapse_grace_then_lockout_then_reactivation(self):
        self._pay()
        sub = Subscription.objects.get(tenant=self.tenant)
        end = sub.current_period_end
        services.run_billing_cycle(now=end - timedelta(days=3))           # renewal invoice appears
        self.assertTrue(Invoice.objects.filter(tenant=self.tenant, status="OPEN").exists())
        services.run_billing_cycle(now=end + timedelta(hours=1))
        self.assertEqual(Subscription.objects.get(tenant=self.tenant).status, "PAST_DUE")
        self.assertTrue(Subscription.objects.get(tenant=self.tenant).grants_access(end + timedelta(days=2)))
        services.run_billing_cycle(now=end + timedelta(days=6))
        sub.refresh_from_db()
        self.assertEqual(sub.status, "SUSPENDED")
        self.assertFalse(sub.grants_access(end + timedelta(days=6)))
        services.run_billing_cycle(now=end + timedelta(days=80))
        sub.refresh_from_db()
        self.assertEqual(sub.status, "CANCELLED")
        self.assertTrue(self.tenant.billing_events.filter(type="subscription.deleted").exists())
        inv = Invoice.objects.get(tenant=self.tenant, status="OPEN")
        services.mark_invoice_paid(inv)
        sub.refresh_from_db()
        self.assertEqual(sub.status, "ACTIVE")

    def test_comp_override_grants_access(self):
        su = User.objects.create_superuser("root", "r@x.com", "pw")
        services.grant_complimentary_access(self.tenant, days=30, by=su, note="pilot")
        self.tenant.refresh_from_db()
        self.assertTrue(Subscription.objects.get(tenant=self.tenant).grants_access())


@override_settings(TENANCY_ALLOW_NULL_TENANT=False)
class HttpTests(TestCase):
    def test_signup_gate_callback_and_platform(self):
        resp = self.client.post("/signup/", data=json.dumps({**SIGNUP, "phone": "0712345678"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 202, resp.content)
        self.assertFalse(User.objects.filter(username="njeri@example.com").exists())  # not until the link is opened
        link = re.search(r"https?://\S+/signup/verify/\S+/", mail.outbox[0].body).group(0)
        self.assertEqual(self.client.get(link).status_code, 302)                       # verified + signed in -> /billing/
        tenant = User.objects.get(username="njeri@example.com").tenant
        # unpaid owner: billing reachable, everything else locked
        self.assertEqual(self.client.get("/billing/").status_code, 200)
        with mock.patch("billing.mpesa.stk_push",
                        return_value={"MerchantRequestID": "m", "CheckoutRequestID": "ws_1"}):
            r = self.client.post("/billing/checkout/", {"term_months": 3, "phone": "0712345678"})
        self.assertEqual(r.status_code, 201, r.content)
        bad = self.client.post("/billing/mpesa/callback/wrong/", json.dumps(callback("ws_1", 6148)),
                               content_type="application/json")
        self.assertEqual(bad.status_code, 404)
        ok = self.client.post("/billing/mpesa/callback/topsecret/", json.dumps(callback("ws_1", 6148)),
                              content_type="application/json")
        self.assertEqual(ok.json()["ResultCode"], 0)
        tenant.refresh_from_db()
        self.assertTrue(tenant.has_access)
        self.assertEqual(self.client.get("/platform/").status_code, 403)  # paid, but not a superuser
        # a cashier of another tenant cannot hit this owner's billing
        t2, _, _ = signup(business_name="Other", email="o@example.com")
        create_staff_user(tenant=t2, handle="sam", role="CASHIER", password="Pw-12345-xx")
        self.client.logout()
        self.client.login(username=compose_username(t2.slug, "sam"), password="Pw-12345-xx")
        self.assertEqual(self.client.get("/billing/").status_code, 403)
        self.assertEqual(self.client.get("/anything/", HTTP_ACCEPT="application/json").status_code, 402)

    def test_platform_overview(self):
        signup()
        su = User.objects.create_superuser("root", "r@x.com", "pw")
        self.client.force_login(su)
        r = self.client.get("/platform/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["tenants_total"], 1)
        t = signup(business_name="B2", email="b2@example.com")[0]
        r = self.client.post(f"/platform/tenants/{t.pk}/comp/", {"days": 14, "note": "demo"})
        self.assertTrue(r.json()["has_access"])
        r = self.client.post(f"/platform/tenants/{t.pk}/support-start/")
        self.assertEqual(self.client.get("/platform/tenants/").status_code, 200)



@override_settings(TENANCY_ALLOW_NULL_TENANT=False, SIGNUP_RESEND_SECONDS=60,
                   SITE_URL="https://shop.example.com")
class SignupVerificationTests(TestCase):
    """Public sign-up: form -> emailed link -> account. Nothing exists before the link is opened."""
    form = {**SIGNUP, "phone": "0712 345 678", "term_months": 1, "branch_count": 1, "branch_names": []}

    def post(self, **over):
        return self.client.post("/signup/", data=json.dumps({**self.form, **over}),
                                content_type="application/json")

    def link(self, n=-1):
        return re.search(r"https://shop\.example\.com(/signup/verify/\S+/)", mail.outbox[n].body).group(1)

    def test_signup_sends_link_and_creates_nothing_yet(self):
        r = self.post()
        self.assertEqual(r.status_code, 202, r.content)
        self.assertEqual(r.json()["next"], "/signup/check-email/")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["njeri@example.com"])
        self.assertFalse(User.objects.filter(email="njeri@example.com").exists())
        self.assertEqual(Tenant.objects.count(), 0)
        self.assertEqual(self.client.get("/signup/check-email/").status_code, 200)

    def test_link_creates_verified_owner_signs_in_and_works_once(self):
        self.post()
        url = self.link()
        r = self.client.get(url)
        self.assertRedirects(r, "/billing/", fetch_redirect_response=False)
        owner = User.objects.get(username="njeri@example.com")
        self.assertTrue(owner.is_owner)
        self.assertIsNotNone(owner.email_verified_at)
        self.assertEqual(owner.phone, "254712345678")                  # owner's own number, normalised
        self.assertEqual(owner.tenant.phone, "254712345678")
        self.assertTrue(owner.check_password("S3cure-pass-99"))        # password survived as a hash
        self.assertEqual(self.client.get("/billing/").status_code, 200)  # already signed in
        self.assertEqual(Invoice.objects.filter(tenant=owner.tenant).count(), 1)
        self.assertEqual(Branch.all_objects.filter(tenant=owner.tenant).count(), 1)
        # second click: link is spent
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(Tenant.objects.count(), 1)

    def test_garbage_and_expired_links(self):
        self.assertEqual(self.client.get("/signup/verify/not-a-real-token/").status_code, 404)
        self.post()
        PendingSignup.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(self.link()).status_code, 410)
        self.assertEqual(Tenant.objects.count(), 0)

    def test_any_kenyan_number_accepted_and_junk_rejected(self):
        self.assertEqual(self.post(phone="0733123456").status_code, 202)         # Airtel-range, not just Safaricom
        r = self.post(phone="12345", email="x@example.com")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Kenyan phone number", r.json()["errors"]["phone"][0]["message"])

    def test_taken_email_is_rejected_before_any_email_goes_out(self):
        signup()
        r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertIn("email", r.json()["errors"])
        self.assertEqual(len(mail.outbox), 0)

    def test_resend_cooldown_then_new_link_replaces_old(self):
        self.post()
        old = self.link()
        self.assertEqual(self.post().status_code, 429)                  # form can't be used to flood an inbox
        self.assertEqual(len(mail.outbox), 1)
        r = self.client.post("/signup/resend/")                         # session remembers the address
        self.assertIn("wait=", r["Location"])
        self.assertEqual(len(mail.outbox), 1)
        PendingSignup.objects.update(last_sent_at=timezone.now() - timedelta(minutes=5))
        r = self.client.post("/signup/resend/")
        self.assertIn("sent=1", r["Location"])
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.client.get(old).status_code, 404)         # old link dead
        self.assertEqual(self.client.get(self.link()).status_code, 302)

    def test_resending_signup_replaces_earlier_attempt(self):
        self.post(business_name="Typo Mart")
        PendingSignup.objects.update(last_sent_at=timezone.now() - timedelta(minutes=5))
        self.post(business_name="Right Mart")
        self.assertEqual(PendingSignup.objects.count(), 1)
        self.assertEqual(self.client.get(self.link(0)).status_code, 404)
        self.client.get(self.link(1))
        self.assertEqual(Tenant.objects.get().name, "Right Mart")

    def test_email_failure_is_reported_not_swallowed(self):
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            r = self.post()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(Tenant.objects.count(), 0)

    def test_new_business_types_from_the_form_are_accepted(self):
        for i, kind in enumerate(("HARDWARE", "AGROVET")):
            self.assertEqual(self.post(business_type=kind, email=f"k{i}@example.com").status_code, 202, kind)

    def test_register_business_needs_a_credential(self):
        with self.assertRaises(ValueError):
            register_business(**{k: v for k, v in SIGNUP.items() if k != "password"})

    def test_pages_render_with_the_new_wording(self):
        page = self.client.get("/signup/")
        self.assertContains(page, "Phone number")
        self.assertNotContains(page, "M-Pesa number")
        self.assertContains(page, "confirm it")
        self.assertContains(self.client.get("/users/login/"), "/signup/")
        self.assertRedirects(self.client.get("/signup/check-email/"), "/signup/", fetch_redirect_response=False)
        self.post()
        self.assertContains(self.client.get("/signup/check-email/"), "njeri@example.com")
        self.assertContains(self.client.get("/signup/verify/bogus/"), "isn", status_code=404)
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn(self.link(), html)


@override_settings(TENANCY_ALLOW_NULL_TENANT=False, SIGNUP_RESEND_SECONDS=60,
                   SITE_URL="https://shop.example.com")
class CheckEmailRedirectTests(TestCase):
    """The check-email page must move on by itself once the link is clicked."""
    form = {**SIGNUP, "phone": "0712 345 678", "term_months": 1, "branch_count": 1, "branch_names": []}

    def start(self):
        self.client.post("/signup/", data=json.dumps(self.form), content_type="application/json")
        return re.search(r"https://shop\.example\.com(/signup/verify/\S+/)", mail.outbox[-1].body).group(1)

    def status(self, client=None):
        return (client or self.client).get("/signup/check-email/status/").json()

    def test_pending_then_verified_on_another_device(self):
        link = self.start()
        self.assertEqual(self.status(), {"state": "pending", "next": ""})
        from django.test import Client
        Client().get(link)                                     # the phone clicks the link
        self.assertEqual(self.status(), {"state": "verified", "next": "/users/login/?verified=1"})
        self.assertRedirects(self.client.get("/signup/check-email/"), "/users/login/?verified=1",
                             fetch_redirect_response=False)
        page = self.client.get("/users/login/?verified=1")      # email is pre-filled
        self.assertContains(page, 'value="njeri@example.com"')
        self.assertContains(page, "Email confirmed")

    def test_verified_in_same_browser_goes_to_billing(self):
        link = self.start()
        self.client.get(link)
        self.assertEqual(self.status(), {"state": "verified", "next": "/billing/"})

    def test_expired_link_is_reported(self):
        self.start()
        PendingSignup.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.status()["state"], "expired")
        self.assertContains(self.client.get("/signup/check-email/"), "expired")

    def test_nothing_started_here_means_nothing_to_wait_for(self):
        self.assertEqual(self.status(), {"state": "none", "next": "/signup/"})

    def test_cannot_learn_that_someone_elses_email_has_an_account(self):
        register_business(**{**SIGNUP, "email_verified": True})            # existing, confirmed earlier
        s = self.client.session
        s["signup_pending_email"] = "njeri@example.com"
        s["signup_started_at"] = timezone.now().isoformat()
        s.save()
        self.assertEqual(self.status()["state"], "none")
