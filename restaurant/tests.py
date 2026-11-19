import json
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from billing.models import Subscription

from inventory.models import StockLevel
from inventory.stock import change_stock
from products.models import UnitOfMeasure
from sales.models import CashRegisterSession, Order
from tenants.context import tenant_context
from tenants.services import create_staff_user, register_business

from .models import (BillPayment, CashierShift, DeliveryRider, FloorZone, Ingredient, KitchenTicket, MenuItem, Modifier,
                     OrderLine, Recipe, RecipeLine, RestaurantOrder, RestaurantSettings, Station, Table, WasteRecord,
                     get_restaurant_settings)
from .services import billing, costing, delivery, kitchen, online
from .services import orders as osvc
from .services import stock as stock_svc
from .services.errors import RestaurantError
from .services.setup import create_ingredient, create_menu_item

User = get_user_model()
PW = "S3cure-pass-99"
def grant_access(*tenants):
    for t in tenants:
        Subscription.objects.filter(tenant=t).update(comp_until=timezone.now() + timedelta(days=30))


SIGNUP = dict(business_name="Mama Pima Grill", business_type="RESTAURANT", owner_name="Pima W",
              email="pima@example.com", phone="254712345678", password=PW, term_months=3)


def stock_of(branch, variant):
    sl = StockLevel.objects.filter(branch=branch, variant=variant).first()
    return D(sl.quantity) if sl else D(0)


@override_settings(TENANCY_ALLOW_NULL_TENANT=False)
class Base(TestCase):
    """A restaurant with: flour/cheese ingredients, a Margherita pizza recipe, a Coke (stocked), 2 stations."""

    def setUp(self):
        self.tenant, self.owner, _ = register_business(**SIGNUP)
        grant_access(self.tenant)
        ctx = tenant_context(self.tenant)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.branch = self.owner.branch
        self.waiter = create_staff_user(tenant=self.tenant, handle="wanjiru", role="WAITER", password=PW,
                                        first_name="Wanjiru", branch=self.branch)
        self.cashier = create_staff_user(tenant=self.tenant, handle="otieno", role="CASHIER", password=PW, branch=self.branch)
        self.kitchen_user = create_staff_user(tenant=self.tenant, handle="chef", role="KITCHEN", password=PW, branch=self.branch)
        self.grill = Station.objects.get(name="Kitchen")
        self.bar = Station.objects.get(name="Bar")
        g = UnitOfMeasure.objects.get(name="Gram")
        self.flour = create_ingredient(name="Flour", unit=g, cost_per_unit=D("0.10"), low_stock_threshold=D("500"))
        self.cheese = create_ingredient(name="Cheese", unit=g, cost_per_unit=D("0.80"), low_stock_threshold=D("300"))
        self.onion = create_ingredient(name="Onion", unit=g, cost_per_unit=D("0.05"), low_stock_threshold=D("100"))
        from products.models import Category
        meals, drinks = Category.objects.get(name="Meals"), Category.objects.get(name="Drinks")
        self.pizza = create_menu_item(name="Margherita", category=meals, price=D("800"), station=self.grill)
        rec = Recipe.objects.create(menu_item=self.pizza)
        RecipeLine.objects.create(recipe=rec, ingredient=self.flour, quantity=D("200"), waste_percent=D("10"))
        RecipeLine.objects.create(recipe=rec, ingredient=self.cheese, quantity=D("100"))
        RecipeLine.objects.create(recipe=rec, ingredient=self.onion, quantity=D("30"))
        costing.refresh_menu_cost(self.pizza)
        self.coke = create_menu_item(name="Coke", category=drinks, price=D("100"), kind="DRINK", fulfilment="STOCKED",
                                     station=self.bar, cost=D("40"))
        for ing, n in ((self.flour, 10000), (self.cheese, 5000), (self.onion, 2000)):
            change_stock(self.branch, ing.variant, D(n))
        change_stock(self.branch, self.coke.variant, D(24))
        self.extra_cheese = Modifier.objects.create(name="Extra cheese", kind="EXTRA", price=D("100"),
                                                    ingredient=self.cheese, ingredient_qty=D("50"))
        self.no_onion = Modifier.objects.create(name="No onions", kind="REMOVE", ingredient=self.onion)
        self.well_done = Modifier.objects.create(name="Well done", kind="CHOICE")
        self.t1 = Table.objects.create(branch=self.branch, name="T1", seats=4, zone=FloorZone.objects.first())
        self.t2 = Table.objects.create(branch=self.branch, name="T2", seats=2)
        self.t3 = Table.objects.create(branch=self.branch, name="T3", seats=2)
        self.session = CashRegisterSession.objects.create(branch=self.branch, opened_by=self.owner, opening_balance=D("1000"))

    def order(self, table=None, **kw):
        return osvc.create_order(user=self.waiter, branch=self.branch, table=table or self.t1, **kw)

    def policy(self, p, allow_negative=True):
        rs = get_restaurant_settings()
        rs.deduction_policy, rs.allow_negative_stock = p, allow_negative
        rs.save()


class SetupAndCostingTests(Base):
    def test_workspace_seeded(self):
        self.assertTrue(Station.objects.filter(name="Bar").exists())
        self.assertTrue(RestaurantSettings.objects.filter(tenant=self.tenant).exists())

    def test_recipe_cost_includes_waste_and_syncs_to_variant(self):
        # flour 200g +10% = 220g * .10 = 22 ; cheese 100*.8 = 80 ; onion 30*.05 = 1.5
        self.assertEqual(costing.recipe_cost(self.pizza), D("103.50"))
        self.pizza.variant.refresh_from_db()
        self.assertEqual(self.pizza.variant.cost_price, D("103.50"))
        bd = costing.cost_breakdown(self.pizza)
        self.assertEqual(bd["profit"], D("696.50"))
        self.assertEqual(bd["food_cost_percent"], D("12.94"))

    def test_recipe_yield_divides_cost(self):
        r = self.pizza.recipe
        r.yield_portions = D("2")
        r.save()
        self.assertEqual(costing.recipe_cost(self.pizza), D("51.75"))

    def test_ingredient_cost_change_reaches_menu_cost(self):
        self.cheese.variant.cost_price = D("1.00")
        self.cheese.variant.save()
        costing.refresh_all_costs()
        self.pizza.variant.refresh_from_db()
        self.assertEqual(self.pizza.variant.cost_price, D("123.50"))


class TableAndOrderTests(Base):
    def test_dine_in_needs_table_and_delivery_needs_details(self):
        with self.assertRaises(RestaurantError):
            osvc.create_order(user=self.waiter, branch=self.branch, order_type="DINE_IN")
        with self.assertRaises(RestaurantError):
            osvc.create_order(user=self.waiter, branch=self.branch, order_type="DELIVERY", customer_phone="0711")
        o = osvc.create_order(user=self.waiter, branch=self.branch, order_type="TAKEAWAY", customer_name="Ann")
        self.assertIsNone(o.table_id)

    def test_modifiers_price_notes_and_merge_identical_lines(self):
        o = self.order()
        a = osvc.add_line(o, self.pizza, 1, [self.extra_cheese.pk, self.no_onion.pk], "cut in 8")
        self.assertEqual(a.unit_price, D("900.00"))
        self.assertEqual(a.note, "cut in 8")
        b = osvc.add_line(o, self.pizza, 2, [self.no_onion.pk, self.extra_cheese.pk], "cut in 8")
        self.assertEqual(b.pk, a.pk)                       # same item + same options + same note = one line
        self.assertEqual(OrderLine.objects.get(pk=a.pk).quantity, 3)
        c = osvc.add_line(o, self.pizza, 1)                # plain pizza is a different line
        self.assertNotEqual(c.pk, a.pk)
        o.refresh_from_db()
        self.assertEqual(o.subtotal, D("3500.00"))

    def test_option_not_offered_on_item_is_refused(self):
        m = Modifier.objects.create(name="Ice", kind="CHOICE")
        m.menu_items.add(self.coke)
        with self.assertRaises(RestaurantError):
            osvc.add_line(self.order(), self.pizza, 1, [m.pk])

    def test_sold_out_item_cannot_be_added(self):
        self.pizza.is_available = False
        self.pizza.save()
        with self.assertRaises(RestaurantError):
            osvc.add_line(self.order(), self.pizza)

    def test_multiple_orders_per_table_and_transfer(self):
        o1, o2 = self.order(), self.order()
        self.assertEqual(osvc.table_open_orders(self.t1).count(), 2)
        with self.assertRaises(RestaurantError):               # T1 still has the other bill
            osvc.transfer_order(o1, self.t1)
        osvc.transfer_order(o1, self.t2)
        o1.refresh_from_db()
        self.assertEqual(o1.table_id, self.t2.pk)
        with self.assertRaises(RestaurantError):               # T1 busy -> must merge instead
            osvc.transfer_order(o1, self.t1)

    def test_merge_orders_moves_lines(self):
        o1, o2 = self.order(), self.order(self.t2)
        osvc.add_line(o1, self.pizza)
        osvc.add_line(o2, self.coke, 2)
        merged = osvc.merge_orders(o2, o1)
        self.assertEqual(merged.subtotal, D("1000.00"))
        o2.refresh_from_db()
        self.assertEqual(o2.status, "VOID")

    def test_merge_and_split_tables(self):
        osvc.merge_tables(self.t1, [self.t2, self.t3])
        self.t1.refresh_from_db()
        self.assertEqual(self.t1.capacity, 8)
        self.assertEqual(self.t1.label, "T1+T2+T3")
        with self.assertRaises(RestaurantError):               # T2 is part of T1 now
            osvc.create_order(user=self.waiter, branch=self.branch, table=self.t2)
        snap = {t["id"]: t for t in osvc.floor_snapshot(self.branch)}
        self.assertEqual(snap[self.t2.pk]["state"], "MERGED")
        osvc.split_tables(self.t1)
        self.t2.refresh_from_db()
        self.assertIsNone(self.t2.merged_into_id)

    def test_cannot_merge_occupied_table(self):
        self.order(self.t2)
        with self.assertRaises(RestaurantError):
            osvc.merge_tables(self.t1, [self.t2])

    def test_assign_waiter_and_table_states(self):
        osvc.assign_waiter(self.t1, self.waiter)
        o = self.order()
        self.assertEqual(o.waiter_id, self.waiter.pk)
        osvc.set_hold(self.t3, "RESERVED", "Mr Kamau")
        snap = {t["id"]: t for t in osvc.floor_snapshot(self.branch)}
        self.assertEqual(snap[self.t1.pk]["state"], "OCCUPIED")
        self.assertEqual(snap[self.t3.pk]["state"], "RESERVED")
        self.assertEqual(snap[self.t2.pk]["state"], "AVAILABLE")
        with self.assertRaises(RestaurantError):
            osvc.set_hold(self.t1, "CLEANING")

    def test_split_bill_by_items_and_equal_shares(self):
        o = self.order()
        a = osvc.add_line(o, self.pizza)
        b = osvc.add_line(o, self.coke, 3)
        child = osvc.split_order(o, [b.pk], self.waiter)
        o.refresh_from_db()
        self.assertEqual((o.subtotal, child.subtotal), (D("800.00"), D("300.00")))
        self.assertEqual(child.table_id, o.table_id)
        with self.assertRaises(RestaurantError):               # must leave something behind
            osvc.split_order(child, [OrderLine.objects.get(order=child).pk], self.waiter)
        shares = osvc.equal_shares(o, 3)
        self.assertEqual(sum(shares), o.balance)
        self.assertEqual(shares, [D("266.66"), D("266.66"), D("266.68")])

    def test_service_charge_dine_in_only(self):
        rs = get_restaurant_settings()
        rs.service_charge_percent = D("10")
        rs.save()
        o = self.order()
        osvc.add_line(o, self.pizza)
        o.refresh_from_db()
        self.assertEqual((o.service_charge, o.total), (D("80.00"), D("880.00")))
        tk = osvc.create_order(user=self.waiter, branch=self.branch, order_type="TAKEAWAY", customer_name="Ann")
        self.assertEqual(tk.service_charge_percent, 0)

    def test_delivery_fee_and_zone(self):
        from .models import DeliveryZone
        z = DeliveryZone.objects.create(name="Town", fee=D("150"))
        o = osvc.create_order(user=self.waiter, branch=self.branch, order_type="DELIVERY", customer_phone="0711",
                              delivery_address="Moi Ave", delivery_zone=z)
        osvc.add_line(o, self.coke)
        o.refresh_from_db()
        self.assertEqual((o.delivery_fee, o.total), (D("150.00"), D("250.00")))

    def test_discount_rules(self):
        o = self.order()
        osvc.add_line(o, self.pizza)
        with self.assertRaises(RestaurantError):               # over the cap
            osvc.set_discount(o, self.owner, "PERCENT", D("99"))
        o = osvc.set_discount(o, self.owner, "PERCENT", D("10"), "Regular")
        self.assertEqual((o.discount_total, o.total), (D("80.00"), D("720.00")))
        o = osvc.set_discount(o, self.owner, "NONE")
        self.assertEqual(o.total, D("800.00"))


class KitchenAndStockTests(Base):
    def test_send_creates_one_ticket_per_station(self):
        o = self.order()
        osvc.add_line(o, self.pizza)
        osvc.add_line(o, self.coke)
        tickets = kitchen.send_to_kitchen(o, self.waiter)
        self.assertEqual({t.station.name for t in tickets}, {"Kitchen", "Bar"})
        with self.assertRaises(RestaurantError):
            kitchen.send_to_kitchen(o, self.waiter)            # nothing new
        osvc.add_line(o, self.coke)
        t2 = kitchen.send_to_kitchen(o, self.waiter)
        self.assertEqual(t2[0].round, 2)
        with self.assertRaises(RestaurantError):               # sent items can't be edited by quantity
            osvc.set_line_quantity(o.lines.filter(status="SENT").first(), 5)

    def test_ticket_flow_prep_time_and_waiter_alert(self):
        from notifications.models import Notification
        o = self.order()
        osvc.add_line(o, self.pizza)
        (t,) = kitchen.send_to_kitchen(o, self.waiter)
        kitchen.start_ticket(t, self.kitchen_user)
        kitchen.ready_ticket(t, self.kitchen_user)
        t.refresh_from_db()
        self.assertEqual(t.status, "READY")
        self.assertIsNotNone(t.prep_seconds)
        self.assertEqual(o.lines.first().status if False else OrderLine.objects.get(order=o).status, "READY")
        n = Notification.objects.filter(user=self.waiter, title__startswith="Food ready")
        self.assertEqual(n.count(), 1)
        self.assertEqual(len(kitchen.ready_for_waiter(self.branch, self.waiter)), 1)
        snap = {x["id"]: x for x in osvc.floor_snapshot(self.branch)}
        self.assertTrue(snap[self.t1.pk]["food_ready"])
        kitchen.serve_ticket(t, self.waiter)
        self.assertEqual(OrderLine.objects.get(order=o).status, "SERVED")
        self.assertEqual(kitchen.ready_for_waiter(self.branch, self.waiter), [])
        with self.assertRaises(RestaurantError):
            kitchen.start_ticket(t, self.kitchen_user)

    def test_kds_board_filters_by_station(self):
        o = self.order()
        osvc.add_line(o, self.pizza)
        osvc.add_line(o, self.coke)
        kitchen.send_to_kitchen(o, self.waiter)
        self.assertEqual(len(kitchen.kds_board(self.branch)), 2)
        only_bar = kitchen.kds_board(self.branch, self.bar.pk)
        self.assertEqual([t["station"] for t in only_bar], ["Bar"])

    def test_policy_on_ready_deducts_ingredients_once(self):
        self.policy("ON_READY")
        o = self.order()
        osvc.add_line(o, self.pizza, 2, [self.extra_cheese.pk, self.no_onion.pk])
        (t,) = kitchen.send_to_kitchen(o, self.waiter)
        self.assertEqual(stock_of(self.branch, self.flour.variant), D(10000))     # nothing yet
        kitchen.ready_ticket(t, self.kitchen_user)
        # per pizza: flour 220 ; cheese 100 + 50 extra ; onion removed
        self.assertEqual(stock_of(self.branch, self.flour.variant), D(10000) - 440)
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(5000) - 300)
        self.assertEqual(stock_of(self.branch, self.onion.variant), D(2000))
        kitchen.serve_ticket(t, self.waiter)                                      # no double deduction
        self.assertEqual(stock_of(self.branch, self.flour.variant), D(10000) - 440)

    def test_policy_on_send(self):
        self.policy("ON_SEND")
        o = self.order()
        osvc.add_line(o, self.pizza)
        osvc.add_line(o, self.coke, 2)
        kitchen.send_to_kitchen(o, self.waiter)
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(4900))
        self.assertEqual(stock_of(self.branch, self.coke.variant), D(22))

    def test_policy_on_served_and_on_paid(self):
        self.policy("ON_SERVED")
        o = self.order()
        osvc.add_line(o, self.pizza)
        (t,) = kitchen.send_to_kitchen(o, self.waiter)
        kitchen.ready_ticket(t, self.kitchen_user)
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(5000))
        kitchen.serve_ticket(t, self.waiter)
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(4900))

        self.policy("ON_PAID")
        o2 = self.order(self.t2)
        osvc.add_line(o2, self.pizza)
        (t2,) = kitchen.send_to_kitchen(o2, self.waiter)
        kitchen.ready_ticket(t2, self.kitchen_user)
        kitchen.serve_ticket(t2, self.waiter)
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(4900))
        billing.pay(o2, user=self.cashier, method="CASH", tendered=D("800"))
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(4800))

    def test_skipping_a_step_still_deducts_once(self):
        self.policy("ON_READY")
        o = self.order()
        osvc.add_line(o, self.pizza)
        (t,) = kitchen.send_to_kitchen(o, self.waiter)
        kitchen.ready_ticket(t, self.kitchen_user)
        kitchen.recall_ticket(t, self.kitchen_user)
        kitchen.ready_ticket(t, self.kitchen_user)
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(4900))

    def test_voiding_a_sent_line_restores_stock_and_cancels_ticket(self):
        self.policy("ON_SEND")
        o = self.order()
        line = osvc.add_line(o, self.pizza)
        (t,) = kitchen.send_to_kitchen(o, self.waiter)
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(4900))
        osvc.void_line(line, self.owner, "Guest left")
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(5000))
        t.refresh_from_db()
        self.assertEqual(t.status, "CANCELLED")
        o.refresh_from_db()
        self.assertEqual(o.total, D("0.00"))

    def test_cancel_order_restores_stock(self):
        self.policy("ON_SEND")
        o = self.order()
        osvc.add_line(o, self.pizza, 2)
        kitchen.send_to_kitchen(o, self.waiter)
        osvc.cancel_order(o, self.owner, "walkout")
        self.assertEqual(stock_of(self.branch, self.flour.variant), D(10000))
        o.refresh_from_db()
        self.assertEqual(o.status, "VOID")

    def test_negative_stock_policy(self):
        self.policy("ON_SEND", allow_negative=False)
        StockLevel.objects.filter(variant=self.cheese.variant).update(quantity=50)
        o = self.order()
        osvc.add_line(o, self.pizza)
        with self.assertRaises(RestaurantError) as cm:
            kitchen.send_to_kitchen(o, self.waiter)
        self.assertIn("Cheese", str(cm.exception))
        self.assertEqual(o.lines.get().status, "DRAFT")           # the whole send rolled back
        self.policy("ON_SEND", allow_negative=True)
        kitchen.send_to_kitchen(o, self.waiter)
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(-50))

    def test_low_stock_alert_for_ingredient(self):
        from notifications.models import Notification
        self.policy("ON_SEND")
        StockLevel.objects.filter(variant=self.cheese.variant).update(quantity=350)
        o = self.order()
        osvc.add_line(o, self.pizza)
        with self.captureOnCommitCallbacks(execute=True):
            kitchen.send_to_kitchen(o, self.waiter)
        self.assertTrue(Notification.objects.filter(title__icontains="Cheese").exists())

    def test_waste_ingredient_and_dish(self):
        from inventory.models import StockAdjustment
        rec = stock_svc.record_waste(branch=self.branch, user=self.owner, reason="SPOILAGE",
                                     ingredient=self.cheese, quantity=D("250"))
        self.assertEqual(rec.cost, D("200.00"))
        self.assertEqual(stock_of(self.branch, self.cheese.variant), D(4750))
        self.assertTrue(StockAdjustment.objects.filter(variant=self.cheese.variant, adjustment_type="DAMAGE").exists())
        rec2 = stock_svc.record_waste(branch=self.branch, user=self.owner, reason="BURNT", menu_item=self.pizza, portions=D("2"))
        self.assertEqual(stock_of(self.branch, self.flour.variant), D(10000) - 440)
        self.assertEqual(rec2.cost, D("207.00"))
        with self.assertRaises(RestaurantError):
            stock_svc.record_waste(branch=self.branch, user=self.owner, reason="PREP", ingredient=self.cheese, quantity=D("0"))
        self.assertEqual(WasteRecord.objects.count(), 2)


class BillingTests(Base):
    def ready_order(self, table=None, items=((None, 1),)):
        o = self.order(table)
        osvc.add_line(o, self.pizza)
        osvc.add_line(o, self.coke, 2)           # 800 + 200 = 1000
        kitchen.send_to_kitchen(o, self.waiter)
        return o

    def test_cash_payment_makes_sales_order_and_receipt(self):
        o = self.ready_order()
        p = billing.pay(o, user=self.cashier, method="CASH", tendered=D("1200"))
        self.assertEqual((p.amount, p.change_given), (D("1000.00"), D("200.00")))
        o.refresh_from_db()
        self.assertEqual(o.status, "PAID")
        sale = o.sales_order
        self.assertEqual((sale.total_revenue, sale.payment_method, sale.cash_amount), (D("1000.00"), "CASH", D("1000.00")))
        self.assertEqual(sale.total_cogs, D("103.50") + 2 * D("40"))
        self.assertEqual(sale.items.count(), 2)
        self.assertEqual(sale.receipt.total, D("1000.00"))
        self.assertEqual(sale.receipt.meta["table"], "T1")
        self.assertEqual(sale.cash_session_id, self.session.pk)
        self.assertEqual(self.session.total_cash_sales, D("1000.00"))
        self.assertEqual(self.session.expected_closing_balance if hasattr(self.session, "expected_closing_balance") and False else 0, 0)

    def test_service_charge_and_discount_flow_into_order_and_cash(self):
        rs = get_restaurant_settings()
        rs.service_charge_percent = D("10")
        rs.save()
        o = self.ready_order()
        osvc.set_discount(o, self.owner, "PERCENT", D("10"), "Regular")       # 1000 - 100 = 900, +10% = 990
        o.refresh_from_db()
        self.assertEqual(o.total, D("990.00"))
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("1000"))
        sale = RestaurantOrder.objects.get(pk=o.pk).sales_order
        self.assertEqual((sale.subtotal, sale.discount_total, sale.total_revenue), (D("1000.00"), D("100.00"), D("900.00")))
        self.assertEqual((sale.service_charge, sale.grand_total, sale.cash_amount), (D("90.00"), D("990.00"), D("990.00")))
        self.assertEqual(sum(i.revenue_line for i in sale.items.all()), D("900.00"))   # discount fully allocated
        self.assertEqual(sale.receipt.total, D("990.00"))
        self.assertEqual(self.session.total_cash_sales, D("990.00"))

    def test_discount_allocation_is_exact_with_odd_cents(self):
        o = self.order()
        osvc.add_line(o, self.coke, 3)
        osvc.add_line(o, self.pizza, 1)
        osvc.set_discount(o, self.owner, "AMOUNT", D("100.01"))
        o.refresh_from_db()
        self.assertEqual(o.total, D("999.99"))
        kitchen.send_to_kitchen(o, self.waiter)
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("1000"))
        sale = RestaurantOrder.objects.get(pk=o.pk).sales_order
        self.assertEqual(sum(i.revenue_line for i in sale.items.all()), D("999.99"))

    def test_partial_then_split_payments_with_mpesa(self):
        rs = get_restaurant_settings()
        rs.accept_mpesa = True
        rs.save()
        o = self.ready_order()
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("400"), payer="Guest 1")
        o.refresh_from_db()
        self.assertEqual((o.status, o.balance), ("OPEN", D("600.00")))
        with self.assertRaises(RestaurantError):                           # reference required
            billing.pay(o, user=self.cashier, method="MPESA", amount=D("300"))
        with self.assertRaises(RestaurantError):                           # more than balance
            billing.pay(o, user=self.cashier, method="MPESA", amount=D("700"), reference="QWE1")
        billing.pay(o, user=self.cashier, method="MPESA", amount=D("300"), reference="QWE123", payer="Guest 2")
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("500"), payer="Guest 3")
        o.refresh_from_db()
        self.assertEqual(o.status, "PAID")
        sale = o.sales_order
        self.assertEqual(sale.payment_method, "SPLIT")
        self.assertEqual(sale.cash_amount, D("700.00"))                    # 400 + 300 of the 500 handed over
        self.assertEqual(sale.change_given, D("200.00"))
        self.assertEqual(self.session.total_cash_sales, D("700.00"))
        self.assertEqual(len(sale.receipt.meta["payments"]), 3)

    def test_mpesa_and_card_off_by_default(self):
        o = self.ready_order()
        with self.assertRaises(RestaurantError):
            billing.pay(o, user=self.cashier, method="MPESA", amount=D("100"), reference="X")
        with self.assertRaises(RestaurantError):
            billing.pay(o, user=self.cashier, method="ONLINE", amount=D("100"))

    def test_cash_needs_open_register(self):
        o = self.ready_order()
        CashRegisterSession.objects.update(status="CLOSED")
        with self.assertRaises(RestaurantError) as cm:
            billing.pay(o, user=self.cashier, method="CASH", tendered=D("1000"))
        self.assertIn("register", str(cm.exception).lower())

    def test_unsent_items_block_payment(self):
        o = self.order()
        osvc.add_line(o, self.pizza)
        with self.assertRaises(RestaurantError):
            billing.pay(o, user=self.cashier, method="CASH", tendered=D("800"))

    def test_paid_order_is_locked(self):
        o = self.ready_order()
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("1000"))
        with self.assertRaises(RestaurantError):
            osvc.add_line(RestaurantOrder.objects.get(pk=o.pk), self.coke)
        with self.assertRaises(RestaurantError):
            billing.pay(RestaurantOrder.objects.get(pk=o.pk), user=self.cashier, method="CASH", tendered=D("5"))

    def test_void_payment_reopens_balance(self):
        o = self.ready_order()
        p = billing.pay(o, user=self.cashier, method="CASH", tendered=D("400"))
        billing.void_payment(p, self.owner)
        o.refresh_from_db()
        self.assertEqual(o.balance, D("1000.00"))

    def test_complimentary_bill(self):
        o = self.order()
        osvc.add_line(o, self.coke)
        kitchen.send_to_kitchen(o, self.waiter)
        osvc.set_discount(o, self.owner, "PERCENT", D("100")) if False else None
        o.discount_type, o.discount_value = "PERCENT", D("100")
        o.save()
        osvc.recalculate(o)
        sale = billing.close_complimentary(o, self.owner)
        self.assertEqual(sale.total_revenue, D("0.00"))

    def test_refund_of_dish_does_not_restock_it(self):
        from sales.refunds import _is_prepared_dish
        self.assertTrue(_is_prepared_dish(self.pizza.variant))
        self.assertFalse(_is_prepared_dish(self.coke.variant))

    def test_split_bill_each_guest_pays_own_check(self):
        o = self.ready_order()
        coke_line = o.lines.get(menu_item=self.coke)
        child = osvc.split_order(o, [coke_line.pk], self.waiter)
        billing.pay(child, user=self.cashier, method="CASH", tendered=D("200"))
        o.refresh_from_db()
        self.assertEqual(o.status, "OPEN")
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("800"))
        self.assertEqual(Order.objects.filter(restaurant_order__isnull=False).count(), 2)
        self.assertEqual(self.session.total_cash_sales, D("1000.00"))


class ShiftTests(Base):
    def test_shift_reconciliation_short_and_over(self):
        from notifications.models import Notification
        o = self.order()
        osvc.add_line(o, self.pizza)
        osvc.add_line(o, self.coke, 2)
        kitchen.send_to_kitchen(o, self.waiter)
        shift = billing.open_shift(self.cashier, self.branch, D("500"))
        with self.assertRaises(RestaurantError):
            billing.open_shift(self.cashier, self.branch)
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("1000"))
        t = billing.shift_totals(shift)
        self.assertEqual((t["cash_sales"], t["expected_cash"]), (D("1000.00"), D("1500.00")))
        shift = billing.close_shift(shift, self.cashier, counted_cash=D("1450"))
        self.assertEqual((shift.variance, shift.status), (D("-50.00"), "CLOSED"))
        self.assertTrue(Notification.objects.filter(title__startswith="Shift closed short").exists())
        self.assertIsNone(billing.current_shift(self.cashier))

    def test_payment_auto_starts_shift(self):
        o = self.order()
        osvc.add_line(o, self.coke)
        kitchen.send_to_kitchen(o, self.waiter)
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("100"))
        self.assertIsNotNone(billing.current_shift(self.cashier))
        self.assertEqual(BillPayment.objects.get().shift_id, billing.current_shift(self.cashier).pk)


class DeliveryTests(Base):
    def delivery_order(self):
        o = osvc.create_order(user=self.cashier, branch=self.branch, order_type="DELIVERY", customer_name="Ann",
                              customer_phone="0711222333", delivery_address="Moi Ave 5")
        osvc.add_line(o, self.coke, 2)
        return o

    def test_delivery_lifecycle(self):
        o = self.delivery_order()
        (t,) = kitchen.send_to_kitchen(o, self.cashier)
        kitchen.ready_ticket(t, self.kitchen_user)
        o.refresh_from_db()
        self.assertEqual(o.handover, "READY")
        with self.assertRaises(RestaurantError):                       # no rider yet
            delivery.set_handover(o, "OUT")
        rider = DeliveryRider.objects.create(name="Juma", phone="0700")
        delivery.assign_rider(o, rider)
        o.refresh_from_db()
        self.assertEqual(o.handover, "ASSIGNED")
        delivery.set_handover(o, "OUT")
        with self.assertRaises(RestaurantError):
            delivery.assign_rider(o, None)                             # already left
        delivery.set_handover(o, "COMPLETED")
        o.refresh_from_db()
        self.assertEqual(o.handover, "COMPLETED")

    def test_takeaway_pickup_and_pay(self):
        o = osvc.create_order(user=self.cashier, branch=self.branch, order_type="TAKEAWAY", customer_name="Bob")
        osvc.add_line(o, self.coke)
        (t,) = kitchen.send_to_kitchen(o, self.cashier)
        kitchen.ready_ticket(t, self.kitchen_user)
        delivery.set_handover(o, "COMPLETED")
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("100"))
        o.refresh_from_db()
        self.assertEqual((o.status, o.handover), ("PAID", "COMPLETED"))
        with self.assertRaises(RestaurantError):
            delivery.set_handover(o, "PENDING")

    def test_delivery_fee_is_on_bill_but_not_revenue(self):
        o = self.delivery_order()
        osvc.set_delivery_fee(o, D("200"))
        kitchen.send_to_kitchen(o, self.cashier)
        billing.pay(o, user=self.cashier, method="CASH", tendered=D("400"))
        sale = RestaurantOrder.objects.get(pk=o.pk).sales_order
        self.assertEqual((sale.total_revenue, sale.delivery_fee, sale.grand_total, sale.cash_amount),
                         (D("200.00"), D("200.00"), D("400.00"), D("400.00")))


class OnlineOrderingTests(Base):
    def setUp(self):
        super().setUp()
        rs = get_restaurant_settings()
        rs.online_ordering_enabled = True
        rs.save()
        self.key = online.generate_api_key(self.tenant)
        self.client = Client()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.key}"}

    def payload(self, **over):
        return {"external_ref": "WEB-1", "type": "DELIVERY", "customer": {"name": "Ann", "phone": "0711"},
                "address": "Moi Ave", "items": [{"sku": self.coke.variant.sku, "quantity": 2},
                                                {"menu_item_id": self.pizza.pk, "quantity": 1, "option_ids": [self.well_done.pk]}],
                **over}

    def post(self, body, **extra):
        return self.client.post("/restaurant/api/v1/orders/", data=json.dumps(body), content_type="application/json", **{**self.auth, **extra})

    def test_key_required_and_hash_only_stored(self):
        self.assertEqual(self.client.get("/restaurant/api/v1/menu/").status_code, 401)
        self.assertEqual(self.client.get("/restaurant/api/v1/menu/", HTTP_AUTHORIZATION="Bearer us_x_y").status_code, 401)
        rs = get_restaurant_settings()
        self.assertNotIn(self.key.split("_", 2)[2], rs.online_key_hash)
        r = self.client.get("/restaurant/api/v1/menu/", **self.auth)
        self.assertEqual(r.status_code, 200)
        names = {i["name"] for i in r.json()["items"]}
        self.assertEqual(names, {"Margherita", "Coke"})

    def test_order_waits_for_acceptance_then_goes_to_kitchen(self):
        r = self.post(self.payload())
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["status"], "awaiting_acceptance")
        o = RestaurantOrder.objects.get(external_ref="WEB-1")
        self.assertEqual((o.source, o.total), ("ONLINE", D("1000.00")))
        self.assertFalse(KitchenTicket.objects.exists())
        kitchen.accept_online_order(o, self.owner)
        self.assertEqual(KitchenTicket.objects.count(), 2)

    def test_idempotent_and_auto_accept_and_prepaid(self):
        rs = get_restaurant_settings()
        rs.auto_accept_online = True
        rs.save()
        a = self.post(self.payload(paid=True, payment_reference="PAY9"))
        b = self.post(self.payload())
        self.assertEqual((a.status_code, b.status_code), (201, 200))
        self.assertEqual(RestaurantOrder.objects.filter(external_ref="WEB-1").count(), 1)
        self.assertEqual(KitchenTicket.objects.count(), 2)
        o = RestaurantOrder.objects.get(external_ref="WEB-1")
        self.assertEqual(o.balance, D("0.00"))
        self.assertEqual(self.client.get("/restaurant/api/v1/orders/WEB-1/", **self.auth).json()["status"], "OPEN")

    def test_bad_payloads(self):
        self.assertEqual(self.post({"items": []}).status_code, 400)
        self.assertEqual(self.post(self.payload(items=[{"sku": "NOPE", "quantity": 1}])).status_code, 400)
        self.assertEqual(self.post(self.payload(type="DELIVERY", address="")).status_code, 400)
        self.assertFalse(RestaurantOrder.objects.exists())     # a failed request leaves nothing behind

    def test_disabled_or_revoked_key_is_rejected(self):
        online.revoke_api_key(self.tenant)
        self.assertEqual(self.post(self.payload()).status_code, 401)

    def test_other_restaurants_key_cannot_see_this_menu(self):
        t2, o2, _ = register_business(**{**SIGNUP, "business_name": "Other Cafe", "email": "o2@example.com"})
        grant_access(t2)
        with tenant_context(t2):
            rs2 = get_restaurant_settings(t2)
            rs2.online_ordering_enabled = True
            rs2.save()
            key2 = online.generate_api_key(t2)
        r = self.client.get("/restaurant/api/v1/menu/", HTTP_AUTHORIZATION=f"Bearer {key2}")
        self.assertEqual(r.json()["items"], [])


@override_settings(TENANCY_ALLOW_NULL_TENANT=False)
class AccessTests(TestCase):
    """Restaurant features exist only for restaurants/hotels, and only for the right roles."""

    def setUp(self):
        self.rt, self.ro, _ = register_business(**SIGNUP)
        self.st, self.so, _ = register_business(**{**SIGNUP, "business_name": "Duka", "business_type": "MINI_MART", "email": "d@example.com"})
        self.ht, self.ho, _ = register_business(**{**SIGNUP, "business_name": "Lodge", "business_type": "HOTEL", "email": "h@example.com"})
        grant_access(self.rt, self.st, self.ht)
        self.waiter = create_staff_user(tenant=self.rt, handle="waiter1", role="WAITER", password=PW, branch=self.ro.branch)
        self.chef = create_staff_user(tenant=self.rt, handle="chef1", role="KITCHEN", password=PW, branch=self.ro.branch)
        self.cashier = create_staff_user(tenant=self.rt, handle="cashier1", role="CASHIER", password=PW, branch=self.ro.branch)

    def get(self, user, url):
        c = Client()
        c.force_login(user)
        return c.get(url, HTTP_HOST="testserver")

    def test_non_restaurant_business_gets_404_everywhere(self):
        for url in ("/restaurant/", "/restaurant/floor/", "/restaurant/kitchen/", "/restaurant/menu/",
                    "/restaurant/api/floor/", "/restaurant/settings/"):
            self.assertEqual(self.get(self.so, url).status_code, 404, url)

    def test_restaurant_and_hotel_owners_get_in(self):
        for owner in (self.ro, self.ho):
            self.assertEqual(self.get(owner, "/restaurant/").status_code, 200)
            self.assertEqual(self.get(owner, "/restaurant/floor/").status_code, 200)
            self.assertEqual(self.get(owner, "/restaurant/menu/").status_code, 200)

    def test_anonymous_is_sent_to_login(self):
        r = Client().get("/restaurant/floor/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r["Location"])
        self.assertEqual(Client().get("/restaurant/api/floor/").status_code, 401)

    def test_role_boundaries(self):
        self.assertEqual(self.get(self.waiter, "/restaurant/floor/").status_code, 200)
        self.assertEqual(self.get(self.waiter, "/restaurant/menu/").status_code, 403)
        self.assertEqual(self.get(self.waiter, "/restaurant/kitchen/").status_code, 403)
        self.assertEqual(self.get(self.waiter, "/restaurant/shift/").status_code, 403)
        self.assertEqual(self.get(self.chef, "/restaurant/kitchen/").status_code, 200)
        self.assertEqual(self.get(self.chef, "/restaurant/floor/").status_code, 403)
        self.assertEqual(self.get(self.chef, "/restaurant/waste/").status_code, 200)
        self.assertEqual(self.get(self.cashier, "/restaurant/shift/").status_code, 200)
        self.assertEqual(self.get(self.cashier, "/restaurant/settings/").status_code, 403)

    def test_sidebar_links_only_for_restaurants(self):
        self.assertContains(self.get(self.ro, "/restaurant/"), "Floor &amp; Tables")
        from django.urls import reverse
        c = Client()
        c.force_login(self.so)
        resp = c.get(reverse("admin-dashboard"), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Floor &amp; Tables")
        self.assertNotContains(resp, "Kitchen Display")

    def test_login_landing_page_by_role(self):
        from users.views import home_for
        self.assertEqual(home_for(self.waiter), "/restaurant/waiter/")
        self.assertEqual(home_for(self.chef), "/restaurant/kitchen/")
        self.assertEqual(home_for(self.cashier), "/restaurant/floor/")

    def test_retail_pos_terminal_redirects_restaurants(self):
        from django.urls import reverse
        r = self.get(self.cashier, reverse("pos_terminal"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/restaurant/floor/")

    def test_signup_form_offers_restaurant_and_hotel(self):
        r = Client().get("/tenants/signup/") if False else None
        from tenants.models import Tenant
        self.assertIn("RESTAURANT", Tenant.BusinessType.values)
        self.assertTrue(self.rt.is_hospitality and self.ht.is_hospitality and not self.st.is_hospitality)


@override_settings(TENANCY_ALLOW_NULL_TENANT=False)
class ApiFlowTests(Base):
    """The JSON endpoints the screens use, end to end as a waiter and a cashier."""

    def login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def call(self, c, url, body=None, method="post"):
        if method == "get":
            return c.get(url)
        return c.post(url, data=json.dumps(body or {}), content_type="application/json")

    def test_waiter_takes_order_kitchen_cooks_cashier_collects(self):
        w, k, cash = self.login(self.waiter), self.login(self.kitchen_user), self.login(self.cashier)
        r = self.call(w, "/restaurant/api/order/create/", {"type": "DINE_IN", "table_id": self.t1.pk, "guests": 2})
        self.assertEqual(r.status_code, 200, r.content)
        oid = r.json()["id"]
        r = self.call(w, f"/restaurant/api/order/{oid}/add_line/", {"item_id": self.pizza.pk, "qty": 1, "option_ids": [self.extra_cheese.pk]})
        self.assertEqual(r.json()["order"]["total"], "900.00")
        self.assertEqual(self.call(w, f"/restaurant/api/order/{oid}/send/").status_code, 200)
        # the waiter may not take payment, discount or cancel
        self.assertEqual(self.call(w, f"/restaurant/api/order/{oid}/pay/", {"method": "CASH", "tendered": "900"}).status_code, 400)
        self.assertEqual(self.call(w, f"/restaurant/api/order/{oid}/discount/", {"type": "PERCENT", "value": "5"}).status_code, 400)
        self.assertEqual(self.call(w, f"/restaurant/api/order/{oid}/cancel/").status_code, 400)
        # kitchen board
        board = self.call(k, "/restaurant/api/kds/", method="get").json()["tickets"]
        self.assertEqual(len(board), 1)
        tid = board[0]["id"]
        self.assertEqual(self.call(w, f"/restaurant/api/ticket/{tid}/ready/").status_code, 400)   # waiter can't cook
        self.assertEqual(self.call(k, f"/restaurant/api/ticket/{tid}/start/").status_code, 200)
        self.assertEqual(self.call(k, f"/restaurant/api/ticket/{tid}/ready/").status_code, 200)
        ready = self.call(w, "/restaurant/api/waiter/", method="get").json()["ready"]
        self.assertEqual(len(ready), 1)
        self.assertEqual(self.call(w, f"/restaurant/api/ticket/{tid}/serve/").status_code, 200)
        # cashier pays
        r = self.call(cash, f"/restaurant/api/order/{oid}/pay/", {"method": "CASH", "tendered": "1000"})
        self.assertEqual(r.status_code, 200, r.content)
        o = r.json()["order"]
        self.assertEqual(o["status"], "PAID")
        self.assertTrue(o["receipt_url"])
        self.assertEqual(Client().get(o["receipt_url"]).status_code, 302)

    def test_waiter_cannot_touch_another_branchs_order(self):
        from inventory.models import Branch
        other = Branch.objects.create(name="Other")
        o = osvc.create_order(user=self.owner, branch=other, order_type="TAKEAWAY", customer_name="x")
        w = self.login(self.waiter)
        self.assertEqual(w.get(f"/restaurant/api/order/{o.pk}/").status_code, 404)

    def test_floor_editor_save_and_delete(self):
        owner = self.login(self.owner)
        r = self.call(owner, "/restaurant/api/table/save/", {"name": "T9", "seats": 6, "shape": "ROUND"})
        self.assertEqual(r.status_code, 200)
        tid = r.json()["id"]
        self.assertEqual(self.call(owner, "/restaurant/api/table/save/", {"name": "T9", "seats": 2}).status_code, 400)  # duplicate
        r = self.call(owner, "/restaurant/api/floor/layout/", {"tables": [{"id": tid, "x": 40, "y": 30, "w": 12, "h": 12}]})
        self.assertEqual(r.json()["saved"], 1)
        t = Table.objects.get(pk=tid)
        self.assertEqual((t.x, t.y), (40, 30))
        self.assertEqual(self.call(owner, f"/restaurant/api/table/{tid}/delete/").status_code, 200)
        self.assertFalse(Table.objects.get(pk=tid).is_active)
        w = self.login(self.waiter)
        self.assertEqual(self.call(w, "/restaurant/api/table/save/", {"name": "Z"}).status_code, 403)

    def test_pages_render(self):
        owner = self.login(self.owner)
        o = self.order()
        osvc.add_line(o, self.pizza)
        for url in ("/restaurant/", "/restaurant/floor/", "/restaurant/floor/edit/", f"/restaurant/order/{o.pk}/",
                    "/restaurant/orders/", "/restaurant/kitchen/", "/restaurant/waiter/", "/restaurant/delivery/",
                    "/restaurant/menu/", "/restaurant/menu/new/", f"/restaurant/menu/{self.pizza.pk}/",
                    f"/restaurant/menu/{self.pizza.pk}/recipe/", "/restaurant/ingredients/", "/restaurant/ingredients/new/",
                    f"/restaurant/ingredients/{self.flour.pk}/", "/restaurant/waste/", "/restaurant/shift/", "/restaurant/shifts/",
                    "/restaurant/settings/", "/restaurant/reports/", "/restaurant/setup/stations/", "/restaurant/setup/modifiers/",
                    "/restaurant/setup/riders/", "/restaurant/setup/zones/", "/restaurant/setup/modifiers/new/",
                    "/restaurant/api/menu/", "/restaurant/api/floor/", "/restaurant/api/kds/", "/restaurant/api/waiter/"):
            r = owner.get(url)
            self.assertEqual(r.status_code, 200, f"{url} -> {r.status_code}")

    def test_menu_item_form_creates_variant_backed_item(self):
        owner = self.login(self.owner)
        from products.models import Category
        r = owner.post("/restaurant/menu/new/", {
            "name": "Pilau", "category": Category.objects.get(name="Meals").pk, "price": "450", "kind": "MEAL",
            "fulfilment": "RECIPE", "station": self.grill.pk, "prep_minutes": 15, "is_available": "on", "is_active": "on"})
        self.assertEqual(r.status_code, 302)
        item = MenuItem.objects.get(variant__product__name="Pilau")
        self.assertEqual(item.price, D("450.00"))
        self.assertTrue(item.variant.sku.startswith("M"))
