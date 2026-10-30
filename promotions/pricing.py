"""
promotions/pricing.py

The ONE place that decides what a product costs after discounts. The cart, the payment page,
checkout, offline sync and the product tiles on the till all ask a PriceBook, so they can never
disagree with each other.

Rules (all enforced here, never in the browser):

* Only discounts that are running (switched on, started, not ended) at the moment of the sale
  count, and only for the branch the sale is made at.
* A discount is worked out PER UNIT and rounded to the cent, so line total = net unit price x
  quantity exactly. That keeps receipts, reports and refunds (which hand back net unit price
  x quantity) free of rounding drift.
* If several discounts match, the best single one wins. They never stack.
* Hard limits from the shop's Settings are applied at sale time too, so tightening a limit
  immediately tightens every existing discount:
      - never more than `max_discount_percent` off a unit
      - with below_cost_policy == LIMIT, never below the unit's cost price
* With the master switch off, nothing is discounted.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db.models import Q
from django.utils import timezone

from tenants.models import TenantSettings, get_settings

from .models import Discount

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value):
    return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LinePrice:
    list_price: Decimal                 # per unit, before any discount
    discount_per_unit: Decimal          # per unit
    quantity: int
    discount: Optional[Discount] = None

    @property
    def net_price(self):
        return self.list_price - self.discount_per_unit

    @property
    def list_total(self):
        return self.list_price * self.quantity

    @property
    def discount_total(self):
        return self.discount_per_unit * self.quantity

    @property
    def net_total(self):
        return self.net_price * self.quantity

    @property
    def discount_name(self):
        return self.discount.name if self.discount is not None else ""

    @property
    def has_discount(self):
        return self.discount_per_unit > 0


class PriceBook:
    """
    Everything needed to price lines for one shop / branch / moment, loaded once
    (one query for the running discounts, one for category parents if any are needed).
    """

    def __init__(self, tenant=None, branch=None, at=None, settings: Optional[TenantSettings] = None):
        self.at = at or timezone.now()
        self._tenant = tenant
        self.branch_id = getattr(branch, "pk", branch)
        self.settings = settings or get_settings(tenant)
        self._parents = None
        self.discounts = self._load(tenant) if self.settings.discounts_enabled else []

    # -- loading -----------------------------------------------------------------------
    def _load(self, tenant):
        qs = Discount.all_objects.filter(tenant=tenant) if tenant is not None else Discount.objects.all()
        qs = (qs.filter(is_active=True, starts_at__lte=self.at)
                .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=self.at))
                .prefetch_related("products", "categories", "branches"))
        running = []
        for d in qs:
            branch_ids = {b.pk for b in d.branches.all()}
            if branch_ids and self.branch_id not in branch_ids:
                continue
            d._product_ids = {p.pk for p in d.products.all()}
            d._category_ids = {c.pk for c in d.categories.all()}
            running.append(d)
        return running

    def _category_parent(self, category_id):
        if self._parents is None:
            from products.models import Category
            qs = Category.all_objects.filter(tenant=self._tenant) if self._tenant is not None else Category.objects.all()
            self._parents = dict(qs.values_list("id", "parent_id"))
        return self._parents.get(category_id)

    # -- matching ----------------------------------------------------------------------
    def _matches(self, d, variant):
        if d.target == Discount.Target.ALL:
            return True
        if d.target == Discount.Target.PRODUCTS:
            return variant.product_id in d._product_ids
        if d.target == Discount.Target.CATEGORIES:
            cat = variant.product.category_id
            return cat in d._category_ids or self._category_parent(cat) in d._category_ids
        return False

    # -- pricing -----------------------------------------------------------------------
    def unit_discount(self, d, base, variant):
        """KSH off ONE unit priced at `base` under discount `d`, after all hard limits."""
        amount = base * Decimal(d.value) / 100 if d.kind == Discount.Kind.PERCENT else Decimal(d.value)
        amount = money(amount)
        amount = min(amount, money(base * Decimal(self.settings.max_discount_percent) / 100), base)
        if self.settings.below_cost_policy == TenantSettings.BelowCost.LIMIT:
            amount = min(amount, max(ZERO, base - Decimal(variant.cost_price)))
        return max(amount, ZERO)

    def price(self, variant, quantity, list_price=None):
        """
        Price `quantity` units of `variant`. `list_price` is the per-unit price the line was added
        at (the cart keeps it); it defaults to the variant's current retail price.
        """
        base = money(variant.retail_price if list_price is None else list_price)
        quantity = int(quantity)
        best, best_amount = None, ZERO
        for d in self.discounts:
            if quantity < d.min_quantity or not self._matches(d, variant):
                continue
            amount = self.unit_discount(d, base, variant)
            if amount > best_amount:
                best, best_amount = d, amount
        return LinePrice(list_price=base, discount_per_unit=best_amount, quantity=quantity, discount=best)

    def attach(self, variants):
        """Annotate catalogue variants (for the till tiles / offline snapshot) with their 1-unit price."""
        out = list(variants)
        for v in out:
            line = self.price(v, 1)
            v.net_price = line.net_price
            v.promo_name = line.discount_name
            v.has_promo = line.has_discount
        return out
