import uuid
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from inventory.models import StockLevel
from tenants.context import tenant_context

from .cart import CartItem
from .models import CashRegisterSession, Order, Refund
from .tests import make_cashier, make_shop
from .utils import complete_pos_sale

FORM = "/pos/refunds/new/{}/"


class RefundBase(TestCase):
    def setUp(self):
        self.shop = make_shop()
        self.admin = self.shop["owner"]
        self.cashier = make_cashier(self.shop)
        self.client.force_login(self.admin)

    def sell(self, lines=None, age_days=0):
        """Ring up a real sale (default: 5 tablets @ 5.00 = 25.00; cost 2.00 each)."""
        s = self.shop
        lines = lines or [(s["tablet"], 5)]
        with tenant_context(s["tenant"]):
            cart = [CartItem(v.pk, v.sku, v.sku, v.retail_price, q) for v, q in lines]
            order = complete_pos_sale(cart, "CASH", self.cashier, s["session"], Decimal("1000"))
            if age_days:
                Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(days=age_days))
            return Order.objects.select_related("receipt").get(pk=order.pk)

    def stock(self):
        with tenant_context(self.shop["tenant"]):
            return StockLevel.objects.get(branch=self.shop["branch"], variant=self.shop["tablet"]).quantity

    def expected_cash(self):
        with tenant_context(self.shop["tenant"]):
            return CashRegisterSession.objects.get(pk=self.shop["session"].pk).get_current_expected_cash()

    def refund(self, order, qty, restock=True, reason="CHANGED_MIND", item=None, client_uuid=None, **extra):
        with tenant_context(self.shop["tenant"]):
            item = item or order.items.first()
        data = {f"qty_{item.pk}": str(qty), "reason": reason, "notes": "", "client_uuid": str(client_uuid or uuid.uuid4())}
        if restock:
            data[f"restock_{item.pk}"] = "on"
        data.update(extra)
        return self.client.post(FORM.format(order.receipt.pk), data)


class RefundRules(RefundBase):
    def test_full_refund_returns_stock_pays_cash_and_leaves_the_sale_alone(self):
        order = self.sell()
        self.assertEqual(self.stock(), Decimal("95"))
        before = self.expected_cash()
        r = self.refund(order, 5)
        self.assertEqual(r.status_code, 302)
        with tenant_context(self.shop["tenant"]):
            rf = Refund.objects.get()
            self.assertEqual(rf.total_amount, Decimal("25.00"))
            self.assertEqual((rf.cogs_reversed, rf.profit_reversed), (Decimal("10.00"), Decimal("15.00")))
            self.assertTrue(rf.refund_number.startswith("RFD-"))
            self.assertEqual(rf.cash_session_id, self.shop["session"].pk)
            fresh = Order.objects.get(pk=order.pk)
            self.assertEqual(fresh.total_revenue, Decimal("25.00"))       # the sale is untouched
            self.assertEqual(fresh.refund_status, "FULL")
            self.assertEqual(fresh.net_revenue, Decimal("0.00"))
        self.assertEqual(self.stock(), Decimal("100"))
        self.assertEqual(self.expected_cash(), before - Decimal("25.00"))

    def test_partial_refunds_add_up_but_never_exceed_what_was_sold(self):
        order = self.sell()                                                 # 5 sold
        self.assertEqual(self.refund(order, 2).status_code, 302)
        self.assertEqual(self.refund(order, 3).status_code, 302)
        self.assertEqual(self.refund(order, 1).status_code, 400)            # nothing left
        with tenant_context(self.shop["tenant"]):
            self.assertEqual(Refund.objects.count(), 2)
            self.assertEqual(Order.objects.get(pk=order.pk).refunded_amount, Decimal("25.00"))
        self.assertEqual(self.stock(), Decimal("100"))

    def test_cannot_refund_more_than_remains_on_a_line(self):
        order = self.sell()
        self.refund(order, 4)
        r = self.refund(order, 2)                                           # only 1 left
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "only 1 can still be refunded", status_code=400)
        self.assertEqual(self.stock(), Decimal("99"))

    def test_write_off_keeps_stock_and_books_the_whole_refund_as_lost_profit(self):
        order = self.sell()
        self.refund(order, 2, restock=False, reason="EXPIRED")
        with tenant_context(self.shop["tenant"]):
            rf = Refund.objects.get()
            self.assertEqual((rf.total_amount, rf.cogs_reversed, rf.profit_reversed),
                             (Decimal("10.00"), Decimal("0.00"), Decimal("10.00")))
            self.assertFalse(rf.items.get().restocked)
        self.assertEqual(self.stock(), Decimal("95"))                       # goods not back on the shelf

    def test_returned_box_goes_back_into_the_shared_pool_as_tablets(self):
        order = self.sell([(self.shop["box"], 2)])                          # 2 boxes x 30 tablets
        self.assertEqual(self.stock(), Decimal("40"))
        with tenant_context(self.shop["tenant"]):
            item = order.items.get()
        self.refund(order, 1, item=item)
        self.assertEqual(self.stock(), Decimal("70"))
        with tenant_context(self.shop["tenant"]):
            self.assertEqual(Refund.objects.get().total_amount, Decimal("120.00"))

    def test_needs_a_reason_and_at_least_one_item(self):
        order = self.sell()
        self.assertEqual(self.refund(order, 1, reason="").status_code, 400)
        self.assertEqual(self.refund(order, 0).status_code, 400)
        with tenant_context(self.shop["tenant"]):
            self.assertEqual(Refund.objects.count(), 0)

    def test_needs_an_open_register(self):
        order = self.sell()
        with tenant_context(self.shop["tenant"]):
            CashRegisterSession.objects.filter(pk=self.shop["session"].pk).update(
                status="CLOSED", closing_balance=Decimal("1000"), closed_at=timezone.now())
        r = self.refund(order, 1)
        self.assertContains(r, "no open cash register", status_code=400)
        self.assertEqual(self.stock(), Decimal("95"))

    def test_refund_today_comes_out_of_todays_register_not_the_sales_register(self):
        order = self.sell()
        with tenant_context(self.shop["tenant"]):
            old = CashRegisterSession.objects.get(pk=self.shop["session"].pk)
            old.closing_balance = old.get_current_expected_cash(); old.status = "CLOSED"
            old.closed_by = self.admin; old.closed_at = timezone.now(); old.save()
            new = CashRegisterSession.objects.create(branch=self.shop["branch"], opened_by=self.admin,
                                                     opening_balance=old.closing_balance)
        self.refund(order, 2)
        with tenant_context(self.shop["tenant"]):
            self.assertEqual(Refund.objects.get().cash_session_id, new.pk)
            self.assertEqual(CashRegisterSession.objects.get(pk=old.pk).discrepancy, Decimal("0.00"))
            self.assertEqual(new.get_current_expected_cash(), old.closing_balance - Decimal("10.00"))

    def test_cannot_pay_out_more_cash_than_the_register_holds(self):
        order = self.sell()
        with tenant_context(self.shop["tenant"]):
            from .models import CashTransaction
            s = CashRegisterSession.objects.get(pk=self.shop["session"].pk)
            CashTransaction.objects.create(session=s, user=self.admin, transaction_type="CASH_OUT",
                                           amount=s.get_current_expected_cash() - Decimal("5"), reason="bank")
        r = self.refund(order, 2)                                           # 10.00 > 5.00 in the drawer
        self.assertContains(r, "only holds KSH 5.00", status_code=400)

    def test_closing_the_register_balances_after_a_refund(self):
        order = self.sell()
        self.refund(order, 2)
        with tenant_context(self.shop["tenant"]):
            s = CashRegisterSession.objects.get(pk=self.shop["session"].pk)
            self.assertEqual(s.total_refunds, Decimal("10.00"))
            s.closing_balance = Decimal("1015.00")                           # 1000 + 25 sale - 10 refund
            s.status = "CLOSED"; s.save()
            self.assertEqual((s.expected_closing_balance, s.discrepancy), (Decimal("1015.00"), Decimal("0.00")))

    def test_double_submit_refunds_once(self):
        order = self.sell()
        u = uuid.uuid4()
        self.refund(order, 2, client_uuid=u)
        self.refund(order, 2, client_uuid=u)
        with tenant_context(self.shop["tenant"]):
            self.assertEqual(Refund.objects.count(), 1)
        self.assertEqual(self.stock(), Decimal("97"))

    def test_old_sale_needs_an_admin(self):
        from tenants.services import create_staff_user
        manager = create_staff_user(tenant=self.shop["tenant"], handle="peter", role="MANAGER", pin="4321",
                                    password="S3cure-pass-99")
        order = self.sell(age_days=45)
        self.client.force_login(manager)
        self.assertContains(self.refund(order, 1), "45 days old", status_code=400)
        self.client.force_login(self.admin)
        self.assertEqual(self.refund(order, 1).status_code, 302)

    def test_another_shops_receipt_is_not_reachable(self):
        other = make_shop(business_name="Kamau Pharmacy", email="kamau@example.com")
        order = self.sell()
        self.client.force_login(other["owner"])
        self.assertEqual(self.client.get(FORM.format(order.receipt.pk)).status_code, 404)
        self.assertEqual(self.refund(order, 1).status_code, 404)

    def test_cashiers_cannot_refund(self):
        order = self.sell()
        self.client.force_login(self.cashier)
        self.assertEqual(self.client.get("/pos/refunds/").status_code, 403)
        self.assertEqual(self.client.get(FORM.format(order.receipt.pk)).status_code, 403)
        self.assertEqual(self.refund(order, 1).status_code, 403)
        self.assertEqual(self.stock(), Decimal("95"))


class RefundReportsAndPages(RefundBase):
    def test_dashboard_figures_are_net_of_refunds(self):
        order = self.sell()                                                 # revenue 25, cogs 10, profit 15
        self.refund(order, 2)                                               # restocked: -10 revenue, -4 cogs, -6 profit
        r = self.client.get("/pos/dashboard/")
        m = r.context["metrics"]
        self.assertEqual((m["revenue"], m["cogs"], m["profit"], m["refunds"]),
                         (Decimal("15.00"), Decimal("6.00"), Decimal("9.00"), Decimal("10.00")))
        self.assertEqual(r.context["cumulative_cash"]["total_cash_revenue"], Decimal("15.00"))
        self.assertEqual(r.context["cash_metrics"]["refunds"], Decimal("10.00"))

    def test_pages_render_and_link_up(self):
        order = self.sell()
        self.assertContains(self.client.get("/pos/refunds/?find=" + order.receipt.receipt_number), "Refund</a>")
        self.assertContains(self.client.get(FORM.format(order.receipt.pk)), "Cash to give the customer")
        self.refund(order, 2)
        rf = Refund.all_objects.get()
        d = self.client.get(f"/pos/refunds/{rf.pk}/")
        self.assertContains(d, rf.refund_number); self.assertContains(d, "Customer signature")
        self.assertContains(self.client.get("/pos/refunds/"), rf.refund_number)
        rec = self.client.get(f"/pos/receipts/{order.receipt.pk}/")
        self.assertContains(rec, "has been refunded"); self.assertContains(rec, "Refund this sale")
        self.assertContains(self.client.get(f"/pos/order/{order.pk}/"), "net revenue")
        self.assertContains(self.client.get("/pos/receipts/"), "Refunded 10.00")
        self.assertContains(self.client.get("/pos/register/close/"), "Customer Refunds")

    def test_fully_refunded_sale_offers_no_more_refunds(self):
        order = self.sell(); self.refund(order, 5)
        self.assertContains(self.client.get(FORM.format(order.receipt.pk)), "already been refunded")
        self.assertNotContains(self.client.get(f"/pos/receipts/{order.receipt.pk}/"), "Refund this sale")
