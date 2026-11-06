from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from inventory.models import Branch, StockLevel
from products.models import Brand, Category, Product, ProductVariant, UnitOfMeasure
from tenants.context import tenant_context
from tenants.models import Tenant
from tenants.services import register_business

from . import services
from .models import OnboardingProgress

SIGNUP = dict(business_name="Mama Njeri Mart", business_type="MINI_MART", owner_name="Njeri W",
              email="njeri@example.com", phone="254712345678", password="S3cure-pass-99", term_months=3)


def new_shop(**over):
    return register_business(**{**SIGNUP, **over})


class ProgressTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, _ = new_shop()

    def test_fresh_shop_starts_at_category_with_nothing_done(self):
        with tenant_context(self.tenant):
            p = services.get_progress()
        self.assertEqual([s.done for s in p.steps], [False, False, False, False])
        self.assertEqual(p.current.key, "category")
        self.assertTrue(p.show_banner)

    def test_seeded_general_category_does_not_count(self):
        with tenant_context(self.tenant):
            self.assertTrue(Category.objects.filter(slug="general").exists())
            self.assertFalse(services.get_progress().steps[0].done)

    def test_steps_complete_from_real_data(self):
        with tenant_context(self.tenant):
            cat, created = services.create_category("Pain relief")
            self.assertTrue(created)
            services.create_brand("Panadol")
            uom = UnitOfMeasure.objects.first()
            product, variant = services.create_first_product(
                name="Paracetamol 500mg", category=cat, brand=None, unit_of_measure=uom,
                cost_price=Decimal("2"), retail_price=Decimal("5"))
            p = services.get_progress()
            self.assertEqual([s.done for s in p.steps], [True, True, True, False])
            self.assertEqual(p.current.key, "stock")
            self.assertFalse(p.complete)

            StockLevel.objects.create(branch=Branch.objects.first(), variant=variant, quantity=Decimal("50"))
            self.assertTrue(services.record_if_complete().complete)
            self.assertIsNotNone(OnboardingProgress.objects.get().completed_at)
            self.assertFalse(services.get_progress().show_link)

    def test_existing_product_satisfies_category_and_brand_steps(self):
        with tenant_context(self.tenant):
            services.create_first_product(
                name="Soap", category=Category.objects.first(), brand=None,
                unit_of_measure=UnitOfMeasure.objects.first(), cost_price=1, retail_price=2)
            keys = {s.key: s.done for s in services.get_progress().steps}
        self.assertTrue(keys["category"] and keys["brand"] and keys["product"])
        self.assertFalse(keys["stock"])

    def test_skip_only_optional_steps(self):
        with tenant_context(self.tenant):
            services.skip_step("brand")
            self.assertTrue(services.get_progress().steps[1].done)
            with self.assertRaises(ValueError):
                services.skip_step("product")

    def test_duplicate_names_reuse_the_existing_row(self):
        with tenant_context(self.tenant):
            a, _ = services.create_category("Drinks")
            b, created = services.create_category("drinks")
            self.assertEqual(a.pk, b.pk)
            self.assertFalse(created)
            self.assertEqual(Category.objects.filter(name__iexact="drinks").count(), 1)

    def test_suggested_sku_is_unique(self):
        with tenant_context(self.tenant):
            cat = Category.objects.first()
            uom = UnitOfMeasure.objects.first()
            kw = dict(category=cat, brand=None, unit_of_measure=uom, cost_price=1, retail_price=2)
            p1, _ = services.create_first_product(name="Sugar 1kg", **kw)
            p2, _ = services.create_first_product(name="Sugar 1kg", **kw)
        self.assertEqual(p1.sku_prefix, "SUGAR-1KG")
        self.assertEqual(p2.sku_prefix, "SUGAR-1KG-2")

    def test_shops_do_not_share_progress(self):
        other, _, _ = new_shop(business_name="Kamau Pharmacy", email="kamau@example.com")
        with tenant_context(self.tenant):
            services.create_category("Pain relief")
            services.skip_step("brand")
        with tenant_context(other):
            p = services.get_progress()
        self.assertEqual([s.done for s in p.steps], [False, False, False, False])


@mock.patch.object(Tenant, "has_access", new_callable=mock.PropertyMock, return_value=True)
class ViewTests(TestCase):
    def setUp(self):
        self.tenant, self.owner, _ = new_shop()
        self.client.force_login(self.owner)

    def test_first_dashboard_visit_redirects_to_guide_once(self, _):
        r = self.client.get(reverse("admin-dashboard"))
        self.assertRedirects(r, reverse("onboarding:guide"), fetch_redirect_response=False)
        r = self.client.get(reverse("admin-dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Finish setting up your shop")

    def test_dismiss_hides_banner_but_keeps_sidebar_link(self, _):
        self.client.get(reverse("admin-dashboard"))              # uses up the one-time redirect
        self.client.post(reverse("onboarding:dismiss"))
        r = self.client.get(reverse("admin-dashboard"))
        self.assertNotContains(r, "Finish setting up your shop")
        self.assertContains(r, "Setup Guide")

    def test_guide_page_renders_the_current_step(self, _):
        r = self.client.get(reverse("onboarding:guide"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Add a category")
        self.assertContains(r, 'name="name"')

    def test_full_walkthrough(self, _):
        guide = reverse("onboarding:guide")
        r = self.client.post(reverse("onboarding:step", args=["category"]), {"name": "Pain relief"})
        self.assertRedirects(r, guide, fetch_redirect_response=False)
        r = self.client.post(reverse("onboarding:step", args=["brand"]), {"name": "Panadol"})
        self.assertRedirects(r, guide, fetch_redirect_response=False)

        with tenant_context(self.tenant):
            cat, brand = Category.objects.get(name="Pain relief"), Brand.objects.get(name="Panadol")
            uom = UnitOfMeasure.objects.get(short_name="pcs")
        r = self.client.post(reverse("onboarding:step", args=["product"]), {
            "name": "Paracetamol 500mg", "category": cat.pk, "brand": brand.pk, "unit_of_measure": uom.pk,
            "cost_price": "2.00", "retail_price": "5.00", "sku": ""})
        self.assertRedirects(r, guide, fetch_redirect_response=False)
        with tenant_context(self.tenant):
            variant = ProductVariant.objects.get(product__name="Paracetamol 500mg")
            self.assertEqual(variant.retail_price, Decimal("5.00"))
            self.assertEqual(Product.objects.get().brand, brand)

        page = self.client.get(guide)
        self.assertContains(page, "Record my first purchase")
        self.assertContains(page, f"variants={variant.pk}")

        with tenant_context(self.tenant):
            StockLevel.objects.create(branch=Branch.objects.first(), variant=variant, quantity=Decimal("20"))
        done = self.client.get(guide)
        self.assertContains(done, "all set")

    def test_invalid_product_shows_errors_and_creates_nothing(self, _):
        r = self.client.post(reverse("onboarding:step", args=["product"]), {"name": "X"})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "This field is required", status_code=400)
        with tenant_context(self.tenant):
            self.assertFalse(Product.objects.exists())

    def test_duplicate_sku_rejected(self, _):
        with tenant_context(self.tenant):
            services.create_first_product(name="Rice", category=Category.objects.first(), brand=None,
                                          unit_of_measure=UnitOfMeasure.objects.first(), cost_price=1, retail_price=2)
            cat, uom = Category.objects.first(), UnitOfMeasure.objects.first()
        r = self.client.post(reverse("onboarding:step", args=["product"]), {
            "name": "Beans", "category": cat.pk, "unit_of_measure": uom.pk,
            "cost_price": "1", "retail_price": "2", "sku": "rice"})
        self.assertEqual(r.status_code, 400)
        self.assertContains(r, "already used", status_code=400)

    def test_cannot_use_another_shops_category(self, _):
        other, _o, _i = new_shop(business_name="Kamau Pharmacy", email="kamau@example.com")
        with tenant_context(other):
            foreign = Category.objects.create(name="Foreign", slug="foreign")
        with tenant_context(self.tenant):
            uom = UnitOfMeasure.objects.first()
        r = self.client.post(reverse("onboarding:step", args=["product"]), {
            "name": "Beans", "category": foreign.pk, "unit_of_measure": uom.pk,
            "cost_price": "1", "retail_price": "2"})
        self.assertEqual(r.status_code, 400)
        with tenant_context(self.tenant):
            self.assertFalse(Product.objects.exists())

    def test_cashier_is_refused(self, _):
        from tenants.services import create_staff_user
        with tenant_context(self.tenant):
            cashier = create_staff_user(
                tenant=self.tenant, handle="wanjiru", role="CASHIER", branch=Branch.objects.first(),
                password="Another-pass-77", first_name="Wanjiru", last_name="K",
                email="w@example.com", phone="254700000000", national_id="12345678")
        self.client.force_login(cashier)
        self.assertEqual(self.client.get(reverse("onboarding:guide")).status_code, 403)
        self.assertEqual(self.client.post(reverse("onboarding:step", args=["category"]), {"name": "x1"}).status_code, 403)

    def test_skip_marks_step_and_moves_on(self, _):
        self.client.post(reverse("onboarding:skip", args=["category"]))
        with tenant_context(self.tenant):
            self.assertEqual(OnboardingProgress.objects.get().skipped_steps, ["category"])
        self.client.post(reverse("onboarding:skip", args=["product"]))       # not skippable: ignored
        with tenant_context(self.tenant):
            self.assertEqual(OnboardingProgress.objects.get().skipped_steps, ["category"])
