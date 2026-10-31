from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from products.models import Category, Product, ProductVariant, UnitOfMeasure
from tenants.models import Tenant

from .models import Branch, InventoryTransfer, StockLevel
from .stock import change_stock
from .transfers import (TransferError, cancel_transfer, create_transfer, dispatch_transfer,
                        receive_transfer)

User = get_user_model()


class TransferTestBase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name='Shop')
        self.user = User.objects.create_user('mgr', password='x')
        self.a = Branch.objects.create(name='Main', is_main=True, tenant=self.tenant)
        self.b = Branch.objects.create(name='Westlands', tenant=self.tenant)
        uom = UnitOfMeasure.objects.create(name='Tablet', short_name='tab', tenant=self.tenant)
        cat = Category.objects.create(name='Meds', slug='meds', tenant=self.tenant)
        prod = Product.objects.create(name='Panadol', sku_prefix='PAN', category=cat,
                                      unit_of_measure=uom, tenant=self.tenant)
        self.tab = ProductVariant.objects.create(product=prod, sku='PAN-T', cost_price=1,
                                                 retail_price=2, tenant=self.tenant)
        self.box = ProductVariant.objects.create(product=prod, sku='PAN-B', cost_price=30,
                                                 retail_price=50, unit_name='Box', base_variant=self.tab,
                                                 units_per_pack=30, tenant=self.tenant)
        change_stock(self.a, self.tab, 100)

    def qty(self, branch):
        row = StockLevel.objects.filter(branch=branch, variant=self.tab).first()
        return row.quantity if row else Decimal('0')


class TransferServiceTests(TransferTestBase):
    def test_pending_moves_no_stock(self):
        t = create_transfer(self.user, self.a, self.b, [(self.tab, 10)])
        self.assertEqual(t.status, 'PENDING')
        self.assertEqual(self.qty(self.a), 100)

    def test_full_lifecycle_converts_units(self):
        t = create_transfer(self.user, self.a, self.b, [(self.box, 2), (self.tab, 5)], dispatch_now=True)
        self.assertEqual(t.status, 'IN_TRANSIT')
        self.assertEqual(self.qty(self.a), 100 - 60 - 5)      # 2 boxes of 30 + 5 tablets
        self.assertEqual(self.qty(self.b), 0)                 # on the road: counted nowhere
        receive_transfer(t, self.user)
        t.refresh_from_db()
        self.assertEqual(t.status, 'COMPLETED')
        self.assertEqual(self.qty(self.b), 65)
        self.assertEqual(self.qty(self.a), 35)

    def test_short_receipt(self):
        t = create_transfer(self.user, self.a, self.b, [(self.tab, 10)], dispatch_now=True)
        item = t.items.get()
        receive_transfer(t, self.user, {item.pk: Decimal('7')})
        item.refresh_from_db()
        self.assertEqual(item.shortfall, 3)
        self.assertEqual(self.qty(self.b), 7)
        self.assertEqual(self.qty(self.a), 90)

    def test_cannot_receive_more_than_sent(self):
        t = create_transfer(self.user, self.a, self.b, [(self.tab, 10)], dispatch_now=True)
        with self.assertRaises(TransferError):
            receive_transfer(t, self.user, {t.items.get().pk: Decimal('11')})
        self.assertEqual(self.qty(self.b), 0)

    def test_insufficient_stock_rolls_everything_back(self):
        with self.assertRaises(TransferError) as cm:
            create_transfer(self.user, self.a, self.b, [(self.tab, 50), (self.box, 4)], dispatch_now=True)
        self.assertIn('Not enough', str(cm.exception))
        self.assertEqual(self.qty(self.a), 100)
        self.assertEqual(InventoryTransfer.objects.count(), 0)

    def test_pending_dispatch_failure_leaves_it_pending(self):
        t = create_transfer(self.user, self.a, self.b, [(self.tab, 500)])
        with self.assertRaises(TransferError):
            dispatch_transfer(t, self.user)
        t.refresh_from_db()
        self.assertEqual(t.status, 'PENDING')
        self.assertEqual(self.qty(self.a), 100)

    def test_no_double_dispatch_or_receive(self):
        t = create_transfer(self.user, self.a, self.b, [(self.tab, 10)], dispatch_now=True)
        with self.assertRaises(TransferError):
            dispatch_transfer(t, self.user)
        receive_transfer(t, self.user)
        with self.assertRaises(TransferError):
            receive_transfer(t, self.user)
        self.assertEqual(self.qty(self.b), 10)

    def test_cancel_in_transit_returns_stock(self):
        t = create_transfer(self.user, self.a, self.b, [(self.tab, 10)], dispatch_now=True)
        cancel_transfer(t, self.user)
        self.assertEqual(self.qty(self.a), 100)
        with self.assertRaises(TransferError):
            cancel_transfer(t, self.user)

    def test_cancel_pending_touches_no_stock(self):
        t = create_transfer(self.user, self.a, self.b, [(self.tab, 10)])
        cancel_transfer(t, self.user)
        self.assertEqual(self.qty(self.a), 100)

    def test_validation(self):
        with self.assertRaises(TransferError):
            create_transfer(self.user, self.a, self.a, [(self.tab, 1)])
        with self.assertRaises(TransferError):
            create_transfer(self.user, self.a, self.b, [])
        with self.assertRaises(TransferError):
            create_transfer(self.user, self.a, self.b, [(self.tab, 0)])
        with self.assertRaises(TransferError):
            create_transfer(self.user, self.a, self.b, [(self.tab, 1), (self.tab, 2)])
        self.b.is_active = False
        self.b.save()
        with self.assertRaises(TransferError):
            create_transfer(self.user, self.a, self.b, [(self.tab, 1)])


class TransferViewTests(TransferTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _post_create(self, **extra):
        data = {
            'from_branch': self.a.pk, 'to_branch': self.b.pk, 'remarks': 'van 1',
            'items-TOTAL_FORMS': '1', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '1', 'items-MAX_NUM_FORMS': '1000',
            'items-0-variant': self.box.pk, 'items-0-quantity': '1',
        }
        data.update(extra)
        return self.client.post(reverse('transfer-create'), data)

    def test_pages_render(self):
        self.assertEqual(self.client.get(reverse('transfer-create')).status_code, 200)

    def test_create_send_receive_flow(self):
        r = self._post_create(dispatch_now='on')
        t = InventoryTransfer.objects.get()
        self.assertRedirects(r, reverse('transfer-detail', args=[t.pk]))
        self.assertEqual(t.status, 'IN_TRANSIT')
        self.assertEqual(self.client.get(reverse('transfer-detail', args=[t.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('transfer-receive', args=[t.pk])).status_code, 200)
        self.client.post(reverse('transfer-receive', args=[t.pk]), {f'Receive_{t.items.get().pk}': '1'})
        t.refresh_from_db()
        self.assertEqual(t.status, 'COMPLETED')
        self.assertEqual(self.qty(self.b), 30)
        self.assertEqual(self.client.get(reverse('transfer-detail', args=[t.pk])).status_code, 200)

    def test_draft_then_dispatch_then_cancel(self):
        self._post_create()
        t = InventoryTransfer.objects.get()
        self.assertEqual(t.status, 'PENDING')
        self.assertEqual(self.client.get(reverse('transfer-detail', args=[t.pk])).status_code, 200)
        self.client.post(reverse('transfer-dispatch', args=[t.pk]))
        self.assertEqual(self.qty(self.a), 70)
        self.client.post(reverse('transfer-cancel', args=[t.pk]))
        self.assertEqual(self.qty(self.a), 100)

    def test_insufficient_stock_shows_error_and_creates_nothing(self):
        r = self._post_create(dispatch_now='on', **{'items-0-quantity': '10'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Not enough')
        self.assertEqual(InventoryTransfer.objects.count(), 0)

    def test_same_branch_rejected(self):
        r = self._post_create(to_branch=self.a.pk)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(InventoryTransfer.objects.count(), 0)

    def test_post_only_actions_reject_get(self):
        t = create_transfer(self.user, self.a, self.b, [(self.tab, 1)])
        self.assertEqual(self.client.get(reverse('transfer-dispatch', args=[t.pk])).status_code, 405)
