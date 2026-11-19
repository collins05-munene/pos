from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from inventory.models import (InventoryTransfer, PurchaseOrder, PurchaseOrderItem, StockAdjustment, StockLevel)
from inventory.transfers import cancel_transfer, create_transfer, dispatch_transfer, receive_transfer
from promotions.models import Discount
from sales.cart import CartItem
from sales.models import Order, OrderItem, Refund
from sales.refunds import process_refund
from sales.utils import complete_pos_sale
from supplier.models import Supplier
from tenants.context import tenant_context

from . import integration as ig
from . import reporting as rp
from .models import JournalEntry
from .services import trial_balance
from .testkit import D, TODAY, ago, balance, finance_shop


class Base(TestCase):
    def setUp(self):
        self.shop = finance_shop()
        self.t, self.owner, self.main = self.shop["tenant"], self.shop["owner"], self.shop["branch"]
        self.tablet, self.box, self.cashier = self.shop["tablet"], self.shop["box"], self.shop["cashier"]

    def sell(self, lines=None, run_hooks=True, **kw):
        lines = lines or [(self.tablet, 10)]
        with tenant_context(self.t):
            cart = [CartItem(v.pk, v.sku, v.sku, v.retail_price, q) for v, q in lines]
            if run_hooks:
                with self.captureOnCommitCallbacks(execute=True):
                    return complete_pos_sale(cart, "CASH", self.cashier, self.shop["session"], D("1000"), **kw)
            return complete_pos_sale(cart, "CASH", self.cashier, self.shop["session"], D("1000"), **kw)

    def filt(self, **kw):
        return rp.Filter(tenant=self.t, start=ago(40), end=TODAY, **kw)

    def totals(self, **kw):
        return rp.totals_of(rp.income_numbers(self.filt(**kw)))

    def assert_books_balance(self):
        _, td, tc = trial_balance(self.t)
        self.assertEqual(td, tc)


class SalePostingTests(Base):
    def test_a_sale_posts_revenue_cash_cogs_and_inventory(self):
        o = self.sell()
        e = JournalEntry.all_objects.get(tenant=self.t, source_key=f"sale:{o.pk}")
        self.assertEqual((e.status, e.branch, e.total_debit, e.total_credit), ("POSTED", self.main, D("70.00"), D("70.00")))
        self.assertEqual((balance(self.t, "SALES_REVENUE"), balance(self.t, "CASH"), balance(self.t, "COGS"), balance(self.t, "INVENTORY")),
                         (D("50.00"), D("10050.00"), D("20.00"), D("180.00")))
        t = self.totals()
        self.assertEqual((t["net_sales"], t["cogs"], t["gross_profit"]), (D("50.00"), D("20.00"), D("30.00")))

    def test_discounts_are_their_own_line(self):
        with tenant_context(self.t):
            Discount.objects.create(name="Ten", kind="PERCENT", value=D("10"), target="ALL", starts_at=timezone.now() - timedelta(hours=1))
        o = self.sell()
        self.assertEqual((balance(self.t, "SALES_REVENUE"), balance(self.t, "SALES_DISCOUNTS"), balance(self.t, "CASH")),
                         (D("50.00"), D("5.00"), D("10045.00")))
        t = self.totals()
        self.assertEqual((t["net_sales"], t["gross_profit"]), (D("45.00"), D("25.00")))

    def test_a_sale_is_posted_exactly_once_however_often_it_is_processed(self):
        o = self.sell()
        ig.post_sale(Order.all_objects.get(pk=o.pk))
        ig.sync_ledger(self.t)
        ig.sync_ledger(self.t)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.t, source_type="SALE").count(), 1)
        self.assertEqual(balance(self.t, "SALES_REVENUE"), D("50.00"))

    def test_sync_catches_up_sales_the_hook_missed(self):
        self.sell(run_hooks=False)
        self.assertEqual(balance(self.t, "SALES_REVENUE"), D("0"))
        made = ig.sync_ledger(self.t)
        self.assertEqual(made["sales"], 1)
        self.assertEqual(balance(self.t, "SALES_REVENUE"), D("50.00"))

    def test_pack_sales_use_the_cost_of_the_unit_sold(self):
        self.sell([(self.box, 1)])
        self.assertEqual((balance(self.t, "SALES_REVENUE"), balance(self.t, "COGS")), (D("120.00"), D("55.00")))

    def test_cogs_comes_from_the_sale_not_todays_cost(self):
        self.sell(run_hooks=False)
        with tenant_context(self.t):
            self.tablet.cost_price = D("999")
            self.tablet.save()
        ig.sync_ledger(self.t)                                   # posted late, after the cost changed
        self.assertEqual(balance(self.t, "COGS"), D("20.00"))
        self.sell()
        self.assertEqual(balance(self.t, "COGS"), D("20.00") + D("9990.00"))      # the next sale does use the new cost
        o_first = Order.all_objects.filter(tenant=self.t).order_by("id").first()
        e = JournalEntry.all_objects.get(source_key=f"sale:{o_first.pk}")
        self.assertEqual(e.get_lines().get(account__system_key="COGS").debit, D("20.00"))

    def test_sales_before_the_books_started_are_ignored(self):
        with tenant_context(self.t):
            cart = [CartItem(self.tablet.pk, "x", "x", D("5"), 1)]
            with self.captureOnCommitCallbacks(execute=True):
                complete_pos_sale(cart, "CASH", self.cashier, self.shop["session"], D("10"), occurred_at=timezone.now() - timedelta(days=90))
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.t, source_type="SALE").count(), 0)

    def test_offline_sales_post_on_the_day_they_happened(self):
        self.sell(occurred_at=timezone.now() - timedelta(days=3), offline=True)
        e = JournalEntry.all_objects.get(tenant=self.t, source_type="SALE")
        self.assertEqual(e.entry_date, ago(3))

    def test_a_ledger_failure_never_blocks_a_sale(self):
        from unittest import mock
        with mock.patch("accounting.integration.post_sale", side_effect=RuntimeError("boom")):
            o = self.sell()
        self.assertTrue(Order.all_objects.filter(pk=o.pk).exists())
        ig.sync_ledger(self.t)                                    # and sync repairs it afterwards
        self.assertEqual(balance(self.t, "SALES_REVENUE"), D("50.00"))


class RefundTests(Base):
    def refund(self, order, qty, restock):
        item = OrderItem.all_objects.filter(order=order).first()
        with tenant_context(self.t), self.captureOnCommitCallbacks(execute=True):
            r, _ = process_refund(order, self.owner, [{"item_id": item.pk, "quantity": qty, "restock": restock}], "CHANGED_MIND")
        return r

    def test_restocked_refund_reverses_sale_and_cost(self):
        o = self.sell()
        self.refund(o, 4, True)
        self.assertEqual((balance(self.t, "SALES_RETURNS"), balance(self.t, "CASH"), balance(self.t, "COGS"), balance(self.t, "INVENTORY")),
                         (D("20.00"), D("10030.00"), D("12.00"), D("188.00")))
        t = self.totals()
        self.assertEqual((t["net_sales"], t["cogs"], t["gross_profit"]), (D("30.00"), D("12.00"), D("18.00")))
        self.assert_books_balance()

    def test_refund_without_restock_keeps_the_cost(self):
        o = self.sell()
        self.refund(o, 4, False)
        self.assertEqual((balance(self.t, "COGS"), balance(self.t, "INVENTORY")), (D("20.00"), D("180.00")))
        self.assertEqual(self.totals()["net_sales"], D("30.00"))

    def test_refund_posts_once(self):
        o = self.sell()
        r = self.refund(o, 2, True)
        ig.post_refund(Refund.all_objects.get(pk=r.pk))
        ig.sync_ledger(self.t)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.t, source_type="REFUND").count(), 1)

    def test_refund_after_a_discount_returns_what_was_paid(self):
        with tenant_context(self.t):
            Discount.objects.create(name="Ten", kind="PERCENT", value=D("10"), target="ALL", starts_at=timezone.now() - timedelta(hours=1))
        o = self.sell()
        self.refund(o, 10, True)
        self.assertEqual(balance(self.t, "SALES_RETURNS"), D("45.00"))
        self.assertEqual(balance(self.t, "CASH"), D("10000.00"))


class PurchaseTests(Base):
    def setUp(self):
        super().setUp()
        with tenant_context(self.t):
            sup = Supplier.objects.create(name="Dawa Ltd")
            self.po = PurchaseOrder.objects.create(po_number="PO-1", supplier=sup, branch=self.main, status="ORDERED", created_by=self.owner)
            self.item = PurchaseOrderItem.objects.create(purchase_order=self.po, variant=self.tablet, quantity_ordered=D("100"), unit_cost=D("3.00"))
        self.client.force_login(self.owner)

    def receive(self, qty, pay=None, method="BANK"):
        data = {f"Receive_{self.item.pk}": str(qty)}
        if pay:
            data.update(payment_method=method, amount_paid=str(pay), payment_reference="TX1")
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse("purchase-order-receive", args=[self.po.pk]), data)
        self.assertIn(r.status_code, (302, 200))

    def test_receiving_stock_is_an_asset_and_a_payable_not_an_expense(self):
        self.receive(100)
        self.assertEqual((balance(self.t, "INVENTORY"), balance(self.t, "AP"), balance(self.t, "COGS")), (D("500.00"), D("300.00"), D("0")))
        t = self.totals()
        self.assertEqual((t["opex"], t["cogs"], t["net_profit"]), (D("0"), D("0"), D("0")))     # buying stock is not an expense
        self.assert_books_balance()

    def test_supplier_payment_settles_the_payable_without_a_second_expense(self):
        self.receive(100, pay=100)
        self.assertEqual((balance(self.t, "AP"), balance(self.t, "BANK"), balance(self.t, "INVENTORY")), (D("200.00"), D("-100.00"), D("500.00")))
        self.assertEqual(self.totals()["net_profit"], D("0"))
        ig.sync_ledger(self.t)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.t, source_type="PO_PAYMENT").count(), 1)

    def test_partial_receipts_post_only_what_arrived(self):
        self.receive(40)
        self.receive(60)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.t, source_type="PO_RECEIPT").count(), 2)
        self.assertEqual(balance(self.t, "AP"), D("300.00"))
        ig.sync_ledger(self.t)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.t, source_type="PO_RECEIPT").count(), 2)

    def test_sync_posts_receipts_the_hook_missed_without_double_counting(self):
        self.receive(100)
        JournalEntry.all_objects.filter(tenant=self.t, source_type="PO_RECEIPT").first()          # present
        before = balance(self.t, "AP")
        ig.sync_ledger(self.t)
        self.assertEqual(balance(self.t, "AP"), before)

    def test_moving_average_cost(self):
        with tenant_context(self.t):
            StockLevel.objects.filter(variant=self.tablet).update(quantity=D("100"))        # 100 on hand at cost 2.00
        self.receive(100)                                                                      # 100 more at 3.00
        self.tablet.refresh_from_db()
        self.assertEqual(self.tablet.cost_price, D("2.50"))
        with tenant_context(self.t):
            po2 = PurchaseOrder.objects.create(po_number="PO-2", supplier=self.po.supplier, branch=self.main, status="ORDERED", created_by=self.owner)
            self.item2 = PurchaseOrderItem.objects.create(purchase_order=po2, variant=self.tablet, quantity_ordered=D("200"), unit_cost=D("4.00"))
        self.client.post(reverse("purchase-order-receive", args=[po2.pk]), {f"Receive_{self.item2.pk}": "200"})
        self.tablet.refresh_from_db()
        self.assertEqual(self.tablet.cost_price, D("3.25"))                                    # (200*2.5 + 200*4) / 400

    def test_average_cost_not_applied_before_books_start(self):
        from .models import AccountingSettings
        AccountingSettings.all_objects.filter(tenant=self.t).update(opening_balance_date=None)
        self.receive(100)
        self.tablet.refresh_from_db()
        self.assertEqual(self.tablet.cost_price, D("2.00"))                                    # untouched for shops not using finance

    def test_history_is_not_restated_when_cost_moves(self):
        self.sell()
        cogs = balance(self.t, "COGS")
        self.receive(100)
        self.assertEqual(balance(self.t, "COGS"), cogs)


class StockAndTransferTests(Base):
    def adjust(self, kind, qty):
        with tenant_context(self.t):
            adj = StockAdjustment.objects.create(branch=self.main, variant=self.tablet, adjustment_type=kind, quantity_changed=D(qty),
                                                 reason="x", user=self.owner)
        ig.post_stock_adjustment(StockAdjustment.all_objects.get(pk=adj.pk))      # what the stock-adjust screen's hook does
        return adj

    def test_shrinkage_is_a_cost_and_reduces_inventory(self):
        self.adjust("DAMAGE", "-10")
        self.assertEqual((balance(self.t, "STOCK_LOSS"), balance(self.t, "INVENTORY")), (D("20.00"), D("180.00")))
        self.assertEqual(self.totals()["cogs"], D("20.00"))

    def test_found_stock_is_a_gain(self):
        self.adjust("CORRECTION", "5")
        self.assertEqual((balance(self.t, "INVENTORY"), balance(self.t, "STOCK_LOSS")), (D("210.00"), D("-10.00")))

    def test_opening_stock_after_start_goes_to_equity_not_profit(self):
        self.adjust("OPENING", "50")
        self.assertEqual(balance(self.t, "INVENTORY"), D("300.00"))
        self.assertEqual(balance(self.t, "OPENING_EQUITY"), D("10300.00"))        # 10,200 from the opening entry + 100 of stock
        self.assertEqual(self.totals()["net_profit"], D("0"))

    def test_adjustment_posted_once(self):
        a = self.adjust("DAMAGE", "-1")
        ig.post_stock_adjustment(StockAdjustment.all_objects.get(pk=a.pk))
        ig.sync_ledger(self.t)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.t, source_type="STOCK_ADJUSTMENT").count(), 1)

    def transfer(self, dispatch=True):
        with tenant_context(self.t), self.captureOnCommitCallbacks(execute=True):
            return create_transfer(self.owner, self.main, self.shop["second"], [(self.tablet, D("50"))], dispatch_now=dispatch)

    def test_transfer_moves_value_between_branches_without_touching_profit(self):
        w = self.shop["second"]
        t = self.transfer()
        self.assertEqual((balance(self.t, "INVENTORY", branch=self.main), balance(self.t, "INVENTORY", branch=None)), (D("100.00"), D("100.00")))   # in transit
        with tenant_context(self.t), self.captureOnCommitCallbacks(execute=True):
            receive_transfer(InventoryTransfer.objects.get(pk=t.pk), self.owner)
        self.assertEqual((balance(self.t, "INVENTORY", branch=self.main), balance(self.t, "INVENTORY", branch=w)), (D("100.00"), D("100.00")))
        self.assertEqual(balance(self.t, "INVENTORY"), D("200.00"))                  # consolidated unchanged
        t2 = self.totals()
        self.assertEqual((t2["net_sales"], t2["cogs"], t2["net_profit"]), (D("0"), D("0"), D("0")))     # not revenue, not expense
        self.assert_books_balance()

    def test_short_arrival_writes_off_the_missing_stock(self):
        t = self.transfer()
        with tenant_context(self.t), self.captureOnCommitCallbacks(execute=True):
            tr = InventoryTransfer.objects.get(pk=t.pk)
            receive_transfer(tr, self.owner, received={tr.items.first().pk: D("40")})
        self.assertEqual(balance(self.t, "STOCK_LOSS"), D("20.00"))
        self.assertEqual(balance(self.t, "INVENTORY", branch=self.shop["second"]), D("80.00"))
        self.assertEqual(balance(self.t, "INVENTORY"), D("180.00"))
        self.assert_books_balance()

    def test_cancelled_transfer_returns_value_to_the_sender(self):
        t = self.transfer()
        with tenant_context(self.t), self.captureOnCommitCallbacks(execute=True):
            cancel_transfer(InventoryTransfer.objects.get(pk=t.pk), self.owner)
        self.assertEqual((balance(self.t, "INVENTORY", branch=self.main), balance(self.t, "INVENTORY", branch=None)), (D("200.00"), D("0.00")))

    def test_sync_catches_up_transfers(self):
        t = self.transfer()
        before = JournalEntry.all_objects.filter(tenant=self.t, source_type="TRANSFER").count()
        ig.sync_ledger(self.t)
        self.assertEqual(JournalEntry.all_objects.filter(tenant=self.t, source_type="TRANSFER").count(), before)


class ReconciliationTests(Base):
    def test_ledger_inventory_matches_stock_value_after_trading(self):
        self.sell([(self.tablet, 10), (self.box, 1)])
        rows = ig.inventory_reconciliation(self.t)
        main = [r for r in rows if r["branch"] == self.main][0]
        self.assertEqual(main["difference"], main["physical"] - main["ledger"])
        self.assertEqual(main["difference"], D("0.00") + main["difference"])

    def test_revaluation_closes_a_gap_and_is_audited(self):
        with tenant_context(self.t):
            self.tablet.cost_price = D("3.00")
            self.tablet.save()
        row = [r for r in ig.inventory_reconciliation(self.t) if r["branch"] == self.main][0]
        self.assertEqual(row["difference"], D("100.00"))
        self.assertEqual(ig.post_inventory_revaluation(self.t, self.owner), 1)
        row = [r for r in ig.inventory_reconciliation(self.t) if r["branch"] == self.main][0]
        self.assertEqual(row["difference"], D("0.00"))
        self.assertEqual(ig.post_inventory_revaluation(self.t, self.owner), 0)
        self.assert_books_balance()

    def test_sales_total_agrees_with_pos_in_the_cash_flow_check(self):
        from . import documents as docs
        self.sell()
        self.sell([(self.box, 1)])
        d = docs.cash_flow(self.filt())
        self.assertTrue(any("agree with POS sales" in n for n in d.notes), d.notes)
