import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from billing.models import Subscription
from inventory.models import Branch, StockLevel
from notifications.models import Notification
from products.models import Category, Product, ProductVariant, UnitOfMeasure
from tenants.context import tenant_context
from tenants.services import create_staff_user, register_business

from .models import CashRegisterSession, Order

User = get_user_model()

SIGNUP = dict(business_name="Mama Njeri Mart", business_type="MINI_MART", owner_name="Njeri W",
              email="njeri@example.com", phone="254712345678", password="S3cure-pass-99", term_months=3)

SNAPSHOT = "/pos/offline/snapshot/"
SYNC = "/pos/offline/sync/"


def make_shop(**over):
    """A tenant with a branch, an open register, one product (stock 100) and a 30-pack selling unit."""
    tenant, owner, _ = register_business(**{**SIGNUP, **over})
    Subscription.objects.filter(tenant=tenant).update(comp_until=timezone.now() + timedelta(days=30))
    with tenant_context(tenant):
        branch = Branch.objects.first()
        cat = Category.objects.create(name="Pain", slug="pain")
        uom = UnitOfMeasure.objects.create(name="Tablet", short_name="tab")
        prod = Product.objects.create(name="Panadol", sku_prefix="PAN", category=cat, unit_of_measure=uom)
        tablet = ProductVariant.objects.create(product=prod, sku="PAN-1", cost_price=Decimal("2"),
                                               retail_price=Decimal("5"))
        box = ProductVariant.objects.create(product=prod, sku="PAN-BOX", cost_price=Decimal("55"),
                                            retail_price=Decimal("120"), unit_name="Box",
                                            base_variant=tablet, units_per_pack=Decimal("30"))
        StockLevel.objects.create(branch=branch, variant=tablet, quantity=Decimal("100"))
        session = CashRegisterSession.objects.create(branch=branch, opened_by=owner,
                                                     opening_balance=Decimal("1000"))
    return dict(tenant=tenant, owner=owner, branch=branch, tablet=tablet, box=box, session=session)


def make_cashier(shop, handle="amina"):
    return create_staff_user(tenant=shop["tenant"], handle=handle, role="CASHIER", pin="1234",
                             password="S3cure-pass-99")


def sale(shop, lines=None, **over):
    now = timezone.now()
    body = {
        "uuid": str(uuid.uuid4()),
        "invoice": f"INV-{now:%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        "occurred_at": (now - timedelta(minutes=20)).isoformat(),
        "session_id": shop["session"].pk,
        "cashier_id": None,
        "amount_received": "500.00",
        "lines": lines or [{"variant_id": shop["tablet"].pk, "quantity": 4, "price": "5.00"}],
    }
    body.update(over)
    return body


class OfflineTestBase(TestCase):
    def setUp(self):
        self.shop = make_shop()
        self.cashier = make_cashier(self.shop)
        self.client.force_login(self.cashier)

    def post_sync(self, *sales, client=None):
        return (client or self.client).post(
            SYNC, data=json.dumps({"sales": list(sales)}), content_type="application/json",
            HTTP_ACCEPT="application/json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def stock(self):
        with tenant_context(self.shop["tenant"]):
            return StockLevel.objects.get(branch=self.shop["branch"], variant=self.shop["tablet"]).quantity


class SnapshotTests(OfflineTestBase):
    def test_requires_login_and_answers_json(self):
        self.client.logout()
        r = self.client.get(SNAPSHOT, HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"], "auth_required")

    def test_snapshot_contents(self):
        r = self.client.get(SNAPSHOT, HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["user"]["id"], self.cashier.pk)
        self.assertEqual(data["session"]["id"], self.shop["session"].pk)
        self.assertEqual({v["sku"] for v in data["variants"]}, {"PAN-1", "PAN-BOX"})
        box = next(v for v in data["variants"] if v["sku"] == "PAN-BOX")
        self.assertEqual(box["base"], self.shop["tablet"].pk)
        self.assertEqual(Decimal(box["per_pack"]), 30)
        self.assertEqual(Decimal(data["stock"][str(self.shop["tablet"].pk)]), 100)

    def test_snapshot_never_leaks_cost_price(self):
        raw = self.client.get(SNAPSHOT, HTTP_ACCEPT="application/json").content.decode()
        self.assertNotIn("cost", raw)
        self.assertNotIn('"55.00"', raw)

    def test_etag_gives_304_when_nothing_changed(self):
        first = self.client.get(SNAPSHOT, HTTP_ACCEPT="application/json")
        again = self.client.get(SNAPSHOT, HTTP_ACCEPT="application/json", HTTP_IF_NONE_MATCH=first["ETag"])
        self.assertEqual(again.status_code, 304)

    def test_etag_changes_when_stock_changes(self):
        first = self.client.get(SNAPSHOT, HTTP_ACCEPT="application/json")
        with tenant_context(self.shop["tenant"]):
            StockLevel.objects.filter(variant=self.shop["tablet"]).update(quantity=Decimal("99"))
        again = self.client.get(SNAPSHOT, HTTP_ACCEPT="application/json", HTTP_IF_NONE_MATCH=first["ETag"])
        self.assertEqual(again.status_code, 200)

    def test_other_tenants_data_is_not_in_the_snapshot(self):
        other = make_shop(business_name="Kamau Pharmacy", email="kamau@example.com")
        data = self.client.get(SNAPSHOT, HTTP_ACCEPT="application/json").json()
        ids = {v["id"] for v in data["variants"]}
        self.assertNotIn(other["tablet"].pk, ids)
        self.assertNotIn(other["box"].pk, ids)


class SyncTests(OfflineTestBase):
    def test_sale_is_recorded_with_real_time_and_invoice(self):
        s = sale(self.shop)
        r = self.post_sync(s)
        self.assertEqual(r.status_code, 200)
        result = r.json()["results"][0]
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["invoice"], s["invoice"])
        self.assertIsNotNone(result["receipt_number"])

        with tenant_context(self.shop["tenant"]):
            order = Order.objects.get(client_uuid=s["uuid"])
            self.assertTrue(order.is_offline_sale)
            self.assertEqual(order.cashier_id, self.cashier.pk)
            self.assertEqual(order.total_revenue, Decimal("20.00"))
            self.assertEqual(order.total_cogs, Decimal("8.00"))      # cost comes from the DB
            self.assertEqual(order.change_given, Decimal("480.00"))
            self.assertEqual(order.cash_session_id, self.shop["session"].pk)
            # stamped with when it happened, not when it synced
            self.assertLess(timezone.now() - order.created_at, timedelta(minutes=25))
            self.assertGreater(timezone.now() - order.created_at, timedelta(minutes=15))
            self.assertEqual(order.receipt.issued_at, order.created_at)
            self.assertEqual(order.offline_flags, [])
        self.assertEqual(self.stock(), Decimal("96"))

    def test_resending_the_same_sale_is_harmless(self):
        s = sale(self.shop)
        self.assertEqual(self.post_sync(s).json()["results"][0]["status"], "created")
        again = self.post_sync(s).json()["results"][0]
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["invoice"], s["invoice"])
        with tenant_context(self.shop["tenant"]):
            self.assertEqual(Order.objects.filter(client_uuid=s["uuid"]).count(), 1)
        self.assertEqual(self.stock(), Decimal("96"))               # deducted once

    def test_same_sale_twice_in_one_request(self):
        s = sale(self.shop)
        results = self.post_sync(s, s).json()["results"]
        self.assertEqual([r["status"] for r in results], ["created", "duplicate"])
        self.assertEqual(self.stock(), Decimal("96"))

    def test_selling_a_box_draws_on_the_shared_pool(self):
        s = sale(self.shop, lines=[{"variant_id": self.shop["box"].pk, "quantity": 2, "price": "120.00"}])
        self.assertEqual(self.post_sync(s).json()["results"][0]["status"], "created")
        self.assertEqual(self.stock(), Decimal("40"))               # 100 - 2*30

    def test_oversold_sale_is_recorded_flagged_and_managers_told(self):
        # Another till sold the stock while this one was offline.
        with tenant_context(self.shop["tenant"]):
            StockLevel.objects.filter(variant=self.shop["tablet"]).update(quantity=Decimal("2"))
        s = sale(self.shop)                                          # sells 4
        result = self.post_sync(s).json()["results"][0]
        self.assertEqual(result["status"], "created")                # the goods are gone: record it
        self.assertIn("NEGATIVE_STOCK", [f["type"] for f in result["flags"]])
        self.assertEqual(self.stock(), Decimal("-2"))
        with tenant_context(self.shop["tenant"]):
            self.assertTrue(Notification.objects.filter(title="Offline sales need a look").exists())

    def test_price_change_is_flagged_and_the_customers_price_is_kept(self):
        s = sale(self.shop, lines=[{"variant_id": self.shop["tablet"].pk, "quantity": 4, "price": "4.00"}],
                 amount_received="100")
        result = self.post_sync(s).json()["results"][0]
        self.assertIn("PRICE_MISMATCH", [f["type"] for f in result["flags"]])
        with tenant_context(self.shop["tenant"]):
            # what was actually taken at the till, so the register still balances
            self.assertEqual(Order.objects.get(client_uuid=s["uuid"]).total_revenue, Decimal("16.00"))

    def test_taken_invoice_number_is_replaced_and_flagged(self):
        a, b = sale(self.shop), sale(self.shop)
        b["invoice"] = a["invoice"]
        results = self.post_sync(a, b).json()["results"]
        self.assertEqual(results[0]["invoice"], a["invoice"])
        self.assertNotEqual(results[1]["invoice"], a["invoice"])
        self.assertIn("INVOICE_RENUMBERED", [f["type"] for f in results[1]["flags"]])

    def test_short_payment_is_rejected(self):
        s = sale(self.shop, amount_received="10")
        r = self.post_sync(s).json()["results"][0]
        self.assertEqual((r["status"], r["code"]), ("rejected", "short_payment"))
        self.assertEqual(self.stock(), Decimal("100"))

    def test_unknown_product_is_rejected_and_nothing_changes(self):
        s = sale(self.shop, lines=[{"variant_id": 999999, "quantity": 1, "price": "5"}])
        r = self.post_sync(s).json()["results"][0]
        self.assertEqual((r["status"], r["code"]), ("rejected", "unknown_product"))

    def test_another_tenants_product_is_rejected(self):
        other = make_shop(business_name="Kamau Pharmacy", email="kamau@example.com")
        s = sale(self.shop, lines=[{"variant_id": other["tablet"].pk, "quantity": 1, "price": "5"}])
        r = self.post_sync(s).json()["results"][0]
        self.assertEqual((r["status"], r["code"]), ("rejected", "unknown_product"))
        with tenant_context(other["tenant"]):
            self.assertEqual(StockLevel.objects.get(variant=other["tablet"]).quantity, Decimal("100"))

    def test_rejected_sale_does_not_block_the_next_one(self):
        bad = sale(self.shop, amount_received="1")
        good = sale(self.shop)
        results = self.post_sync(bad, good).json()["results"]
        self.assertEqual([r["status"] for r in results], ["rejected", "created"])

    def test_no_open_register_is_rejected_then_succeeds_after_opening(self):
        with tenant_context(self.shop["tenant"]):
            CashRegisterSession.objects.filter(pk=self.shop["session"].pk).update(
                status="CLOSED", closing_balance=Decimal("1000"), closed_at=timezone.now())
        s = sale(self.shop, session_id=None)
        r = self.post_sync(s).json()["results"][0]
        self.assertEqual((r["status"], r["code"]), ("rejected", "no_register"))
        with tenant_context(self.shop["tenant"]):
            CashRegisterSession.objects.create(branch=self.shop["branch"], opened_by=self.shop["owner"],
                                               opening_balance=Decimal("1000"))
        self.assertEqual(self.post_sync(s).json()["results"][0]["status"], "created")

    def test_late_sync_into_a_closed_register_updates_its_expected_cash(self):
        with tenant_context(self.shop["tenant"]):
            session = CashRegisterSession.objects.get(pk=self.shop["session"].pk)
            session.closing_balance = Decimal("1020")                # counted: includes the offline sale
            session.status = "CLOSED"
            session.closed_by = self.shop["owner"]
            session.closed_at = timezone.now()
            session.save()
            self.assertEqual(session.discrepancy, Decimal("20"))     # looks like 20 over...
        s = sale(self.shop)                                          # ...until the 20 sale arrives
        r = self.post_sync(s).json()["results"][0]
        self.assertIn("LATE_SYNC_CLOSED_REGISTER", [f["type"] for f in r["flags"]])
        with tenant_context(self.shop["tenant"]):
            session = CashRegisterSession.objects.get(pk=self.shop["session"].pk)
            self.assertEqual(session.expected_closing_balance, Decimal("1020.00"))
            self.assertEqual(session.discrepancy, Decimal("0.00"))

    def test_absurd_client_clock_is_not_believed(self):
        s = sale(self.shop, occurred_at=(timezone.now() + timedelta(days=400)).isoformat())
        r = self.post_sync(s).json()["results"][0]
        self.assertIn("CLOCK_SKEW", [f["type"] for f in r["flags"]])
        with tenant_context(self.shop["tenant"]):
            order = Order.objects.get(client_uuid=s["uuid"])
            self.assertLess(abs(timezone.now() - order.created_at), timedelta(minutes=1))

    def test_a_cashier_cannot_submit_someone_elses_sale(self):
        other = make_cashier(self.shop, handle="brian")
        s = sale(self.shop, cashier_id=other.pk)
        r = self.post_sync(s).json()["results"][0]
        self.assertEqual((r["status"], r["code"]), ("rejected", "wrong_cashier"))

    def test_a_manager_can_deliver_a_cashiers_sale(self):
        self.client.force_login(self.shop["owner"])
        s = sale(self.shop, cashier_id=self.cashier.pk)
        self.assertEqual(self.post_sync(s).json()["results"][0]["status"], "created")
        with tenant_context(self.shop["tenant"]):
            self.assertEqual(Order.objects.get(client_uuid=s["uuid"]).cashier_id, self.cashier.pk)

    def test_unauthenticated_sync_is_401_json(self):
        self.client.logout()
        self.assertEqual(self.post_sync(sale(self.shop)).status_code, 401)

    def test_garbage_is_rejected_cleanly(self):
        r = self.client.post(SYNC, data="{nope", content_type="application/json")
        self.assertEqual(r.status_code, 400)
        r = self.post_sync({"uuid": "not-a-uuid"})
        self.assertEqual(r.json()["results"][0]["code"], "bad_uuid")
        r = self.post_sync(sale(self.shop, lines=[{"variant_id": self.shop["tablet"].pk, "quantity": -3, "price": "5"}]))
        self.assertEqual(r.json()["results"][0]["status"], "rejected")

    def test_leftover_server_cart_is_cleared_only_when_it_is_the_cart_that_sold(self):
        key = str(self.shop["tablet"].pk)
        line = {"variant_id": self.shop["tablet"].pk, "name": "Panadol", "sku": "PAN-1", "price": "5.00"}

        session = self.client.session
        session["pos_cart"] = {key: {**line, "quantity": 4}}
        session.save()
        self.post_sync(sale(self.shop, from_online_cart=True))
        self.assertEqual(self.client.session.get("pos_cart"), {})

        session = self.client.session                                # a different cart is left alone
        session["pos_cart"] = {key: {**line, "quantity": 9}}
        session.save()
        self.post_sync(sale(self.shop, from_online_cart=True))
        self.assertEqual(self.client.session["pos_cart"][key]["quantity"], 9)


class OnlineCheckoutIdempotencyTests(OfflineTestBase):
    def checkout(self, checkout_uuid):
        return self.client.post("/payments/pos/checkout/process/",
                                {"amount_received": "100", "client_uuid": str(checkout_uuid)},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def fill_cart(self):
        r = self.client.post("/pos/cart/add/", {"variant_id": self.shop["tablet"].pk},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 200, r.content)

    def test_retrying_a_checkout_does_not_sell_twice(self):
        self.fill_cart()
        u = uuid.uuid4()
        first = self.checkout(u)
        self.assertTrue(first.json()["ok"])
        second = self.checkout(u)            # the response was lost; the till retries
        self.assertTrue(second.json()["ok"])
        self.assertEqual(first.json()["redirect"], second.json()["redirect"])
        with tenant_context(self.shop["tenant"]):
            self.assertEqual(Order.objects.filter(client_uuid=u).count(), 1)
        self.assertEqual(self.stock(), Decimal("99"))

    def test_checkout_that_succeeded_online_then_arrives_via_the_offline_queue_is_a_duplicate(self):
        self.fill_cart()
        u = uuid.uuid4()
        self.checkout(u)
        replay = sale(self.shop, uuid=str(u), lines=[{"variant_id": self.shop["tablet"].pk, "quantity": 1, "price": "5"}])
        self.assertEqual(self.post_sync(replay).json()["results"][0]["status"], "duplicate")
        self.assertEqual(self.stock(), Decimal("99"))

    def test_normal_form_post_still_redirects(self):
        self.fill_cart()
        r = self.client.post("/payments/pos/checkout/process/", {"amount_received": "100"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/pos/receipts/", r["Location"])

    def test_validation_errors_are_json_for_ajax(self):
        self.fill_cart()
        r = self.client.post("/payments/pos/checkout/process/", {"amount_received": "1"},
                             HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.json()["ok"])


class ShellTests(TestCase):
    def test_shell_is_public_and_carries_no_user_data(self):
        r = self.client.get("/pos/offline/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-cache")
        self.assertNotIn("csrfmiddlewaretoken", r.content.decode())
