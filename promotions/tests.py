import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from inventory.models import StockLevel
from products.models import Category
from sales.cart import CartItem
from sales.models import CashRegisterSession, Order, Refund
from sales.tests import SYNC, make_cashier, make_shop, sale
from sales.utils import complete_pos_sale
from tenants.context import tenant_context
from tenants.models import get_settings
from tenants.services import create_staff_user

from .models import Discount
from .pricing import PriceBook

D = Decimal


class Base(TestCase):
    def setUp(self):
        self.shop = make_shop()
        self.t = self.shop["tenant"]
        self.tablet, self.box = self.shop["tablet"], self.shop["box"]      # 5.00 (cost 2) / 120.00 (cost 55)
        self.cashier = make_cashier(self.shop)

    def disc(self, value="10", kind="PERCENT", target="ALL", products=(), **kw):
        with tenant_context(self.t):
            d = Discount.objects.create(name=kw.pop("name", "Promo"), kind=kind, value=D(value), target=target,
                                        starts_at=kw.pop("starts_at", timezone.now() - timedelta(hours=1)), **kw)
            if products:
                d.products.set(products)
            return d

    def book(self, **kw):
        with tenant_context(self.t):
            return PriceBook(tenant=self.t, branch=self.shop["branch"], **kw)

    def price(self, variant, qty=1, **kw):
        with tenant_context(self.t):
            book = PriceBook(tenant=self.t, branch=self.shop["branch"], **kw)
            variant = type(variant).objects.select_related("product", "product__category").get(pk=variant.pk)
            return book.price(variant, qty)

    def set_rules(self, **fields):
        with tenant_context(self.t):
            s = get_settings(self.t)
            for k, v in fields.items():
                setattr(s, k, v)
            s.save()

    def sell(self, lines, received="1000"):
        with tenant_context(self.t):
            cart = [CartItem(v.pk, v.sku, v.sku, v.retail_price, q) for v, q in lines]
            order = complete_pos_sale(cart, "CASH", self.cashier, self.shop["session"], D(received))
            return Order.objects.select_related("receipt").get(pk=order.pk)


class PricingRules(Base):
    def test_percent_and_fixed(self):
        self.disc("10")
        p = self.price(self.tablet, 2)
        self.assertEqual((p.list_price, p.discount_per_unit, p.net_price, p.net_total), (D("5"), D("0.50"), D("4.50"), D("9.00")))
        Discount.all_objects.all().delete()
        self.disc("1.25", kind="FIXED")
        self.assertEqual(self.price(self.tablet).net_price, D("3.75"))

    def test_no_discount_means_untouched_price(self):
        p = self.price(self.tablet)
        self.assertFalse(p.has_discount)
        self.assertEqual(p.net_price, D("5.00"))

    def test_best_single_discount_wins_and_they_do_not_stack(self):
        self.disc("10", name="Ten")
        self.disc("30", name="Thirty")
        p = self.price(self.tablet)
        self.assertEqual((p.discount_name, p.net_price), ("Thirty", D("3.50")))

    def test_specific_products_only(self):
        self.disc("50", target="PRODUCTS", products=[self.tablet.product])
        self.assertTrue(self.price(self.tablet).has_discount)
        with tenant_context(self.t):
            other = Discount.objects.get().products.count()
        self.assertEqual(other, 1)

    def test_category_discount_includes_subcategories(self):
        with tenant_context(self.t):
            parent = self.tablet.product.category
            child = Category.objects.create(name="Kids", slug="kids", parent=parent)
            self.tablet.product.category = child
            self.tablet.product.save()
            d = Discount.objects.create(name="Cat", kind="PERCENT", value=D("20"), target="CATEGORIES",
                                        starts_at=timezone.now() - timedelta(hours=1))
            d.categories.set([parent])
        self.assertEqual(self.price(self.tablet).discount_per_unit, D("1.00"))

    def test_minimum_quantity(self):
        self.disc("10", min_quantity=3)
        self.assertFalse(self.price(self.tablet, 2).has_discount)
        self.assertTrue(self.price(self.tablet, 3).has_discount)

    def test_schedule_and_switch(self):
        self.disc("10", name="Future", starts_at=timezone.now() + timedelta(days=1))
        self.disc("10", name="Past", ends_at=timezone.now() - timedelta(minutes=1))
        self.disc("10", name="Off", is_active=False)
        self.assertFalse(self.price(self.tablet).has_discount)
        # the same "future" discount is live when asked about a moment inside its window
        self.assertTrue(self.price(self.tablet, at=timezone.now() + timedelta(days=2)).has_discount)

    def test_branch_restriction(self):
        with tenant_context(self.t):
            from inventory.models import Branch
            other = Branch.objects.create(name="Westlands", code="WLK")
        d = self.disc("10")
        d.branches.set([other])
        self.assertFalse(self.price(self.tablet).has_discount)          # till is at the main branch
        d.branches.set([self.shop["branch"]])
        self.assertTrue(self.price(self.tablet).has_discount)

    def test_master_switch_turns_everything_off(self):
        self.disc("10")
        self.set_rules(discounts_enabled=False)
        self.assertFalse(self.price(self.tablet).has_discount)

    def test_shop_ceiling_applies_even_to_old_discounts(self):
        self.disc("80")
        self.set_rules(max_discount_percent=D("25"))
        self.assertEqual(self.price(self.tablet).discount_per_unit, D("1.25"))

    def test_never_below_cost_by_default_but_can_be_allowed(self):
        self.set_rules(max_discount_percent=D("100"))
        self.disc("90")                                                  # 5.00 -> 0.50, but cost is 2.00
        self.assertEqual(self.price(self.tablet).net_price, D("2.00"))
        self.set_rules(below_cost_policy="ALLOW")
        self.assertEqual(self.price(self.tablet).net_price, D("0.50"))

    def test_rounding_keeps_line_total_exact(self):
        self.disc("33.33")
        p = self.price(self.tablet, 7)
        self.assertEqual(p.net_total, p.net_price * 7)
        self.assertEqual(p.net_price, p.list_price - p.discount_per_unit)


class CheckoutAndRefunds(Base):
    def test_sale_records_discount_everywhere(self):
        self.disc("20", name="Weekend")
        order = self.sell([(self.tablet, 10)])                           # 50.00 list, 10.00 off
        self.assertEqual((order.subtotal, order.discount_total, order.total_revenue), (D("50.00"), D("10.00"), D("40.00")))
        self.assertEqual(order.total_profit, D("20.00"))                 # 40 - cost 20
        with tenant_context(self.t):
            item = order.items.get()
            self.assertEqual((item.retail_price, item.discount_per_unit, item.revenue_line), (D("5.00"), D("1.00"), D("40.00")))
            self.assertEqual(item.discount_name, "Weekend")
            r = order.receipt
            self.assertEqual((r.subtotal, r.discount_total, r.total), (D("50.00"), D("10.00"), D("40.00")))
            self.assertEqual(r.lines[0]["discount"], "10.00")
        self.assertEqual(order.change_given, D("960.00"))

    def test_cash_register_counts_what_was_actually_paid(self):
        self.disc("20")
        before = D("1000")
        self.sell([(self.tablet, 10)])
        with tenant_context(self.t):
            exp = CashRegisterSession.objects.get(pk=self.shop["session"].pk).get_current_expected_cash()
        self.assertEqual(exp, before + D("40.00"))

    def test_discount_edits_and_deletes_never_rewrite_history(self):
        d = self.disc("20", name="Weekend")
        order = self.sell([(self.tablet, 10)])
        with tenant_context(self.t):
            d.delete()
            item = order.items.get()
            self.assertIsNone(item.discount)
            self.assertEqual((item.discount_name, item.revenue_line), ("Weekend", D("40.00")))

    def test_amount_received_must_cover_the_discounted_total(self):
        self.disc("20")
        self.sell([(self.tablet, 10)], received="40")                    # exactly the net total is enough
        with self.assertRaises(ValueError):
            self.sell([(self.tablet, 10)], received="39")

    def test_refund_returns_what_was_paid_not_list_price(self):
        self.disc("20")
        order = self.sell([(self.tablet, 10)])                           # paid 40.00
        self.client.force_login(self.shop["owner"])
        item = order.items.first() if False else None
        with tenant_context(self.t):
            item = order.items.get()
        r = self.client.post(f"/pos/refunds/new/{order.receipt.pk}/",
                             {f"qty_{item.pk}": "4", f"restock_{item.pk}": "on", "reason": "CHANGED_MIND",
                              "notes": "", "client_uuid": str(uuid.uuid4())})
        self.assertEqual(r.status_code, 302)
        r = self.client.post(f"/pos/refunds/new/{order.receipt.pk}/",
                             {f"qty_{item.pk}": "6", f"restock_{item.pk}": "on", "reason": "CHANGED_MIND",
                              "notes": "", "client_uuid": str(uuid.uuid4())})
        self.assertEqual(r.status_code, 302)
        with tenant_context(self.t):
            order = Order.objects.get(pk=order.pk)
            self.assertEqual(sum(x.total_amount for x in Refund.objects.all()), D("40.00"))
            self.assertEqual(order.refund_status, "FULL")
            self.assertEqual(order.net_revenue, D("0.00"))


class OfflineSync(Base):
    def post(self, body):
        self.client.force_login(self.cashier)
        return self.client.post(SYNC, data=json.dumps({"sales": [body]}), content_type="application/json",
                                HTTP_ACCEPT="application/json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_snapshot_carries_discounted_price(self):
        self.disc("20", name="Weekend")
        self.client.force_login(self.cashier)
        data = self.client.get("/pos/offline/snapshot/", HTTP_ACCEPT="application/json").json()
        v = next(x for x in data["variants"] if x["sku"] == "PAN-1")
        self.assertEqual((v["price"], v["was"], v["promo"]), ("4.00", "5.00", "Weekend"))
        self.assertNotIn("cost", json.dumps(data))

    def test_offline_sale_at_discounted_price_is_recorded_as_a_discount(self):
        self.disc("20")
        res = self.post(sale(self.shop, lines=[{"variant_id": self.tablet.pk, "quantity": 5, "price": "4.00"}]))
        r = res.json()["results"][0]
        self.assertEqual(r["status"], "created", r)
        self.assertEqual(r["flags"], [])
        with tenant_context(self.t):
            o = Order.objects.get(client_uuid=r["uuid"])
            self.assertEqual((o.subtotal, o.discount_total, o.total_revenue), (D("25.00"), D("5.00"), D("20.00")))

    def test_offline_unexplained_price_is_kept_as_paid_and_flagged(self):
        res = self.post(sale(self.shop, lines=[{"variant_id": self.tablet.pk, "quantity": 2, "price": "4.00"}]))
        r = res.json()["results"][0]
        self.assertEqual(r["status"], "created")
        self.assertEqual([f["type"] for f in r["flags"]], ["PRICE_MISMATCH"])
        with tenant_context(self.t):
            self.assertEqual(Order.objects.get(client_uuid=r["uuid"]).total_revenue, D("8.00"))

    def test_discount_valid_at_sale_time_still_counts_after_it_ended(self):
        d = self.disc("20", ends_at=timezone.now() - timedelta(minutes=5))   # ended 5 min ago; sale was 20 min ago
        res = self.post(sale(self.shop, lines=[{"variant_id": self.tablet.pk, "quantity": 5, "price": "4.00"}]))
        r = res.json()["results"][0]
        self.assertEqual((r["status"], r["flags"]), ("created", []))


class Screens(Base):
    def manager(self):
        return create_staff_user(tenant=self.t, handle="moses", role="MANAGER", password="S3cure-pass-99")

    def form(self, **over):
        data = {"name": "Promo", "kind": "PERCENT", "value": "10", "target": "ALL", "min_quantity": "1",
                "starts_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"), "ends_at": "", "is_active": "on", "note": ""}
        data.update(over)
        return data

    def test_cashier_cannot_open_discounts_or_settings(self):
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get("/discounts/").status_code, 403)
        self.assertEqual(self.client.get("/discounts/new/").status_code, 403)
        self.assertEqual(self.client.get("/settings/").status_code, 403)

    def test_manager_can_create_within_their_limit_and_admins_are_told(self):
        m = self.manager()
        self.client.force_login(m)
        r = self.client.post("/discounts/new/", self.form(value="15"))
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        with tenant_context(self.t):
            d = Discount.objects.get()
            self.assertEqual(d.created_by_id, m.pk)
            from notifications.models import Notification
            self.assertTrue(Notification.objects.filter(title="Discount created").exists())

    def test_manager_over_their_limit_is_refused_but_admin_is_not(self):
        self.client.force_login(self.manager())
        r = self.client.post("/discounts/new/", self.form(value="30"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("value", r.context["form"].errors)
        self.client.force_login(self.shop["owner"])
        self.assertEqual(self.client.post("/discounts/new/", self.form(value="30")).status_code, 302)
        self.assertEqual(self.client.post("/discounts/new/", self.form(value="60", name="Too big")).status_code, 200)

    def test_fixed_amount_is_judged_against_the_cheapest_product(self):
        self.client.force_login(self.shop["owner"])
        r = self.client.post("/discounts/new/", self.form(kind="FIXED", value="4"))      # 4 off a 5.00 tablet = 80%
        self.assertEqual(r.status_code, 200)
        self.assertIn("value", r.context["form"].errors)

    def test_admin_can_stop_managers_managing_discounts(self):
        self.set_rules(managers_can_manage_discounts=False)
        self.client.force_login(self.manager())
        self.assertEqual(self.client.get("/discounts/").status_code, 200)               # may look
        self.assertEqual(self.client.get("/discounts/new/").status_code, 403)           # may not change

    def test_toggle_and_delete(self):
        d = self.disc("10")
        self.client.force_login(self.shop["owner"])
        self.client.post(f"/discounts/{d.pk}/toggle/")
        with tenant_context(self.t):
            self.assertFalse(Discount.objects.get(pk=d.pk).is_active)
        self.assertEqual(self.client.post(f"/discounts/{d.pk}/delete/").status_code, 302)
        with tenant_context(self.t):
            self.assertEqual(Discount.objects.count(), 0)

    def test_one_shop_cannot_see_anothers_discounts(self):
        self.disc("10", name="Mine")
        other = make_shop(business_name="Other Shop", email="o@example.com", phone="254700000001")
        self.client.force_login(other["owner"])
        self.assertNotContains(self.client.get("/discounts/"), "Mine")
        self.assertEqual(self.client.post(f"/discounts/{Discount.all_objects.get().pk}/toggle/").status_code, 404)

    def test_settings_admin_saves_and_it_is_audited(self):
        self.client.force_login(self.shop["owner"])
        r = self.client.post("/settings/", {"discounts_enabled": "on", "managers_can_manage_discounts": "on",
                                            "max_discount_percent": "40", "manager_max_discount_percent": "10",
                                            "below_cost_policy": "LIMIT", "refund_window_days": "14",
                                            "receipt_footer": "No returns after 14 days", "offline_max_hours": "48"})
        self.assertEqual(r.status_code, 302)
        with tenant_context(self.t):
            s = get_settings(self.t)
            self.assertEqual((s.max_discount_percent, s.refund_window_days, s.offline_max_hours), (D("40"), 14, 48))
            from users.models import ActivityLog
            self.assertTrue(ActivityLog.objects.filter(action="SETTINGS_CHANGED").exists())

    def test_settings_rejects_manager_limit_above_shop_limit_and_manager_cannot_save(self):
        self.client.force_login(self.shop["owner"])
        bad = {"discounts_enabled": "on", "max_discount_percent": "10", "manager_max_discount_percent": "20",
               "below_cost_policy": "LIMIT", "refund_window_days": "30", "offline_max_hours": "72"}
        self.assertEqual(self.client.post("/settings/", bad).status_code, 400)
        self.client.force_login(self.manager())
        self.assertEqual(self.client.get("/settings/").status_code, 200)
        self.assertEqual(self.client.post("/settings/", bad).status_code, 403)

    def test_refund_window_and_receipt_footer_follow_settings(self):
        self.set_rules(receipt_footer="Thanks!", refund_window_days=5)
        order = self.sell([(self.tablet, 1)])
        with tenant_context(self.t):
            self.assertEqual(order.receipt.footer, "Thanks!")
            from sales.refunds import refund_window_days
            self.assertEqual(refund_window_days(self.t), 5)


class TillFlow(Base):
    """The real HTTP path a cashier takes: add to cart -> pay."""

    def test_cart_pay_and_receipt_page_show_the_discount(self):
        self.disc("20", name="Weekend")
        self.client.force_login(self.cashier)
        r = self.client.post("/pos/cart/add/", {"variant_id": self.tablet.pk}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 200)
        self.client.post("/pos/cart/update/", {"variant_id": self.tablet.pk, "quantity": 10}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        html = self.client.post("/pos/cart/update/", {"variant_id": self.tablet.pk, "quantity": 10},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["cart_html"]
        self.assertIn("Weekend", html)
        self.assertIn("KSH 40.00", html)
        pay = self.client.get("/payments/pos/checkout/pay/")
        self.assertContains(pay, "KSH 40.00")
        done = self.client.post("/payments/pos/checkout/process/",
                                {"amount_received": "50", "client_uuid": str(uuid.uuid4())},
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(done.json()["ok"], done.json())
        page = self.client.get(done.json()["redirect"])
        self.assertContains(page, "Weekend")
        self.assertContains(page, "You saved KSH 10.00")

    def test_terminal_tiles_show_was_and_now(self):
        self.disc("20", name="Weekend")
        self.client.force_login(self.cashier)
        page = self.client.get("/pos/terminal/")
        self.assertContains(page, "Weekend")
        self.assertContains(page, "KSH 4.00")
