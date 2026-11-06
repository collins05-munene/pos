"""
Setup-guide logic. Everything here runs inside the active tenant (the request's, or `tenant_context`
in tests/commands), because every query goes through the tenant-scoped default managers.

Progress is DERIVED from the shop's data, never stored (see models.OnboardingProgress):

    category  done when the owner added a category of their own (the seeded "General" doesn't count),
              skipped it, or already has a product (a product needs one)
    brand     done when a brand exists, was skipped, or a product exists (brands are optional)
    product   done when the shop has a product
    stock     done when something is in stock, or the owner chose to do it later
"""
import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .models import OnboardingProgress

STEP_ORDER = ("category", "brand", "product", "stock")
SKIPPABLE = frozenset({"category", "brand", "stock"})      # a shop can't sell without a product, so that one isn't

STEP_TITLES = {
    "category": "Add a category",
    "brand": "Add a brand",
    "product": "Add your first product",
    "stock": "Bring it into stock",
}


@dataclass
class Step:
    key: str
    title: str
    done: bool
    skipped: bool
    skippable: bool
    current: bool = False
    number: int = 0


@dataclass
class Progress:
    steps: list = field(default_factory=list)
    welcomed: bool = False
    dismissed: bool = False
    completed_before: bool = False      # completion was already recorded on the row

    @property
    def total(self):
        return len(self.steps)

    @property
    def done_count(self):
        return sum(1 for s in self.steps if s.done)

    @property
    def complete(self):
        return self.completed_before or all(s.done for s in self.steps)

    @property
    def percent(self):
        return int(round(100 * self.done_count / self.total)) if self.total else 100

    @property
    def current(self):
        return next((s for s in self.steps if s.current), None)

    @property
    def show_banner(self):
        """Dashboard reminder: until finished, unless the owner hid it."""
        return not self.complete and not self.dismissed

    @property
    def show_link(self):
        """Sidebar link: until finished, even after the reminder was hidden."""
        return not self.complete


def _row():
    return OnboardingProgress.objects.first()


def _done_flags(skipped):
    from inventory.models import StockLevel
    from products.models import Brand, Category, Product

    has_product = Product.objects.exists()
    return {
        "category": has_product or "category" in skipped
                    or Category.objects.exclude(slug="general").exists(),
        "brand": has_product or "brand" in skipped or Brand.objects.exists(),
        "product": has_product,
        "stock": "stock" in skipped or StockLevel.objects.filter(quantity__gt=0).exists(),
    }


def get_progress(tenant=None):
    """Where this shop is in the guide. `tenant` is accepted for readability; the active one is used."""
    row = _row()
    if row is not None and row.completed_at is not None:
        # Finished long ago: no counting queries on every page.
        steps = [Step(k, STEP_TITLES[k], True, False, k in SKIPPABLE, number=i + 1)
                 for i, k in enumerate(STEP_ORDER)]
        return Progress(steps=steps, welcomed=True, dismissed=True, completed_before=True)

    skipped = set((row.skipped_steps if row else None) or [])
    flags = _done_flags(skipped)
    steps = [Step(key, STEP_TITLES[key], flags[key], key in skipped and key in SKIPPABLE,
                  key in SKIPPABLE, number=i + 1)
             for i, key in enumerate(STEP_ORDER)]
    for s in steps:
        if not s.done:
            s.current = True
            break
    return Progress(steps=steps,
                    welcomed=bool(row and row.welcomed_at),
                    dismissed=bool(row and row.dismissed_at))


def _row_for_update():
    row, _ = OnboardingProgress.objects.get_or_create()
    return row


def record_if_complete():
    """Remember that the guide is finished so later pages skip the checks. Returns the Progress."""
    progress = get_progress()
    if progress.complete and not progress.completed_before:
        row = _row_for_update()
        row.completed_at = timezone.now()
        row.save(update_fields=["completed_at"])
    return progress


def skip_step(key):
    if key not in SKIPPABLE:
        raise ValueError(f"'{key}' can't be skipped.")
    row = _row_for_update()
    if key not in row.skipped_steps:
        row.skipped_steps = [*row.skipped_steps, key]
        row.save(update_fields=["skipped_steps"])


def dismiss_reminder():
    row = _row_for_update()
    row.dismissed_at = timezone.now()
    row.save(update_fields=["dismissed_at"])


def restore_reminder():
    row = _row_for_update()
    row.dismissed_at = None
    row.save(update_fields=["dismissed_at"])


def mark_welcomed():
    row = _row_for_update()
    if row.welcomed_at is None:
        row.welcomed_at = timezone.now()
        row.save(update_fields=["welcomed_at"])


def welcome_redirect(request):
    """
    Called when a shop's owner/admin lands on the dashboard. The FIRST time, while setup is
    unfinished, returns the guide's URL (once only); otherwise None.
    """
    user, tenant = request.user, getattr(request, "tenant", None)
    if tenant is None or user.is_superuser:
        return None
    if not (getattr(user, "is_owner", False) or getattr(user, "role", None) == "ADMIN"):
        return None
    progress = get_progress(tenant)
    if progress.complete or progress.dismissed or progress.welcomed:
        return None
    mark_welcomed()
    return reverse("onboarding:guide")


# --------------------------------------------------------------------------------------------------
# Creating things from the guide. Same rules as the full screens (per-shop uniqueness, slugs, SKUs).
# --------------------------------------------------------------------------------------------------
def create_category(name):
    """(category, created). An existing category with the same name is reused, as in the quick-create."""
    from products.models import Category

    existing = Category.objects.filter(name__iexact=name).first()
    if existing:
        return existing, False
    base = slugify(name) or "category"
    slug, i = base, 2
    while Category.objects.filter(slug=slug).exists():
        slug = f"{base}-{i}"
        i += 1
    return Category.objects.create(name=name, slug=slug), True


def create_brand(name, description=""):
    from products.models import Brand

    existing = Brand.objects.filter(name__iexact=name).first()
    if existing:
        return existing, False
    return Brand.objects.create(name=name, description=description), True


def sku_in_use(sku):
    from products.models import Product, ProductVariant
    return (Product.objects.filter(sku_prefix__iexact=sku).exists()
            or ProductVariant.objects.filter(sku__iexact=sku).exists())


def suggest_sku(name):
    """PARACETAMOL 500mg -> PARACETAMOL-500MG, with -2, -3 ... if that is taken."""
    base = re.sub(r"-{2,}", "-", re.sub(r"[^A-Z0-9]+", "-", name.upper())).strip("-")[:30].strip("-") or "ITEM"
    sku, i = base, 2
    while sku_in_use(sku):
        sku = f"{base}-{i}"
        i += 1
    return sku


@transaction.atomic
def create_first_product(*, name, category, brand, unit_of_measure, cost_price, retail_price,
                         sku="", low_stock_threshold=Decimal("10")):
    """One product with one variant (no sizes/colours, no extra selling units): the simple case."""
    from products.models import Product, ProductVariant

    sku = (sku or "").strip() or suggest_sku(name)
    product = Product.objects.create(name=name, sku_prefix=sku, category=category, brand=brand,
                                     unit_of_measure=unit_of_measure, has_variations=False)
    variant = ProductVariant.objects.create(product=product, sku=sku, cost_price=cost_price,
                                            retail_price=retail_price,
                                            low_stock_threshold=low_stock_threshold)
    return product, variant


def stock_link_for_latest_product():
    """Purchase screen with the newest product's main variant preselected (what the product screen does)."""
    from products.models import ProductVariant

    ids = list(ProductVariant.objects.filter(is_active=True, base_variant__isnull=True)
               .order_by("-pk").values_list("pk", flat=True)[:1])
    base = reverse("purchase-order-create")
    return f"{base}?variants={ids[0]}" if ids else base
