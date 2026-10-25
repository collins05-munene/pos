import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from billing import manual, services
from billing.models import (BillingCredit, BillingEvent, Invoice, ManualPaymentRequest, MpesaCode, Subscription)
from tenants.models import Tenant

User = get_user_model()

SMS = ("SJK3L9X2QP Confirmed. Ksh2.00 received from JOHN DOE 0712345678 on 24/10/26 at 2:15 PM. "
       "New Account balance is Ksh5,000.00. Transaction cost, Ksh0.00.")


def make_shop(name="Shop A", email="a@example.com", term=1):
    tenant = Tenant.objects.create(name=name, slug=name.lower().replace(" ", "-"), phone="0712000000")
    owner = User.objects.create_user(username=email, email=email, password="pw", tenant=tenant,
                                     is_owner=True, role="ADMIN")
    _sub, invoice = services.start_subscription(tenant, term)
    return tenant, owner, invoice


class SmsParsingTests(TestCase):
    def test_till_message(self):
        p = manual.parse_mpesa_sms(SMS)
        self.assertEqual(p["code"], "SJK3L9X2QP")
        self.assertEqual(p["amount"], Decimal("2.00"))
        self.assertEqual(p["phone"], "0712345678")
        self.assertEqual(p["name"], "JOHN DOE")
        self.assertEqual((p["transaction_at"].hour, p["transaction_at"].minute), (14, 15))

    def test_paybill_message_phone_first_and_thousands(self):
        p = manual.parse_mpesa_sms(
            "UJK1ABC2DE Confirmed. Ksh1,499.00 received from 254712345678 JANE W on 3/1/2026 at 12:05 AM. Account Number X")
        self.assertEqual((p["code"], p["amount"], p["phone"], p["name"]),
                         ("UJK1ABC2DE", Decimal("1499.00"), "254712345678", "JANE W"))
        self.assertEqual(p["transaction_at"].hour, 0)

    def test_masked_phone(self):
        p = manual.parse_mpesa_sms("ABCDE12345 Confirmed. Ksh500.00 received from 2547*****123 SAM K on 1/2/26 at 9:00 AM.")
        self.assertEqual(p["phone"], "2547*****123")

    def test_no_code(self):
        with self.assertRaises(services.BillingError):
            manual.parse_mpesa_sms("hello there")

    def test_code_normalising(self):
        self.assertEqual(manual.normalize_code(" sjk3 l9x2qp "), "SJK3L9X2QP")
        for bad in ("", "SHORT", "has-dash-123", "X" * 30):
            with self.assertRaises(services.BillingError):
                manual.normalize_code(bad)


class ManualPaymentFlowTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="root", email="r@x.com", password="pw")
        self.tenant, self.owner, self.invoice = make_shop()   # 1 month = KES 1 + 2 install = 3 total
        self.assertEqual(self.invoice.total, Decimal("3"))

    def add(self, code="SJK3L9X2QP", amount="3"):
        return manual.register_mpesa_code(by=self.admin, code=code, amount=amount)

    def submit(self, code="SJK3L9X2QP", tenant=None, invoice=None, user=None):
        return manual.submit_manual_payment(tenant or self.tenant, user=user or self.owner,
                                            invoice=invoice or self.invoice, raw_code=code)

    def test_listed_code_approves_and_activates(self):
        self.add()
        req = self.submit("sjk3l9x2qp")
        self.assertEqual(req.status, "APPROVED")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        sub = Subscription.objects.get(tenant=self.tenant)
        self.assertEqual(sub.status, Subscription.Status.ACTIVE)
        self.assertTrue(Tenant.objects.get(pk=self.tenant.pk).has_access)
        code = MpesaCode.objects.get(code="SJK3L9X2QP")
        self.assertEqual((code.state, code.used_by_tenant, code.used_invoice), ("USED", self.tenant, self.invoice))
        self.assertTrue(BillingEvent.objects.filter(type="invoice.payment_succeeded").exists())

    def test_unknown_code_waits_then_auto_approves_when_listed(self):
        req = self.submit()
        self.assertEqual(req.status, "PENDING")
        self.assertFalse(Tenant.objects.get(pk=self.tenant.pk).has_access)
        _code, approved = self.add()
        self.assertEqual([r.pk for r in approved], [req.pk])
        req.refresh_from_db()
        self.assertEqual(req.status, "APPROVED")
        self.assertTrue(Tenant.objects.get(pk=self.tenant.pk).has_access)

    def test_resubmitting_pending_code_does_not_duplicate(self):
        self.submit(); self.submit()
        self.assertEqual(ManualPaymentRequest.objects.count(), 1)

    def test_underpayment_is_kept_as_credit_and_topped_up(self):
        self.add(amount="1"); self.add("TOPUP00002", amount="2")
        first = self.submit()
        self.assertEqual(first.status, "PARTIAL")
        self.assertIn("2 is still due", first.reason)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.OPEN)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("1"))
        self.assertEqual(MpesaCode.objects.get(code="SJK3L9X2QP").state, "USED")   # can't be replayed
        self.assertFalse(Tenant.objects.get(pk=self.tenant.pk).has_access)

        second = self.submit("TOPUP00002")
        self.assertEqual(second.status, "APPROVED")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("0"))
        self.assertTrue(Tenant.objects.get(pk=self.tenant.pk).has_access)

    def test_partial_code_cannot_be_replayed(self):
        self.add(amount="1")
        self.submit()
        again = self.submit()
        self.assertEqual(again.status, "REJECTED")
        self.assertEqual(services.credit_balance(self.tenant), Decimal("1"))

    def test_overpayment_pays_and_keeps_surplus_as_credit(self):
        self.add(amount="10")
        req = self.submit()
        self.assertEqual(req.status, "APPROVED")
        self.assertIn("extra", req.reason)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("7"))
        self.assertTrue(BillingEvent.objects.filter(type="manual_payment.credit_carried").exists())

    def test_surplus_credit_pays_next_invoice_automatically(self):
        self.add(amount="10"); self.submit()                         # 3 paid, 7 credit
        renewal = services.issue_invoice(self.tenant, 1, include_install_fee=False)   # KES 1
        self.assertEqual(renewal.status, Invoice.Status.PAID)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("6"))
        self.assertTrue(BillingEvent.objects.filter(type="invoice.paid_from_credit").exists())

    def test_credit_less_than_next_invoice_reduces_amount_due(self):
        self.add(amount="3"); self.submit()
       
        BillingCredit.objects.create(tenant=self.tenant, amount=Decimal("0.50"),
                                     kind=BillingCredit.Kind.ADJUSTMENT)
        t3 = services.issue_invoice(self.tenant, 3, include_install_fee=False)        # 3 months = KES 2
        self.assertEqual(t3.status, Invoice.Status.OPEN)
        self.assertEqual(services.amount_due(t3), Decimal("1.50"))

    def test_code_cannot_pay_a_second_invoice(self):
        self.add()
        self.submit()
        other_tenant, other_owner, other_invoice = make_shop("Shop B", "b@example.com")
        req = self.submit(tenant=other_tenant, invoice=other_invoice, user=other_owner)
        self.assertEqual(req.status, "REJECTED")
        self.assertIn("already been used", req.reason)
        other_invoice.refresh_from_db()
        self.assertEqual(other_invoice.status, Invoice.Status.OPEN)
        self.assertTrue(BillingEvent.objects.filter(type="manual_payment.code_reuse_attempt").exists())

    def test_voided_code_is_rejected(self):
        code, _ = self.add()
        manual.void_code(code, by=self.admin)
        self.assertEqual(self.submit().status, "REJECTED")

    def test_cannot_void_used_code(self):
        code, _ = self.add()
        self.submit()
        with self.assertRaises(services.BillingError):
            manual.void_code(code, by=self.admin)

    def test_duplicate_listing_rejected(self):
        self.add()
        with self.assertRaises(services.BillingError):
            self.add()

    def test_contested_code_is_not_auto_approved(self):
        other_tenant, other_owner, other_invoice = make_shop("Shop B", "b@example.com")
        mine = self.submit()
        theirs = self.submit(tenant=other_tenant, invoice=other_invoice, user=other_owner)
        _code, approved = self.add()
        self.assertEqual(approved, [])
        mine.refresh_from_db(); theirs.refresh_from_db()
        self.assertEqual((mine.status, theirs.status), ("PENDING", "PENDING"))
        # you decide: approve one, the other is then rejected as "already used"
        manual.approve_request(mine, by=self.admin)
        manual.reject_request(theirs, by=self.admin, reason="Not our payment")
        mine.refresh_from_db(); theirs.refresh_from_db()
        self.assertEqual((mine.status, theirs.status), ("APPROVED", "REJECTED"))

    def test_approve_needs_listed_code(self):
        req = self.submit()
        with self.assertRaises(services.BillingError):
            manual.approve_request(req, by=self.admin)

    def test_throttle_after_too_many_misses(self):
        for i in range(8):
            self.submit(code=f"ZZZZZZZZ{i:02d}")
        with self.assertRaises(services.BillingError) as cm:
            self.submit(code="ZZZZZZZZ99")
        self.assertIn("Too many", str(cm.exception))

    def test_paid_invoice_not_payable_again(self):
        self.add(); self.submit()
        self.add("AAAAAAAAAA")
        with self.assertRaises(services.BillingError):
            self.submit("AAAAAAAAAA")

    @override_settings(BILLING={"MANUAL_PAYMENTS_ENABLED": False})
    def test_feature_switch(self):
        with self.assertRaises(services.BillingError):
            self.submit()

    def test_renewal_extends_from_current_period_end(self):
        self.add(); self.submit()
        sub = Subscription.objects.get(tenant=self.tenant)
        first_end = sub.current_period_end
        renewal = services.issue_invoice(self.tenant, 1, include_install_fee=False)   # KES 1
        self.add("RENEWAL001", amount="1")
        self.assertEqual(self.submit("RENEWAL001", invoice=renewal).status, "APPROVED")
        sub.refresh_from_db()
        self.assertGreater(sub.current_period_end, first_end)


class ManualPaymentViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="root", email="r@x.com", password="pw")
        self.tenant, self.owner, self.invoice = make_shop()
        self.client_owner = Client(); self.client_owner.force_login(self.owner)
        self.client_admin = Client(); self.client_admin.force_login(self.admin)

    def post_json(self, client, url, body):
        return client.post(url, json.dumps(body), content_type="application/json")

    def attach_tenant(self):
        # the project's TenantMiddleware isn't in the test settings; emulate it
        from tenants.context import set_current_tenant
        return set_current_tenant(self.tenant)

    def test_owner_flow_over_http(self):
        from django.test import RequestFactory
        from billing.views import ManualPaymentView
        manual.register_mpesa_code(by=self.admin, code="SJK3L9X2QP", amount="3")
        rf = RequestFactory()
        req = rf.post("/billing/manual-payment/", json.dumps({"code": "sjk3l9x2qp"}),
                      content_type="application/json")
        req.user, req.tenant = self.owner, self.tenant
        resp = ManualPaymentView.as_view()(req)
        data = json.loads(resp.content)
        self.assertEqual((resp.status_code, data["status"], data["has_access"]), (200, "APPROVED", True))

    def test_partial_payment_response(self):
        from django.test import RequestFactory
        from billing.views import ManualPaymentView
        manual.register_mpesa_code(by=self.admin, code="SJK3L9X2QP", amount="1")
        req = RequestFactory().post("/billing/manual-payment/", {"code": "SJK3L9X2QP"})
        req.user, req.tenant = self.owner, self.tenant
        resp = ManualPaymentView.as_view()(req)
        d = json.loads(resp.content)
        self.assertEqual((resp.status_code, d["status"]), (200, "PARTIAL"))
        self.assertEqual((Decimal(d["credit_balance"]), Decimal(d["amount_due"])), (Decimal("1"), Decimal("2")))
        self.assertFalse(d["has_access"])

    def test_reused_code_returns_422_with_reason(self):
        from django.test import RequestFactory
        from billing.views import ManualPaymentView
        manual.register_mpesa_code(by=self.admin, code="SJK3L9X2QP", amount="3")
        other, other_owner, other_inv = make_shop("Shop B", "b@example.com")
        manual.submit_manual_payment(other, user=other_owner, invoice=other_inv, raw_code="SJK3L9X2QP")
        req = RequestFactory().post("/billing/manual-payment/", {"code": "SJK3L9X2QP"})
        req.user, req.tenant = self.owner, self.tenant
        resp = ManualPaymentView.as_view()(req)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("already been used", json.loads(resp.content)["reason"])

    def test_credit_alone_can_cover_invoice_without_a_code(self):
        from django.test import RequestFactory
        from billing.views import ManualPaymentView
        BillingCredit.objects.create(tenant=self.tenant, amount=Decimal("50"),
                                     kind=BillingCredit.Kind.ADJUSTMENT)
        req = RequestFactory().post("/billing/manual-payment/", {})
        req.user, req.tenant = self.owner, self.tenant
        d = json.loads(ManualPaymentView.as_view()(req).content)
        self.assertEqual((d["status"], d["has_access"]), ("APPROVED", True))

    def test_stk_checkout_is_blocked_with_friendly_message(self):
        from django.test import RequestFactory
        from billing.views import CheckoutView
        open_before = Invoice.objects.filter(tenant=self.tenant, status="OPEN").count()
        req = RequestFactory().post("/billing/checkout/", {"term_months": 12, "phone": "0712345678"})
        req.user, req.tenant = self.owner, self.tenant
        resp = CheckoutView.as_view()(req)
        d = json.loads(resp.content)
        self.assertEqual((resp.status_code, d["code"]), (400, "stk_unavailable"))
        self.assertIn("in development", d["error"])
        self.assertEqual(Invoice.objects.filter(tenant=self.tenant, status="OPEN").count(), open_before)
        self.assertEqual(Invoice.objects.filter(tenant=self.tenant, status="VOID").count(), 0)

    def test_stk_service_guard(self):
        with self.assertRaises(services.BillingError):
            services.initiate_stk_payment(self.invoice, "0712345678")

    def test_bad_code_format_is_400(self):
        from django.test import RequestFactory
        from billing.views import ManualPaymentView
        req = RequestFactory().post("/billing/manual-payment/", {"code": "!!"})
        req.user, req.tenant = self.owner, self.tenant
        self.assertEqual(ManualPaymentView.as_view()(req).status_code, 400)

    def test_non_owner_forbidden(self):
        from django.core.exceptions import PermissionDenied
        from django.test import RequestFactory
        from billing.views import ManualPaymentView
        cashier = User.objects.create_user(username="c", password="pw", tenant=self.tenant, role="CASHIER")
        req = RequestFactory().post("/billing/manual-payment/", {"code": "SJK3L9X2QP"})
        req.user, req.tenant = cashier, self.tenant
        with self.assertRaises(PermissionDenied):
            ManualPaymentView.as_view()(req)

    # ---- platform admin ----
    def test_platform_pages_need_superuser(self):
        self.assertEqual(self.client_owner.get("/platform/payments/api/").status_code, 403)
        self.assertEqual(self.client_owner.post("/platform/payments/api/add/", {}).status_code, 403)
        self.assertEqual(self.client_admin.get("/platform/payments/api/").status_code, 200)

    def test_add_from_pasted_sms_and_list(self):
        r = self.post_json(self.client_admin, "/platform/payments/api/parse/", {"message": SMS})
        self.assertEqual(r.json()["code"], "SJK3L9X2QP")
        self.assertFalse(r.json()["exists"])
        r = self.post_json(self.client_admin, "/platform/payments/api/add/", {"message": SMS})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["code"]["amount"], "2.00")
        listing = self.client_admin.get("/platform/payments/api/").json()
        self.assertEqual(listing["summary"]["unused_count"], 1)
        self.assertEqual(listing["codes"][0]["sender_name"], "JOHN DOE")
        # second paste of the same message -> friendly field error, not a 500
        r = self.post_json(self.client_admin, "/platform/payments/api/add/", {"message": SMS})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already", r.json()["errors"]["code"])

    def test_manual_fields_override_and_validation(self):
        r = self.post_json(self.client_admin, "/platform/payments/api/add/", {"code": "ABCDE12345"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("amount", r.json()["errors"])
        r = self.post_json(self.client_admin, "/platform/payments/api/add/",
                           {"code": "ABCDE12345", "amount": "1,499"})
        self.assertEqual(r.status_code, 201)

    def test_pending_shows_then_admin_actions(self):
        manual.submit_manual_payment(self.tenant, user=self.owner, invoice=self.invoice, raw_code="PENDING001")
        listing = self.client_admin.get("/platform/payments/api/").json()
        self.assertEqual(listing["summary"]["pending_count"], 1)
        row = listing["pending"][0]
        self.assertFalse(row["code_listed"])
        r = self.client_admin.post(f"/platform/payments/api/requests/{row['id']}/approve/")
        self.assertEqual(r.status_code, 400)                    # code not listed yet
        r = self.post_json(self.client_admin, f"/platform/payments/api/requests/{row['id']}/reject/",
                           {"reason": "No such payment"})
        self.assertEqual(r.json()["status"], "REJECTED")
        self.assertEqual(self.client_admin.post(f"/platform/payments/api/requests/{row['id']}/explode/").status_code, 404)

    def test_adding_code_reports_who_was_approved(self):
        manual.submit_manual_payment(self.tenant, user=self.owner, invoice=self.invoice, raw_code="ABCDE12345")
        r = self.post_json(self.client_admin, "/platform/payments/api/add/", {"code": "ABCDE12345", "amount": "3"})
        self.assertEqual(r.json()["approved"], [{"tenant": "Shop A", "invoice": self.invoice.number}])


class OverviewPayloadAndTemplateTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="root", email="r@x.com", password="pw")
        self.tenant, self.owner, self.invoice = make_shop()

    @override_settings(BILLING={"MANUAL_PAY_LABEL": "Till number", "MANUAL_PAY_NUMBER": "123456",
                                "MANUAL_PAY_NAME": "UniSale"})
    def test_overview_json_carries_manual_pay_and_requests(self):
        from django.test import RequestFactory
        from billing.views import BillingOverviewView
        manual.submit_manual_payment(self.tenant, user=self.owner, invoice=self.invoice, raw_code="PENDING001")
        req = RequestFactory().get("/billing/", headers={"Accept": "application/json"})
        req.user, req.tenant = self.owner, self.tenant
        d = json.loads(BillingOverviewView.as_view()(req).content)
        self.assertEqual(d["manual_pay"], {"enabled": True, "label": "Till number",
                                           "number": "123456", "name": "UniSale"})
        self.assertEqual([(r["code"], r["status"]) for r in d["manual_requests"]], [("PENDING001", "PENDING")])
        self.assertEqual(d["open_invoice"]["number"], self.invoice.number)

    def test_platform_payments_page_renders(self):
        c = Client(); c.force_login(self.admin)
        r = c.get("/platform/payments/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "data-add=\"/platform/payments/api/add/\"")
        self.assertContains(r, "platform-payments.js")

    def test_billing_template_compiles(self):
        from django.template.loader import get_template
        get_template("billing/overview.html")


class CreditNoticeTests(TestCase):
    """Overpayment/credit behaviour and what the billing page + notifications are told."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username="root", email="r@x.com", password="pw")
        self.tenant, self.owner, self.invoice = make_shop()      # KES 3 (1 + 2 install)

    def add(self, code, amount):
        return manual.register_mpesa_code(by=self.admin, code=code, amount=amount)

    def submit(self, code, invoice=None):
        return manual.submit_manual_payment(self.tenant, user=self.owner,
                                            invoice=invoice or self.invoice, raw_code=code)

    def overview(self):
        from django.test import RequestFactory
        from billing.views import BillingOverviewView
        req = RequestFactory().get("/billing/", headers={"Accept": "application/json"})
        req.user, req.tenant = self.owner, self.tenant
        return json.loads(BillingOverviewView.as_view()(req).content)

    def test_part_then_overpay_keeps_only_the_surplus(self):
        self.add("PART000001", "1"); self.add("OVER000001", "10")
        self.assertEqual(self.submit("PART000001").status, "PARTIAL")
        req = self.submit("OVER000001")
        self.assertEqual(req.status, "APPROVED")
        self.assertIn("KES 8 extra is kept as credit", req.reason)
        self.assertEqual(services.credit_balance(self.tenant), Decimal("8"))
        self.assertTrue(Tenant.objects.get(pk=self.tenant.pk).has_access)

    def test_pending_code_follows_a_term_change_to_the_current_invoice(self):
        self.submit("MOVED00001")                                   # waits: not listed yet
        new = services.issue_invoice(self.tenant, 3)                # shop switched term; old one is VOID
        self.assertEqual(Invoice.objects.get(pk=self.invoice.pk).status, Invoice.Status.VOID)
        self.add("MOVED00001", "1")
        r = ManualPaymentRequest.objects.get(code="MOVED00001")
        self.assertEqual(r.status, "PARTIAL")
        self.assertEqual(r.invoice_id, new.pk)
        self.assertIn(new.number, r.reason)
        self.assertIn(f"KES {new.total - 1:.0f} is still due", r.reason)

    def test_replaying_own_code_says_it_became_credit(self):
        self.add("PART000001", "1")
        self.submit("PART000001")
        again = self.submit("PART000001")
        self.assertEqual(again.status, "REJECTED")
        self.assertIn("already been used", again.reason)
        self.assertIn("added to your credit", again.reason)

    def test_overview_reports_live_amount_due_and_hides_stale_attempts(self):
        self.add("PART000001", "1"); self.add("USED000001", "1")
        self.submit("PART000001")
        self.submit("PART000001")                                    # REJECTED replay
        d = self.overview()
        self.assertEqual(Decimal(d["open_invoice"]["amount_due"]), Decimal("2"))
        self.assertEqual(Decimal(d["open_invoice"]["credit_applied"]), Decimal("1"))
        self.assertEqual(Decimal(d["credit_balance"]), Decimal("1"))
        statuses = [r["status"] for r in d["manual_requests"]]
        self.assertNotIn("PARTIAL", statuses)                       # covered by amount_due, not a stored message
        self.assertEqual(statuses, ["REJECTED"])
        self.assertEqual(d["credit_entries"][0]["kind"], "TOPUP")

    def test_old_rejections_drop_off_the_page(self):
        self.add("PART000001", "1")
        self.submit("PART000001"); self.submit("PART000001")
        ManualPaymentRequest.objects.filter(status="REJECTED").update(
            created_at=timezone.now() - timedelta(days=2))
        self.assertEqual(self.overview()["manual_requests"], [])

    def test_credit_covering_a_term_pays_without_a_code_over_http(self):
        self.add("BIG0000001", "20"); self.submit("BIG0000001")     # 3 paid, 17 credit
        c = Client(); c.force_login(self.owner)
        from tenants.context import set_current_tenant
        set_current_tenant(self.tenant)
        from django.test import RequestFactory
        from billing.views import ManualPaymentView
        req = RequestFactory().post("/billing/manual-payment/", json.dumps({"term_months": 6}),
                                    content_type="application/json")
        req.user, req.tenant = self.owner, self.tenant
        r = ManualPaymentView.as_view()(req)
        body = json.loads(r.content)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(body["status"], "APPROVED")
        from billing.pricing import quote
        six = quote(6, include_install_fee=False)["total"]                 # install fee already paid
        self.assertEqual(Decimal(body["credit_balance"]), Decimal("17") - six)

    def test_notifications_for_part_payment_extra_credit_and_credit_renewal(self):
        from notifications.models import Notification
        self.add("PART000001", "1"); self.add("OVER000001", "10")
        self.submit("PART000001"); self.submit("OVER000001")
        titles = set(Notification.objects.filter(tenant=self.tenant).values_list("title", flat=True))
        self.assertIn("Part payment received", titles)
        self.assertIn("Credit added to your account", titles)
        msg = Notification.objects.get(tenant=self.tenant, title="Credit added to your account").message
        self.assertEqual(msg, "KES 8 extra is kept as credit and will be deducted from your next invoice.")
        services.issue_invoice(self.tenant, 1, include_install_fee=False)
        self.assertTrue(Notification.objects.filter(tenant=self.tenant,
                                                    title="Invoice paid from your credit").exists())
