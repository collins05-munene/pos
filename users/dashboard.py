from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Sum, Count, F
from django.db.models.functions import TruncHour
from django.utils import timezone

from sales.models import Order, OrderItem
from inventory.models import StockLevel
from .models import User
from .dashboard_live import get_version

CACHE_TTL_SECONDS = 90

# A cashier counts as "active" if seen in the last 5 minutes, "idle" up to 30, else "offline".
ACTIVE_WINDOW = timedelta(minutes=5)
IDLE_WINDOW = timedelta(minutes=30)


def _cache_key(tenant):
    # Per tenant (never share), per live-version (a sale changes the key),
    # per local date (so "today" can't carry over past midnight).
    # v4: top_products now groups by product in base units.
    today = timezone.localdate().isoformat()
    return f"pos:admin_dashboard:v4:{tenant.pk}:{today}:{get_version(tenant.pk)}"


def _day_bounds(dt):
    # dt must be timezone-aware local time so "today" starts at local midnight.
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def _orders(tenant, start, end):
    return Order.objects.filter(
        tenant_id=tenant.pk, created_at__gte=start, created_at__lt=end
    )


def _order_items(tenant, start, end):
    # Joined lookups bypass tenant managers, so scope through the parent order.
    return OrderItem.objects.filter(
        order__tenant_id=tenant.pk,
        order__created_at__gte=start,
        order__created_at__lt=end,
    )


def _period_kpis(tenant, start, end):
    agg = _orders(tenant, start, end).aggregate(
        gross_sales=Sum("total_revenue"),
        cogs=Sum("total_cogs"),
        profit=Sum("total_profit"),
        transactions=Count("id"),
    )
    gross_sales = agg["gross_sales"] or Decimal("0")
    transactions = agg["transactions"] or 0
    atv = (gross_sales / transactions) if transactions else Decimal("0")
    return {
        "gross_sales": gross_sales,
        "cogs": agg["cogs"] or Decimal("0"),
        "profit": agg["profit"] or Decimal("0"),
        "transactions": transactions,
        "atv": atv,
    }


def _pct_change(current, previous):
    if not previous:
        return None
    return round(float((current - previous) / previous) * 100, 1)


def _payment_breakdown(tenant, start, end):
    return list(
        _orders(tenant, start, end)
        .values("payment_method")
        .annotate(total=Sum("total_revenue"), count=Count("id"))
        .order_by("-total")
    )


def _hourly_trend(tenant, start, end):
    rows = (
        _orders(tenant, start, end)
        .annotate(hour=TruncHour("created_at"))
        .values("hour")
        .annotate(revenue=Sum("total_revenue"), transactions=Count("id"))
        .order_by("hour")
    )
    return [
        {
            "hour": timezone.localtime(row["hour"]).strftime("%H:00"),
            "revenue": float(row["revenue"] or 0),
            "transactions": row["transactions"],
        }
        for row in rows
    ]


def _top_categories(tenant, start, end, limit=6):
    rows = (
        _order_items(tenant, start, end)
        .values(name=F("variant__product__category__name"))
        .annotate(revenue=Sum("revenue_line"))
        .order_by("-revenue")[:limit]
    )
    return [
        {"name": row["name"] or "Uncategorized", "revenue": float(row["revenue"] or 0)}
        for row in rows
    ]


def _top_products(tenant, start, end, limit=10):
    """
    Grouped by PRODUCT, not by variant row: a tablet, a packet and a box of the same
    medicine are one product, so they are added together. Quantities are converted to
    base units (tablets, kg ...) because "2 boxes + 5 tablets" can't be summed raw.
    Ranked by revenue (units of different products aren't comparable).

    Row keys are unchanged for the template, plus `unit`:
      sku (product SKU prefix), name, units_sold (base units), unit, revenue
    """
    return list(
        _order_items(tenant, start, end)
        .values(
            sku=F("variant__product__sku_prefix"),
            name=F("variant__product__name"),
            unit=F("variant__product__unit_of_measure__name"),
        )
        .annotate(
            units_sold=Sum(F("quantity") * F("variant__units_per_pack")),
            revenue=Sum("revenue_line"),
        )
        .order_by("-revenue")[:limit]
    )


def _low_stock_alerts(tenant, limit=15):
    # StockLevel rows exist only for base variants (selling units share their stock),
    # so no unit is double counted. `alert.breakdown_display` gives e.g. "2 Box, 1 Packet".
    return list(
        StockLevel.objects.filter(
            branch__tenant_id=tenant.pk,
            quantity__lte=F("variant__low_stock_threshold"),
        )
        .select_related("branch", "variant", "variant__product", "variant__product__unit_of_measure")
        .prefetch_related("variant__packagings")
        .order_by("quantity")[:limit]
    )


def _recent_transactions(tenant, limit=10):
    return list(
        Order.objects.filter(tenant_id=tenant.pk)
        .select_related("cashier", "branch")
        .order_by("-created_at")[:limit]
    )


def _cashier_status(tenant, start, end):
    # User.objects is unscoped (auth needs that). Always filter by tenant here.
    cashiers = list(
        User.objects.filter(tenant_id=tenant.pk, role=User.Roles.CASHIER, is_active=True)
    )
    if not cashiers:
        return []

    sales_by_cashier = {
        row["cashier_id"]: row
        for row in _orders(tenant, start, end)
        .filter(cashier_id__in=[c.id for c in cashiers])
        .values("cashier_id")
        .annotate(total=Sum("total_revenue"), transactions=Count("id"))
    }

    now = timezone.now()
    results = []
    for cashier in cashiers:
        sales_row = sales_by_cashier.get(cashier.id, {})
        age = (now - cashier.last_seen) if cashier.last_seen else None
        if age is not None and age <= ACTIVE_WINDOW:
            status = "active"
        elif age is not None and age <= IDLE_WINDOW:
            status = "idle"
        else:
            status = "offline"
        results.append({
            "cashier": cashier,
            "status": status,
            "shift_sales": sales_row.get("total") or Decimal("0"),
            "shift_transactions": sales_row.get("transactions") or 0,
        })
    results.sort(key=lambda r: r["shift_sales"], reverse=True)
    return results


def build_dashboard_context(tenant, use_cache=True):
    if tenant is None:
        raise ValueError("build_dashboard_context requires a tenant.")

    key = _cache_key(tenant)
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    now = timezone.localtime()   # local (Nairobi) time, not UTC
    today_start, today_end = _day_bounds(now)
    yesterday_start, yesterday_end = _day_bounds(now - timedelta(days=1))
    last_week_start, last_week_end = _day_bounds(now - timedelta(days=7))

    today_kpis = _period_kpis(tenant, today_start, today_end)
    yesterday_kpis = _period_kpis(tenant, yesterday_start, yesterday_end)
    last_week_kpis = _period_kpis(tenant, last_week_start, last_week_end)

    context = {
        "today_kpis": today_kpis,
        "kpi_deltas": {
            "gross_sales_vs_yesterday": _pct_change(today_kpis["gross_sales"], yesterday_kpis["gross_sales"]),
            "profit_vs_yesterday": _pct_change(today_kpis["profit"], yesterday_kpis["profit"]),
            "transactions_vs_yesterday": _pct_change(today_kpis["transactions"], yesterday_kpis["transactions"]),
            "gross_sales_vs_last_week": _pct_change(today_kpis["gross_sales"], last_week_kpis["gross_sales"]),
        },
        "payment_breakdown": _payment_breakdown(tenant, today_start, today_end),
        "hourly_trend": _hourly_trend(tenant, today_start, today_end),
        "top_categories": _top_categories(tenant, today_start, today_end),
        "top_products": _top_products(tenant, today_start, today_end),
        "low_stock_alerts": _low_stock_alerts(tenant),
        "recent_transactions": _recent_transactions(tenant),
        "cashier_status": _cashier_status(tenant, today_start, today_end),
        "generated_at": now,
    }

    if use_cache:
        cache.set(key, context, CACHE_TTL_SECONDS)

    return context