import io
import os
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image, ImageDraw

from billing.models import Subscription
from inventory.models import Branch, StockLevel
from products import images as imgs
from products.forms import ProductForm
from products.models import Category, Product, ProductVariant, UnitOfMeasure
from sales.models import CashRegisterSession
from tenants.context import tenant_context
from tenants.models import get_settings
from tenants.services import register_business

SIGNUP = dict(business_name="Mama Njeri Mart", business_type="MINI_MART", owner_name="Njeri W",
              email="njeri@example.com", phone="254712345678", password="S3cure-pass-99", term_months=3)


def photo(size=(3000, 2000), fmt="JPEG", mode="RGB"):
    """A smooth, photo-ish test image (noise would never compress and say nothing about real photos)."""
    img = Image.new(mode, size, (200, 220, 240) + ((255,) if mode == "RGBA" else ()))
    d = ImageDraw.Draw(img)
    d.ellipse((size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4), fill=(180, 30, 40))
    d.rectangle((0, size[1] - 200, size[0], size[1]), fill=(20, 90, 40))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def upload(data, name="p.jpg", ctype="image/jpeg"):
    return SimpleUploadedFile(name, data, content_type=ctype)


def make_shop(**over):
    tenant, owner, _ = register_business(**{**SIGNUP, **over})
    Subscription.objects.filter(tenant=tenant).update(comp_until=timezone.now() + timedelta(days=30))
    with tenant_context(tenant):
        branch = Branch.objects.first()
        cat = Category.objects.create(name="Pain", slug="pain")
        uom = UnitOfMeasure.objects.create(name="Tablet", short_name="tab")
        prod = Product.objects.create(name="Panadol", sku_prefix="PAN", category=cat, unit_of_measure=uom)
        variant = ProductVariant.objects.create(product=prod, sku="PAN-1", cost_price=Decimal("2"),
                                                retail_price=Decimal("5"))
        StockLevel.objects.create(branch=branch, variant=variant, quantity=Decimal("100"))
        CashRegisterSession.objects.create(branch=branch, opened_by=owner, opening_balance=Decimal("1000"))
    return dict(tenant=tenant, owner=owner, product=prod, variant=variant, cat=cat, uom=uom)


class CompressionTests(TestCase):
    def test_big_jpeg_is_shrunk_to_two_small_webp_files(self):
        source = photo()
        out = imgs.compress(upload(source))
        full, thumb = Image.open(io.BytesIO(out.full)), Image.open(io.BytesIO(out.thumb))
        self.assertEqual((full.format, thumb.format), ("WEBP", "WEBP"))
        self.assertLessEqual(max(full.size), imgs.FULL_BOX)
        self.assertLessEqual(max(thumb.size), imgs.THUMB_BOX)
        self.assertAlmostEqual(full.size[0] / full.size[1], 1.5, delta=0.01)   # aspect ratio kept (pixel rounding)
        self.assertLess(len(out.full), 100 * 1024)
        self.assertLess(len(out.thumb), 30 * 1024)
        self.assertLess(len(out.full) + len(out.thumb), len(source))

    def test_small_image_is_not_upscaled(self):
        out = imgs.compress(upload(photo((200, 100), "PNG"), "a.png", "image/png"))
        self.assertEqual(Image.open(io.BytesIO(out.full)).size, (200, 100))

    def test_transparency_is_flattened_onto_white(self):
        rgba = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
        buf = io.BytesIO()
        rgba.save(buf, "PNG")
        out = imgs.compress(upload(buf.getvalue(), "t.png", "image/png"))
        pixel = Image.open(io.BytesIO(out.full)).convert("RGB").getpixel((25, 25))
        self.assertTrue(all(c > 240 for c in pixel), pixel)

    def test_exif_rotation_is_applied(self):
        img = Image.new("RGB", (400, 200), (255, 0, 0))
        exif = Image.Exif()
        exif[0x0112] = 6                                               # "rotate 90 degrees"
        buf = io.BytesIO()
        img.save(buf, "JPEG", exif=exif)
        out = imgs.compress(upload(buf.getvalue()))
        self.assertEqual(Image.open(io.BytesIO(out.full)).size, (200, 400))

    def test_rejects_things_that_are_not_images(self):
        for data in (b"not an image at all", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", b""):
            with self.assertRaises(imgs.ImageRejected):
                imgs.compress(upload(data, "x.jpg"))

    def test_rejects_disallowed_formats_even_if_named_jpg(self):
        with self.assertRaises(imgs.ImageRejected):
            imgs.compress(upload(photo((50, 50), "GIF"), "x.jpg"))

    def test_rejects_oversize_upload_and_pixel_bombs(self):
        with self.assertRaises(imgs.ImageRejected):
            imgs.compress(upload(b"x" * (imgs.MAX_UPLOAD_BYTES + 1)))
        original = imgs.MAX_SOURCE_PIXELS
        imgs.MAX_SOURCE_PIXELS = 1000
        try:
            with self.assertRaises(imgs.ImageRejected):
                imgs.compress(upload(photo((100, 100), "PNG"), "b.png"))
        finally:
            imgs.MAX_SOURCE_PIXELS = original


class ProductImageTestBase(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)
        self.shop = make_shop()
        self.product = self.shop["product"]
        self.client.force_login(self.shop["owner"])

    def enable(self, on=True):
        with tenant_context(self.shop["tenant"]):
            s = get_settings(self.shop["tenant"])
            s.product_images_enabled = on
            s.save()

    def attach(self, data=None):
        """Put an image on the product the same way the form does."""
        with tenant_context(self.shop["tenant"]):
            p = Product.objects.get(pk=self.product.pk)
            imgs.set_product_image(p, imgs.compress(upload(data or photo())))
            return Product.objects.get(pk=p.pk)

    def files_on_disk(self):
        return sorted(os.path.join(r, f) for r, _, fs in os.walk(self.media) for f in fs)

    def form_data(self, **extra):
        return dict(name="Panadol", sku_prefix="PAN", category=self.shop["cat"].pk,
                    unit_of_measure=self.shop["uom"].pk, **extra)


class SettingsGateTests(ProductImageTestBase):
    def test_off_by_default_and_listed_on_settings_page(self):
        with tenant_context(self.shop["tenant"]):
            self.assertFalse(get_settings(self.shop["tenant"]).product_images_enabled)
        self.assertContains(self.client.get(reverse("settings")), "Product images")

    def test_form_has_no_image_fields_until_enabled(self):
        with tenant_context(self.shop["tenant"]):
            self.assertNotIn("image_upload", ProductForm().fields)
        self.enable()
        with tenant_context(self.shop["tenant"]):
            form = ProductForm()
            self.assertIn("image_upload", form.fields)
            self.assertIn("remove_image", form.fields)

    def test_upload_is_ignored_when_feature_is_off(self):
        with tenant_context(self.shop["tenant"]):
            form = ProductForm(self.form_data(), {"image_upload": upload(photo())},
                               instance=Product.objects.get(pk=self.product.pk))
            self.assertTrue(form.is_valid(), form.errors)
            self.assertIsNone(form.apply_image(form.instance))
        self.assertEqual(self.files_on_disk(), [])

    def test_admin_can_switch_it_on_from_the_settings_page(self):
        from tenants.forms import TenantSettingsForm
        with tenant_context(self.shop["tenant"]):
            s = get_settings(self.shop["tenant"])
            data = {f: getattr(s, f) for f in TenantSettingsForm.Meta.fields}
            data = {k: v for k, v in data.items() if v is not False}     # unchecked boxes are simply absent
            data["product_images_enabled"] = "on"
        r = self.client.post(reverse("settings"), data)
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:500])
        with tenant_context(self.shop["tenant"]):
            self.assertTrue(get_settings(self.shop["tenant"]).product_images_enabled)


class StorageTests(ProductImageTestBase):
    def setUp(self):
        super().setUp()
        self.enable()

    def test_database_stores_only_paths_not_bytes(self):
        p = self.attach()
        self.assertTrue(p.has_image)
        self.assertTrue(p.image.name.endswith(".webp") and p.thumb.name.endswith("-t.webp"))
        self.assertIn(f"product_images/{self.shop['tenant'].pk}/", p.image.name)
        self.assertEqual(len(self.files_on_disk()), 2)
        self.assertLess(max(os.path.getsize(f) for f in self.files_on_disk()), 100 * 1024)
        self.assertEqual(len(p.image_token), 12)

    def test_replacing_removes_old_files_after_commit(self):
        first = self.attach()
        before = set(self.files_on_disk())
        with self.captureOnCommitCallbacks(execute=True):
            self.attach(photo((1200, 900), "PNG"))
        after = set(self.files_on_disk())
        self.assertEqual(len(after), 2)
        self.assertFalse(before & after, "old files should be gone")
        with tenant_context(self.shop["tenant"]):
            self.assertNotEqual(Product.objects.get(pk=first.pk).image_token, first.image_token)

    def test_remove_image_clears_fields_and_files(self):
        self.attach()
        with tenant_context(self.shop["tenant"]):
            p = Product.objects.get(pk=self.product.pk)
            form = ProductForm(self.form_data(remove_image="on"), instance=p)
            self.assertTrue(form.is_valid(), form.errors)
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(form.apply_image(p), "removed")
            self.assertFalse(Product.objects.get(pk=p.pk).has_image)
        self.assertEqual(self.files_on_disk(), [])

    def test_bad_upload_is_a_form_error_not_a_crash(self):
        with tenant_context(self.shop["tenant"]):
            form = ProductForm(self.form_data(), {"image_upload": upload(b"nope")},
                               instance=Product.objects.get(pk=self.product.pk))
            self.assertFalse(form.is_valid())
            self.assertIn("image_upload", form.errors)


class ServingTests(ProductImageTestBase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.p = self.attach()

    def url(self, size="thumb", pk=None):
        return reverse("product-image", args=[pk or self.p.pk, size])

    def test_serves_webp_with_long_lived_cache_and_etag(self):
        r = self.client.get(self.url())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "image/webp")
        self.assertIn("immutable", r["Cache-Control"])
        thumb_bytes = b"".join(r.streaming_content)
        again = self.client.get(self.url(), HTTP_IF_NONE_MATCH=r["ETag"])
        self.assertEqual(again.status_code, 304)
        full = self.client.get(self.url("full"))
        self.assertGreater(len(b"".join(full.streaming_content)), len(thumb_bytes))

    def test_unknown_size_and_missing_image_are_404(self):
        self.assertEqual(self.client.get(self.url("huge")).status_code, 404)
        with tenant_context(self.shop["tenant"]):
            bare = Product.objects.create(name="Bare", sku_prefix="B", category=self.shop["cat"],
                                          unit_of_measure=self.shop["uom"])
        self.assertEqual(self.client.get(self.url(pk=bare.pk)).status_code, 404)

    def test_login_required(self):
        self.client.logout()
        self.assertIn(self.client.get(self.url()).status_code, (302, 401, 403))

    def test_another_shop_cannot_see_the_image(self):
        other = make_shop(business_name="Kamau Pharmacy", email="kamau@example.com", phone="254700111222")
        self.client.force_login(other["owner"])
        self.assertEqual(self.client.get(self.url()).status_code, 404)


class TerminalTests(ProductImageTestBase):
    def terminal(self, **extra):
        return self.client.get(reverse("pos_terminal"), **extra)

    def test_tiles_have_lazy_images_only_when_enabled(self):
        p = self.attach()
        r = self.terminal()
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'class="tile-img"')                  # feature off: nothing rendered
        self.enable()
        r = self.terminal()
        self.assertContains(r, f"/products/product/{p.pk}/image/thumb/?v={p.image_token}")
        self.assertContains(r, 'loading="lazy"')
        self.assertContains(r, 'decoding="async"')

    def test_product_without_photo_gets_an_initial_placeholder(self):
        self.enable()
        self.assertContains(self.terminal(), 'aria-hidden="true">P</span>')

    def test_ajax_search_results_carry_images_too(self):
        self.enable()
        p = self.attach()
        html = self.terminal(HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()["products_html"]
        self.assertIn(f"/products/product/{p.pk}/image/thumb/", html)
        self.assertIn('loading="lazy"', html)

    def test_offline_snapshot_includes_thumb_only_when_enabled(self):
        p = self.attach()
        snap = lambda: self.client.get("/pos/offline/snapshot/", HTTP_ACCEPT="application/json").json()
        self.assertNotIn("img", snap()["variants"][0])
        self.enable()
        self.assertEqual(snap()["variants"][0]["img"], p.thumb_url)

    def test_product_detail_shows_full_image_only_when_enabled(self):
        p = self.attach()
        url = reverse("product-detail", args=[p.pk])
        self.assertNotContains(self.client.get(url), "/image/full/")
        self.enable()
        self.assertContains(self.client.get(url), f"/products/product/{p.pk}/image/full/")


class ViewFlowTests(ProductImageTestBase):
    """The real create/update views with a multipart upload (not just the form)."""

    def formset(self, prefix, rows, initial):
        data = {f"{prefix}-TOTAL_FORMS": len(rows), f"{prefix}-INITIAL_FORMS": initial,
                f"{prefix}-MIN_NUM_FORMS": 0, f"{prefix}-MAX_NUM_FORMS": 1000}
        for i, row in enumerate(rows):
            data.update({f"{prefix}-{i}-{k}": v for k, v in row.items()})
        return data

    def update_payload(self, **extra):
        v = self.shop["variant"]
        row = dict(id=v.pk, product=self.product.pk, sku="PAN-1", size="", color="", cost_price="2.00",
                   retail_price="5.00", low_stock_threshold="10.000")
        return {**self.form_data(description="", **extra), **self.formset("variants", [row], 1),
                **self.formset("packs", [], 0)}

    def test_update_with_upload_saves_compressed_image(self):
        self.enable()
        url = reverse("product-update", args=[self.product.pk])
        page = self.client.get(url)
        self.assertContains(page, 'enctype="multipart/form-data"')
        self.assertContains(page, 'name="image_upload"')
        r = self.client.post(url, {**self.update_payload(), "image_upload": upload(photo())})
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:800])
        with tenant_context(self.shop["tenant"]):
            self.assertTrue(Product.objects.get(pk=self.product.pk).has_image)
        self.assertEqual(len(self.files_on_disk()), 2)

    def test_update_with_bad_file_redisplays_form_with_error(self):
        self.enable()
        r = self.client.post(reverse("product-update", args=[self.product.pk]),
                             {**self.update_payload(), "image_upload": upload(b"junk")})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "could not be read as an image")
        self.assertEqual(self.files_on_disk(), [])

    def test_update_page_has_no_image_field_when_disabled(self):
        page = self.client.get(reverse("product-update", args=[self.product.pk]))
        self.assertNotContains(page, 'name="image_upload"')

    def test_create_with_upload(self):
        self.enable()
        row = dict(sku="NEW-1", size="", color="", cost_price="1.00", retail_price="3.00",
                   low_stock_threshold="10.000")
        data = {**self.form_data(description=""), "name": "Brufen", "sku_prefix": "BRU",
                **self.formset("variants", [row], 0), **self.formset("packs", [], 0),
                "image_upload": upload(photo((1600, 1200)))}
        r = self.client.post(reverse("product-create"), data)
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:800])
        with tenant_context(self.shop["tenant"]):
            self.assertTrue(Product.objects.get(sku_prefix="BRU").has_image)
