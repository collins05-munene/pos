import logging

from inventory.models import StockLevel
from products.models import fmt_qty
from .models import Notification
from .utils import notify, MANAGEMENT

log = logging.getLogger(__name__)


def check_stock_after_sale(tenant, branch_id, variants):
    try:
        base_ids = {v.base_variant_id or v.pk for v in variants}
        levels = (StockLevel.objects.filter(branch_id=branch_id, variant_id__in=base_ids)
                  .select_related("variant__product", "branch")
                  .prefetch_related("variant__packagings"))
        for lvl in levels:
            v = lvl.variant
            label = f"{v.product.name} ({v.sku})"
            if lvl.quantity <= 0:
                notify(tenant, f"Out of stock: {label}",
                       f"{label} is out of stock at {lvl.branch.name}.",
                       level=Notification.Level.ERROR, roles=MANAGEMENT, dedupe_hours=12)
            elif lvl.is_low_stock:
                notify(tenant, f"Low stock: {label}",
                       f"{lvl.branch.name} has {lvl.breakdown_display} left "
                       f"(alert level {fmt_qty(v.low_stock_threshold)} {v.unit_label}).",
                       level=Notification.Level.WARNING, roles=MANAGEMENT, dedupe_hours=12)
    except Exception:
        log.exception("stock check failed")