import json
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from billing import services
from billing.models import Invoice
from billing.pricing import quote
from inventory.models import Branch
from sales.utils import resolve_user_branch
from tenants import branches as rules
from tenants.branch_context import branch_context
from tenants.context import tenant_context
from tenants.services import create_staff_user, register_business

User = get_user_model()
BASE = dict(business_name="Mama Njeri Mart", business_type="MINI_MART", owner_name="Njeri W",
            email="njeri@example.com", phone="254712345678", password="S3cure-pass-99", term_months=1)


def signup(**over):
    return register_business(**{**BASE, **over})


def pay(invoice):
    return services.mark_invoice_paid(invoice, receipt="TEST")


class PricingTests(TestCase):
    def test_extra_branches_add_to_monthly_before_discount(self):
        one = quote(3)
        three = quote(3, extra_branches=2)
        # 2 extra branches * 500/month * 3 months, then the 30% term discount
        self.assertGreater(three["subscription_amount"], one["subscription_amount"])
        self.assertEqual(three["list_price"] - one["list_price"], Decimal("3000"))
        self.assertEqual(three["subscription_amount"] - one["subscription_amount"], Decimal("2100"))


class SignupBranchTests(TestCase):
    def test_signup_registers_every_branch_and_bills_for_them(self):
        tenant, owner, invoice = signup(branch_count=3, branch_names=["CBD", "Westlands", ""])
        names = list(Branch.all_objects.filter(tenant=tenant).order_by("pk").values_list("name", "is_main", "is_active"))
        self.assertEqual(names, [("CBD", True, True), ("Westlands", False, True), ("Branch 3", False, True)])
        owner.refresh_from_db()
        self.assertEqual(owner.branch.name, "CBD")
        self.assertEqual(tenant.subscription.branch_slots, 3)
        self.assertEqual(invoice.branch_slots, 3)
        self.assertEqual(invoice.total, quote(1, include_install_fee=True, extra_branches=2)["total"])

    def test_default_is_one_main_branch(self):
        tenant, owner, invoice = signup()
        self.assertEqual(Branch.all_objects.filter(tenant=tenant).count(), 1)
        self.assertEqual(invoice.total, quote(1, include_install_fee=True)["total"])

    def test_paying_signup_invoice_keeps_all_branches_active(self):
        tenant, _, invoice = signup(branch_count=2)
        pay(invoice)
        self.assertEqual(Branch.all_objects.filter(tenant=tenant, is_active=True).count(), 2)


class BranchRulesTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, self.invoice = signup()
        pay(self.invoice)
        self.tenant.refresh_from_db()

    def test_beyond_paid_slots_waits_for_payment_then_activates(self):
        b = rules.create_branch(self.tenant, name="Westlands")
        self.assertFalse(b.is_active)
        self.assertTrue(b.awaiting_payment)
        inv = services.issue_branch_upgrade(self.tenant)
        self.assertTrue(inv.is_upgrade)
        self.assertEqual(inv.branch_slots, 2)
        period_end = self.tenant.subscription.current_period_end
        # ~full month remaining -> close to one month of the extra branch
        self.assertAlmostEqual(float(inv.total), 500, delta=25)
        pay(inv)
        b.refresh_from_db()
        sub = self.tenant.subscription
        sub.refresh_from_db()
        self.assertTrue(b.is_active)
        self.assertFalse(b.awaiting_payment)
        self.assertEqual(sub.branch_slots, 2)
        self.assertEqual(sub.current_period_end, period_end)   # add-on never extends the term

    def test_upgrade_survives_payment_screen_reissues(self):
        rules.create_branch(self.tenant, name="Westlands")
        inv = services.issue_branch_upgrade(self.tenant)
        again = services.issue_invoice(self.tenant, self.tenant.subscription.term_months)
        self.assertEqual(again.pk, inv.pk)
        self.assertEqual(Invoice.objects.filter(tenant=self.tenant, status="OPEN").count(), 1)

    def test_renewal_voids_stale_upgrade_and_prices_all_branches(self):
        rules.create_branch(self.tenant, name="Westlands")
        services.issue_branch_upgrade(self.tenant)
        sub = self.tenant.subscription
        sub.current_period_end = timezone.now() + timedelta(days=2)
        sub.save()
        services.run_billing_cycle()
        open_inv = Invoice.objects.get(tenant=self.tenant, status="OPEN")
        self.assertFalse(open_inv.is_upgrade)
        self.assertEqual(open_inv.branch_slots, 2)
        self.assertEqual(open_inv.total, quote(1, extra_branches=1)["total"])
        pay(open_inv)
        self.assertTrue(Branch.all_objects.get(name="Westlands").is_active)

    def test_credit_can_pay_the_upgrade_on_the_spot(self):
        from billing.models import BillingCredit
        BillingCredit.objects.create(tenant=self.tenant, amount=Decimal("5000"),
                                     kind=BillingCredit.Kind.TOPUP)
        rules.create_branch(self.tenant, name="Westlands")
        inv = services.issue_branch_upgrade(self.tenant)
        self.assertEqual(inv.status, Invoice.Status.PAID)
        self.assertTrue(Branch.all_objects.get(name="Westlands").is_active)

    def test_duplicate_name_and_no_free_slot_are_rejected(self):
        with self.assertRaises(ValidationError):
            rules.create_branch(self.tenant, name="Main Branch")
        with self.assertRaises(ValidationError):
            rules.create_branch(self.tenant, name="B", allow_pending=False)

    def test_max_branches_is_enforced(self):
        from billing import conf
        for i in range(conf.get("MAX_BRANCHES") - 1):
            rules.create_branch(self.tenant, name=f"B{i}")
        with self.assertRaises(ValidationError):
            rules.create_branch(self.tenant, name="One too many")

    def test_main_branch_cannot_be_archived_and_archive_moves_staff(self):
        main = rules.main_branch(self.tenant)
        with self.assertRaises(ValidationError):
            rules.archive_branch(main)
        b = rules.create_branch(self.tenant, name="Westlands", active=True)
        cashier = create_staff_user(tenant=self.tenant, handle="amina", role="CASHIER", pin="1234",
                                    last_name="K", phone="254711111111", national_id="12345678", branch=b)
        rules.archive_branch(b)
        cashier.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(cashier.branch_id, main.pk)
        self.assertEqual(b.status, "ARCHIVED")

    def test_restore_without_a_free_slot_waits_for_payment(self):
        b = rules.create_branch(self.tenant, name="Westlands", active=True)   # 2 active, 1 paid
        rules.archive_branch(b)
        rules.restore_branch(b)
        b.refresh_from_db()
        self.assertEqual(b.status, "AWAITING_PAYMENT")

    def test_archived_branch_stops_being_billed_at_renewal(self):
        b = rules.create_branch(self.tenant, name="Westlands", active=True)
        sub = self.tenant.subscription
        sub.branch_slots = 2
        sub.save()
        self.assertEqual(services.billable_branches(self.tenant), 2)
        rules.archive_branch(b)
        self.assertEqual(services.billable_branches(self.tenant), 1)


class WhoWorksWhereTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, inv = signup(branch_count=2, branch_names=["CBD", "Westlands"])
        pay(inv)
        self.main = rules.main_branch(self.tenant)
        self.west = Branch.all_objects.get(name="Westlands")
        self.cashier = create_staff_user(tenant=self.tenant, handle="amina", role="CASHIER", pin="1234",
                                         last_name="K", phone="254711111111", national_id="12345678",
                                         branch=self.west)
        self.manager = create_staff_user(tenant=self.tenant, handle="joe", role="MANAGER", password="Str0ng-pass-77",
                                         last_name="M", phone="254722222222", national_id="87654321")

    def test_cashier_is_pinned_even_if_a_branch_context_is_set(self):
        with tenant_context(self.tenant), branch_context(self.main):
            self.assertEqual(resolve_user_branch(self.cashier), self.west)

    def test_manager_follows_the_switched_branch_else_main(self):
        with tenant_context(self.tenant):
            self.assertEqual(resolve_user_branch(self.manager), self.main)
            with branch_context(self.west):
                self.assertEqual(resolve_user_branch(self.manager), self.west)

    def test_cashier_without_branch_or_with_archived_branch_falls_back_to_main(self):
        self.cashier.branch = None
        self.assertEqual(rules.user_home_branch(self.cashier), self.main)
        self.cashier.branch = self.west
        rules.archive_branch(self.west)
        self.cashier.refresh_from_db()
        self.assertEqual(rules.user_home_branch(self.cashier), self.main)


class BranchEndpointTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, inv = signup(branch_count=2, branch_names=["CBD", "Westlands"])
        pay(inv)
        self.west = Branch.all_objects.get(name="Westlands")
        self.cashier = create_staff_user(tenant=self.tenant, handle="amina", role="CASHIER", pin="1234",
                                         last_name="K", phone="254711111111", national_id="12345678",
                                         branch=self.west)
        self.manager = create_staff_user(tenant=self.tenant, handle="joe", role="MANAGER", password="Str0ng-pass-77",
                                         last_name="M", phone="254722222222", national_id="87654321")
        self.other, self.other_owner, other_inv = signup(business_name="Kamau Pharmacy", email="kamau@example.com")
        pay(other_inv)

    def post(self, user, path, data=None):
        self.client.force_login(user)
        return self.client.post(path, json.dumps(data or {}), content_type="application/json")

    def get(self, user, path):
        self.client.force_login(user)
        return self.client.get(path, HTTP_ACCEPT="application/json")

    def test_owner_adds_paid_for_branch_activates_immediately_beyond_it_invoices(self):
        r = self.post(self.owner, "/branches/", {"name": "Karen"})        # slots 2, active 2 -> beyond
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual(body["branch"]["status"], "AWAITING_PAYMENT")
        self.assertTrue(body["invoice"]["is_upgrade"])
        self.assertEqual(body["next"], "/billing/")

    def test_manager_can_list_and_switch_but_not_add(self):
        self.assertEqual(self.get(self.manager, "/branches/").status_code, 200)
        self.assertEqual(self.post(self.manager, "/branches/", {"name": "X"}).status_code, 403)
        r = self.post(self.manager, "/branches/switch/", {"branch_id": self.west.pk})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.session["active_branch_id"], self.west.pk)

    def test_cashier_has_no_branch_screens_and_cannot_switch(self):
        self.assertEqual(self.get(self.cashier, "/branches/").status_code, 403)
        self.assertEqual(self.post(self.cashier, "/branches/switch/", {"branch_id": 1}).status_code, 403)

    def test_cannot_touch_or_switch_to_another_shops_branch(self):
        theirs = Branch.all_objects.get(tenant=self.other)
        self.assertEqual(self.post(self.owner, f"/branches/{theirs.pk}/update/", {"name": "Hack"}).status_code, 404)
        self.assertEqual(self.post(self.owner, "/branches/switch/", {"branch_id": theirs.pk}).status_code, 403)

    def test_update_and_archive(self):
        r = self.post(self.owner, f"/branches/{self.west.pk}/update/", {"name": "Westlands Mall", "code": "wlk"})
        self.assertEqual(r.status_code, 200)
        self.west.refresh_from_db()
        self.assertEqual((self.west.name, self.west.code), ("Westlands Mall", "WLK"))
        self.assertEqual(self.post(self.owner, f"/branches/{self.west.pk}/archive/").status_code, 200)

    def test_staff_endpoint_assigns_cashier_branch(self):
        main = rules.main_branch(self.tenant)
        self.client.force_login(self.owner)
        r = self.client.post(f"/staff/{self.cashier.pk}/set-branch/", {"branch_id": main.pk})
        self.assertEqual(r.status_code, 200, r.content)
        self.cashier.refresh_from_db()
        self.assertEqual(self.cashier.branch_id, main.pk)
        r = self.client.post(f"/staff/{self.cashier.pk}/set-branch/", {"branch_id": ""})
        self.assertEqual(r.status_code, 400)           # cashiers must have a branch

    def test_signup_endpoint_accepts_branches(self):
        self.client.logout()
        r = self.client.post("/signup/", json.dumps({
            **{k: v for k, v in BASE.items()}, "email": "new@example.com", "business_name": "New Shop",
            "branch_count": 2, "branch_names": ["Town", "Mall"]}), content_type="application/json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(Branch.all_objects.filter(tenant__slug__startswith="new-shop").count(), 2)
        r = self.client.post("/signup/", json.dumps({
            **BASE, "email": "dup@example.com", "business_name": "Dup Shop",
            "branch_count": 2, "branch_names": ["Same", "same"]}), content_type="application/json")
        self.assertEqual(r.status_code, 400)
