"""Numbers for the Financial Overview and Financial Analysis pages. All of it is computed from the ledger."""
import datetime
from collections import defaultdict
from decimal import Decimal

from django.db.models import Exists, OuterRef, Sum
from django.db.models.functions import TruncMonth

from inventory.models import Branch

from . import reporting as rp
from .documents import _change, _doc, _pct_str
from .models import JournalEntry, JournalLine
from .reporting import Row, Table, pct
from .services import ZERO

CASHISH = rp.CASH_KEYS


def _add_months(day, n):
    y, m = divmod(day.year * 12 + day.month - 1 + n, 12)
    return datetime.date(y, m + 1, 1)


def month_label(d):
    return d.strftime("%b %Y")


def monthly_series(f, months=12):
    """Per-month ledger figures for the `months` months ending with the month of the filter's end date."""
    end_month = min(f.end, datetime.date.today()).replace(day=1)
    first = _add_months(end_month, -(months - 1))
    last_day = _add_months(end_month, 1) - datetime.timedelta(days=1)
    keys = [_add_months(first, i) for i in range(months)]
    data = {k: defaultdict(lambda: ZERO) for k in keys}
    qs = rp.lines_qs(f, first, last_day).annotate(m=TruncMonth("entry__entry_date")) \
        .values("m", "account__system_key", "account__account_type").annotate(d=Sum("debit"), c=Sum("credit"))
    for r in qs:
        m = r["m"] if isinstance(r["m"], datetime.date) and not isinstance(r["m"], datetime.datetime) else r["m"].date()
        if m not in data:
            continue
        k, t, d, c = r["account__system_key"], r["account__account_type"], r["d"] or ZERO, r["c"] or ZERO
        row = data[m]
        if k == "SALES_REVENUE":
            row["gross"] += c - d
        elif k == "SALES_DISCOUNTS":
            row["disc"] += d - c
        elif k == "SALES_RETURNS":
            row["ret"] += d - c
        elif t == "REVENUE":
            row["other"] += c - d
        elif t == "COGS":
            row["cogs"] += d - c
        elif t == "OPEX":
            row["opex"] += d - c
        elif t == "OTHER_EXPENSE":
            row["fin"] += d - c
    # cash in / out, skipping moves between your own cash accounts
    ids = rp.cash_account_ids(f.tenant)
    other_side = JournalLine.all_objects.filter(entry_id=OuterRef("entry_id")).exclude(account_id__in=ids)
    cash = rp.lines_qs(f, first, last_day).filter(account_id__in=ids).exclude(entry__source_type="OPENING") \
        .annotate(has_other=Exists(other_side)).filter(has_other=True).annotate(m=TruncMonth("entry__entry_date")) \
        .values("m").annotate(d=Sum("debit"), c=Sum("credit"))
    for r in cash:
        m = r["m"] if not isinstance(r["m"], datetime.datetime) else r["m"].date()
        if m in data:
            data[m]["cash_in"] += r["d"] or ZERO
            data[m]["cash_out"] += r["c"] or ZERO
    out = []
    for k in keys:
        r = data[k]
        net_sales = r["gross"] - r["disc"] - r["ret"]
        revenue = net_sales + r["other"]
        costs = r["cogs"] + r["opex"] + r["fin"]
        out.append(dict(month=k, label=month_label(k), net_sales=net_sales, revenue=revenue, cogs=r["cogs"], opex=r["opex"],
                        expenses=costs, gross_profit=net_sales - r["cogs"], net_profit=revenue - costs,
                        cash_in=r["cash_in"], cash_out=r["cash_out"], net_cash=r["cash_in"] - r["cash_out"]))
    return out


def _f(v):
    return float(v)


def dashboard(f):
    accounts = rp.accounts_map(f.tenant)
    n = rp.income_numbers(f, accounts=accounts)
    t = rp.totals_of(n)
    as_of = min(f.end, datetime.date.today())
    bs = rp.balance_sheet_numbers(f, as_of)
    assets, liab = dict(bs["assets"]), dict(bs["liabilities"])
    cash = sum(rp.cash_balances(f, as_of=as_of).values(), ZERO)
    receivables = assets.get("Accounts receivable", ZERO)
    payables = liab.get("Accounts payable", ZERO) + liab.get("Outstanding expenses", ZERO)
    ps, pe = rp.previous_range(f.start, f.end)
    pt = rp.totals_of(rp.income_numbers(f, ps, pe, accounts))
    margin = pct(t["net_profit"], t["net_sales"])
    kpis = [
        ("Total revenue", t["total_revenue"], _change(t["total_revenue"], pt["total_revenue"])),
        ("Net sales", t["net_sales"], _change(t["net_sales"], pt["net_sales"])),
        ("Cost of goods sold", t["cogs"], _change(t["cogs"], pt["cogs"])),
        ("Gross profit", t["gross_profit"], _change(t["gross_profit"], pt["gross_profit"])),
        ("Operating expenses", t["opex"], _change(t["opex"], pt["opex"])),
        ("Operating profit", t["operating_profit"], _change(t["operating_profit"], pt["operating_profit"])),
        ("Profit before tax", t["pbt"], _change(t["pbt"], pt["pbt"])),
        ("Net profit", t["net_profit"], _change(t["net_profit"], pt["net_profit"])),
        ("Profit margin", f"{margin}%" if margin is not None else "n/a", ""),
        ("Cash balance", cash, "as at " + as_of.strftime("%d %b")),
        ("Receivables", receivables, "owed to you"),
        ("Payables", payables, "you owe"),
    ]
    series = monthly_series(f)
    cats = sorted(n["opex"], key=lambda r: -r[1])
    branches = []
    if f.branch_ids is None and not f.restricted:
        for b in Branch.all_objects.filter(tenant=f.tenant).order_by("name"):
            bt = rp.totals_of(rp.income_numbers(f.with_branches({b.pk}, b.name), accounts=accounts))
            branches.append((b.name, bt["net_profit"], bt["net_sales"]))
    recent = JournalEntry.all_objects.filter(tenant=f.tenant, status__in=rp.LIVE)
    if f.branch_ids is not None:
        recent = recent.filter(branch_id__in=f.branch_ids)
    recent = list(recent.order_by("-entry_date", "-id")[:10])
    for e in recent:
        e.amount = e.total_debit
    summary = []
    if t["net_sales"] or t["opex"]:
        verb = "up" if t["net_profit"] >= pt["net_profit"] else "down"
        summary.append(f"Net profit is {rp.fmt_money(t['net_profit'])}"
                       + (f" ({margin}% of net sales)" if margin is not None else "")
                       + f", {verb} from {rp.fmt_money(pt['net_profit'])} in the previous period.")
        if t["net_sales"]:
            summary.append(f"For every {f.currency} 100 of net sales, {rp.fmt_money(pct(t['cogs'], t['net_sales']))} went to stock "
                           f"and {rp.fmt_money(pct(t['opex'], t['net_sales']))} to running costs.")
    else:
        summary.append("No financial activity in this period yet.")
    charts = dict(
        labels=[s["label"] for s in series],
        revenue=[_f(s["revenue"]) for s in series], expenses=[_f(s["expenses"]) for s in series],
        gross=[_f(s["gross_profit"]) for s in series], net=[_f(s["net_profit"]) for s in series],
        sales=[_f(s["net_sales"]) for s in series], opex=[_f(s["opex"]) for s in series],
        cash_in=[_f(s["cash_in"]) for s in series], cash_out=[_f(s["cash_out"]) for s in series],
        cat_labels=[c[0] for c in cats[:8]], cat_values=[_f(c[1]) for c in cats[:8]],
        branch_labels=[b[0] for b in branches], branch_values=[_f(b[1]) for b in branches])
    return dict(kpis=kpis, charts=charts, recent=recent, summary=summary, totals=t, previous=pt,
                has_activity=bool(t["net_sales"] or t["opex"] or cash or receivables or payables))


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------
def analysis(f):
    accounts = rp.accounts_map(f.tenant)
    n = rp.income_numbers(f, accounts=accounts)
    t = rp.totals_of(n)
    ps, pe = rp.previous_range(f.start, f.end)
    pn = rp.income_numbers(f, ps, pe, accounts)
    pt = rp.totals_of(pn)
    ya_s, ya_e = rp.year_ago(f.start, f.end)
    yt = rp.totals_of(rp.income_numbers(f, ya_s, ya_e, accounts))

    ratio_rows = [
        Row(["Revenue growth", _change(t["net_sales"], pt["net_sales"]), ""]),
        Row(["Expense growth (operating)", _change(t["opex"], pt["opex"]), ""]),
        Row(["Gross profit margin", _pct_str(pct(t["gross_profit"], t["net_sales"])), _pct_str(pct(pt["gross_profit"], pt["net_sales"]))]),
        Row(["Net profit margin", _pct_str(pct(t["net_profit"], t["net_sales"])), _pct_str(pct(pt["net_profit"], pt["net_sales"]))]),
        Row(["Operating expense ratio", _pct_str(pct(t["opex"], t["net_sales"])), _pct_str(pct(pt["opex"], pt["net_sales"]))]),
        Row(["Cost of goods sold ratio", _pct_str(pct(t["cogs"], t["net_sales"])), _pct_str(pct(pt["cogs"], pt["net_sales"]))]),
    ]
    series = monthly_series(f, 12)
    last6 = series[-6:]
    month_rows = [Row([s["label"], s["net_sales"], s["gross_profit"], s["opex"], s["net_profit"]]) for s in last6]

    q_rows = []
    anchor = min(f.end, datetime.date.today())
    qs_, _ = rp.period_bounds("quarterly", anchor)
    for i in range(3, -1, -1):
        s = _add_months(qs_, -3 * i)
        e = _add_months(s, 3) - datetime.timedelta(days=1)
        qt = rp.totals_of(rp.income_numbers(f, s, e, accounts))
        q_rows.append(Row([f"Q{(s.month - 1) // 3 + 1} {s.year}", qt["net_sales"], qt["gross_profit"], qt["opex"], qt["net_profit"]]))

    yoy_rows = [Row([lbl, t[k], yt[k], _change(t[k], yt[k])]) for lbl, k in
                (("Net sales", "net_sales"), ("Gross profit", "gross_profit"), ("Operating expenses", "opex"), ("Net profit", "net_profit"))]

    cur, prev = dict(n["opex"]), dict(pn["opex"])
    cat_rows = [Row([k, cur.get(k, ZERO), prev.get(k, ZERO), _change(cur.get(k, ZERO), prev.get(k, ZERO))])
                for k in sorted(set(cur) | set(prev), key=lambda k: -cur.get(k, ZERO))]
    cash_rows = [Row([s["label"], s["cash_in"], s["cash_out"], s["net_cash"]]) for s in last6]

    tables = [Table("Key ratios", ["Measure", "This period", "Previous period"], ratio_rows),
              Table("Monthly comparison (last 6 months)", ["Month", "Net sales", "Gross profit", "Operating expenses", "Net profit"], month_rows, money_cols=(1, 2, 3, 4)),
              Table("Quarterly comparison", ["Quarter", "Net sales", "Gross profit", "Operating expenses", "Net profit"], q_rows, money_cols=(1, 2, 3, 4)),
              Table(f"Year over year (vs {ya_s:%d %b %Y} to {ya_e:%d %b %Y})", ["Measure", "This period", "Same period last year", "Change"], yoy_rows, money_cols=(1, 2)),
              Table("Expense categories", ["Category", "This period", "Previous period", "Change"], cat_rows, money_cols=(1, 2)),
              Table("Cash flow trend (actual)", ["Month", "Cash in", "Cash out", "Net cash"], cash_rows, money_cols=(1, 2, 3))]
    if f.branch_ids is None and not f.restricted:
        from .documents import branch_comparison
        tables.insert(1, branch_comparison(f, accounts))

    alerts = []
    for name, value in n["opex"]:
        base = prev.get(name, ZERO)
        if base > 0 and value >= base * Decimal("1.5") and (not t["net_sales"] or (value - base) >= t["net_sales"] * Decimal("0.01")):
            alerts.append(f"{name} rose {_change(value, base)} to {rp.fmt_money(value)} (was {rp.fmt_money(base)}).")
    if pt["net_profit"] > 0 and t["net_profit"] < pt["net_profit"] * Decimal("0.8"):
        alerts.append(f"Net profit fell {_change(t['net_profit'], pt['net_profit'])} compared with the previous period.")
    c_m, p_m = pct(t["net_profit"], t["net_sales"]), pct(pt["net_profit"], pt["net_sales"])
    if c_m is not None and p_m is not None and c_m <= p_m - 2:
        alerts.append(f"Net margin slipped from {p_m}% to {c_m}%.")
    if t["net_profit"] < 0:
        alerts.append("The business made a loss in this period.")

    d = _doc(f, "financial-analysis", "Financial Analysis", tables=tables, alerts=alerts)
    complete = [s["net_sales"] for s in series[:-1]][-3:]
    if len(complete) == 3 and all(v > 0 for v in complete):
        d.estimate = sum(complete, ZERO) / 3
        d.notes.append(f"ESTIMATE (not an actual): next month's net sales are expected to be about {rp.fmt_money(d.estimate)}, "
                       f"the average of the last three full months.")
    else:
        d.estimate = None
        d.notes.append("Not enough completed months of sales to estimate next month.")
    d.notes.append("Everything else on this page is actual figures from your ledger.")
    d.series = series
    return d
