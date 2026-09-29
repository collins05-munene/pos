from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from billing.models import BillingEvent, Invoice, Subscription
from tenants.models import Tenant


def platform_overview():
    now = timezone.now()
    S, I = Subscription.Status, Invoice.Status
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    by_status = {r["status"]: r["n"] for r in Subscription.objects.values("status").annotate(n=Count("id"))}
    paying = Subscription.objects.filter(status__in=[S.ACTIVE, S.PAST_DUE])
    mrr = paying.aggregate(t=Sum("monthly_equivalent"))["t"] or Decimal("0")

    paid = Invoice.objects.filter(status=I.PAID)
    revenue = lambda qs: qs.aggregate(total=Sum("total"), subs=Sum("subscription_amount"),
                                      installs=Sum("install_fee_amount"))
    monthly = (paid.filter(paid_at__gte=now - timedelta(days=365))
               .annotate(month=TruncMonth("paid_at")).values("month")
               .annotate(total=Sum("total")).order_by("month"))

    def rows(qs):
        return [{"tenant": s.tenant.name, "slug": s.tenant.slug, "phone": s.tenant.phone,
                 "status": s.status, "period_end": s.current_period_end}
                for s in qs.select_related("tenant")[:25]]

    return {
        "tenants_total": Tenant.objects.count(),
        "tenants_by_status": by_status,
        "mrr": mrr,
        "arr": mrr * 12,
        "revenue_this_month": revenue(paid.filter(paid_at__gte=month_start)),
        "revenue_all_time": revenue(paid),
        "revenue_by_month": list(monthly),
        "new_signups_30d": Tenant.objects.filter(created_at__gte=now - timedelta(days=30)).count(),
        "churned_30d": BillingEvent.objects.filter(
            type="subscription.deleted", created_at__gte=now - timedelta(days=30)).count(),
        "overdue": rows(Subscription.objects.filter(status__in=[S.PAST_DUE, S.SUSPENDED])
                        .order_by("current_period_end")),
        "expiring_7d": rows(Subscription.objects.filter(
            status=S.ACTIVE, current_period_end__lte=now + timedelta(days=7)).order_by("current_period_end")),
        "comped": Subscription.objects.filter(comp_until__gt=now).count(),
    }
