
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Sum, Count, F
from django.db.models.functions import TruncHour
from django.utils import timezone

from sales.models import Order, OrderItem
from inventory.models import StockLevel
from .models import User, ActivityLog

CACHE_KEY = "pos:admin_dashboard:v1"
CACHE_TTL_SECONDS = 90


def _day_bounds(dt):
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def _period_kpis(start, end):
    agg = Order.objects.filter(created_at__gte=start, created_at__lt=end).aggregate(
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


def _payment_breakdown(start, end):
    return list(
        Order.objects.filter(created_at__gte=start, created_at__lt=end)
        .values("payment_method")
        .annotate(total=Sum("total_revenue"), count=Count("id"))
        .order_by("-total")
    )


def _hourly_trend(start, end):
    rows = (
        Order.objects.filter(created_at__gte=start, created_at__lt=end)
        .annotate(hour=TruncHour("created_at"))
        .values("hour")
        .annotate(revenue=Sum("total_revenue"), transactions=Count("id"))
        .order_by("hour")
    )
    return [
        {
            "hour": row["hour"].strftime("%H:00"),
            "revenue": float(row["revenue"] or 0),
            "transactions": row["transactions"],
        }
        for row in rows
    ]


def _top_categories(start, end, limit=6):
    rows = (
        OrderItem.objects.filter(order__created_at__gte=start, order__created_at__lt=end)
        .values(name=F("variant__product__category__name"))
        .annotate(revenue=Sum("revenue_line"))
        .order_by("-revenue")[:limit]
    )
    return [
        {"name": row["name"] or "Uncategorized", "revenue": float(row["revenue"] or 0)}
        for row in rows
    ]


def _top_products(start, end, limit=10):
    return list(
        OrderItem.objects.filter(order__created_at__gte=start, order__created_at__lt=end)
        .values(sku=F("variant__sku"), name=F("variant__product__name"))
        .annotate(units_sold=Sum("quantity"), revenue=Sum("revenue_line"))
        .order_by("-units_sold")[:limit]
    )


def _low_stock_alerts(limit=15):
    return list(
        StockLevel.objects.filter(quantity__lte=F("variant__low_stock_threshold"))
        .select_related("branch", "variant", "variant__product")
        .order_by("quantity")[:limit]
    )


def _recent_transactions(limit=10):
    return list(
        Order.objects.select_related("cashier", "branch").order_by("-created_at")[:limit]
    )


def _cashier_status(start, end):

    cashiers = list(User.objects.filter(role=User.Roles.CASHIER, is_active=True))
    if not cashiers:
        return []

    cashier_ids = [c.id for c in cashiers]

    sales_by_cashier = {
        row["cashier_id"]: row
        for row in Order.objects.filter(
            created_at__gte=start, created_at__lt=end, cashier_id__in=cashier_ids
        )
        .values("cashier_id")
        .annotate(total=Sum("total_revenue"), transactions=Count("id"))
    }

    last_actions = {}
    for row in (
        ActivityLog.objects.filter(user_id__in=cashier_ids)
        .order_by("-timestamp")
        .values("user_id", "action")[:500]
    ):
        last_actions.setdefault(row["user_id"], row["action"])

    results = []
    for cashier in cashiers:
        sales_row = sales_by_cashier.get(cashier.id, {})
        last_action = last_actions.get(cashier.id, "") or ""
        results.append(
            {
                "cashier": cashier,
                "shift_sales": sales_row.get("total") or Decimal("0"),
                "shift_transactions": sales_row.get("transactions") or 0,
                "is_logged_in": ("Login" in last_action) or ("PIN" in last_action.upper()),
            }
        )
    results.sort(key=lambda r: r["shift_sales"], reverse=True)
    return results


def build_dashboard_context(use_cache=True):
    if use_cache:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached

    now = timezone.now()
    today_start, today_end = _day_bounds(now)
    yesterday_start, yesterday_end = _day_bounds(now - timedelta(days=1))
    last_week_start, last_week_end = _day_bounds(now - timedelta(days=7))

    today_kpis = _period_kpis(today_start, today_end)
    yesterday_kpis = _period_kpis(yesterday_start, yesterday_end)
    last_week_kpis = _period_kpis(last_week_start, last_week_end)

    context = {
        "today_kpis": today_kpis,
        "kpi_deltas": {
            "gross_sales_vs_yesterday": _pct_change(today_kpis["gross_sales"], yesterday_kpis["gross_sales"]),
            "profit_vs_yesterday": _pct_change(today_kpis["profit"], yesterday_kpis["profit"]),
            "transactions_vs_yesterday": _pct_change(today_kpis["transactions"], yesterday_kpis["transactions"]),
            "gross_sales_vs_last_week": _pct_change(today_kpis["gross_sales"], last_week_kpis["gross_sales"]),
        },
        "payment_breakdown": _payment_breakdown(today_start, today_end),
        "hourly_trend": _hourly_trend(today_start, today_end),
        "top_categories": _top_categories(today_start, today_end),
        "top_products": _top_products(today_start, today_end),
        "low_stock_alerts": _low_stock_alerts(),
        "recent_transactions": _recent_transactions(),
        "cashier_status": _cashier_status(today_start, today_end),
        "generated_at": now,
    }

    if use_cache:
        cache.set(CACHE_KEY, context, CACHE_TTL_SECONDS)

    return context
