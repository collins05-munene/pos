"""Builds the Doc objects shown on screen and exported to PDF/Excel. One builder per report."""
import datetime
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from inventory.models import Branch, PurchaseOrder
from sales.models import Order

from . import reporting as rp
from .models import (Account, Expense, ExpensePayment, FixedAsset, JournalLine, Loan, OtherIncome)
from .reporting import Doc, Row, Table, fmt_money, pct
from .services import ZERO, get_account

N = Decimal


def _doc(f, key, title, **kw):
    return Doc(key=key, title=title, tenant=f.tenant, branch_label=f.branch_label, period_label=kw.pop("period_label", f.period_label),
               basis_label=kw.pop("basis_label", f.basis_label), currency=f.currency, **kw)


def _change(cur, prev):
    if prev in (None, 0):
        return "n/a" if cur else "-"
    return f"{(cur - prev) / abs(prev) * 100:+.1f}%"


def _pct_str(v):
    return "n/a" if v is None else f"{v}%"


# --------------------------------------------------------------------------
# Profit & Loss
# --------------------------------------------------------------------------
def profit_loss(f, compare=True, **_):
    accounts = rp.accounts_map(f.tenant)
    cur = rp.income_numbers(f, accounts=accounts)
    ct = rp.totals_of(cur)
    prev = pt = None
    if compare:
        ps, pe = rp.previous_range(f.start, f.end)
        prev = rp.income_numbers(f, ps, pe, accounts)
        pt = rp.totals_of(prev)
    pl = f"{ps:%d %b %Y} to {pe:%d %b %Y}" if compare else ""
    cols = ["", "This period"] + (["Previous period", "Change"] if compare else [])
    rows = []

    def add(label, c, p, style="normal", indent=0):
        cells = [label, c] + ([p, _change(c, p)] if compare else [])
        rows.append(Row(cells, style, indent))

    def head(label):
        rows.append(Row([label] + [""] * (len(cols) - 1), "section"))

    def merge(key):
        names = sorted({n for n, _ in cur[key]} | ({n for n, _ in prev[key]} if prev else set()))
        cd, pd_ = dict(cur[key]), dict(prev[key]) if prev else {}
        return [(n, cd.get(n, ZERO), pd_.get(n, ZERO)) for n in names]

    P = lambda k: (prev[k] if prev else ZERO)
    PT = lambda k: (pt[k] if pt else ZERO)
    head("REVENUE")
    add("Gross sales", cur["gross_sales"], P("gross_sales"), indent=1)
    add("Less: sales discounts", -cur["discounts"], -P("discounts"), indent=1)
    add("Less: sales returns", -cur["returns"], -P("returns"), indent=1)
    add("Net sales", ct["net_sales"], PT("net_sales"), "subtotal")
    head("COST OF GOODS SOLD")
    cogs_rows = merge("cogs") or [("Cost of inventory sold", ZERO, ZERO)]
    for n, c, p in cogs_rows:
        add(n, c, p, indent=1)
    add("Total cost of goods sold", ct["cogs"], PT("cogs"), "subtotal")
    add("GROSS PROFIT", ct["gross_profit"], PT("gross_profit"), "total")
    head("OPERATING EXPENSES")
    for n, c, p in merge("opex"):
        add(n, c, p, indent=1)
    add("Total operating expenses", ct["opex"], PT("opex"), "subtotal")
    add("OPERATING PROFIT", ct["operating_profit"], PT("operating_profit"), "total")
    head("OTHER INCOME AND EXPENSES")
    for n, c, p in merge("other_income"):
        add(n, c, p, indent=1)
    for n, c, p in merge("finance"):
        add(n, -c, -p, indent=1)
    add("Net other income / (expense)", ct["other_income"] - ct["finance"], PT("other_income") - PT("finance"), "subtotal")
    add("PROFIT BEFORE TAX", ct["pbt"], PT("pbt"), "total")
    for n, c, p in merge("tax"):
        add("Tax expense: " + n, -c, -p, indent=1)
    if not cur["tax"] and not (prev and prev["tax"]):
        add("Tax expense", ZERO, ZERO, indent=1)
    add("NET PROFIT AFTER TAX", ct["net_profit"], PT("net_profit"), "total")

    mrows = []
    for label, num in (("Gross profit margin", "gross_profit"), ("Operating profit margin", "operating_profit"),
                       ("Net profit margin", "net_profit")):
        c = pct(ct[num], ct["net_sales"])
        p = pct(pt[num], pt["net_sales"]) if pt else None
        mrows.append(Row([label, _pct_str(c)] + ([_pct_str(p), ""] if compare else [])))
    tables = [Table("Profit and loss statement", cols, rows, money_cols=(1, 2) if compare else (1,)),
              Table("Margins", cols, mrows)]

    if f.branch_ids is None and not f.restricted:
        tables.append(branch_comparison(f, accounts))
    d = _doc(f, "profit-loss", "Profit & Loss Statement", tables=tables)
    if compare:
        d.notes.append(f"Previous period: {pl}.")
    if f.basis == "CASH":
        d.notes.append("Cash basis: expenses count when paid and other income when received. Depreciation is a non-cash "
                       "charge and is left out. Sales and cost of goods sold are the same on both bases because every "
                       "sale is paid at the till.")
    else:
        d.notes.append("Accrual basis: income and expenses count when earned or incurred, whether or not money has moved.")
    d.totals = ct
    return d


def branch_comparison(f, accounts=None):
    accounts = accounts or rp.accounts_map(f.tenant)
    rows, sums = [], dict(net_sales=ZERO, gross_profit=ZERO, opex=ZERO, operating_profit=ZERO, net_profit=ZERO)
    whole = rp.totals_of(rp.income_numbers(f, accounts=accounts))
    for b in Branch.all_objects.filter(tenant=f.tenant).order_by("name"):
        t = rp.totals_of(rp.income_numbers(f.with_branches({b.pk}, b.name), accounts=accounts))
        for k in sums:
            sums[k] += t[k]
        rows.append(Row([b.name, t["net_sales"], t["gross_profit"], t["opex"], t["operating_profit"], t["net_profit"],
                         _pct_str(pct(t["net_profit"], t["net_sales"]))]))
    ho = {k: whole[k] - sums[k] for k in sums}
    if any(v != 0 for v in ho.values()):
        rows.append(Row(["Head office / unallocated", ho["net_sales"], ho["gross_profit"], ho["opex"], ho["operating_profit"],
                         ho["net_profit"], _pct_str(pct(ho["net_profit"], ho["net_sales"]))], "muted"))
    rows.append(Row(["All branches", whole["net_sales"], whole["gross_profit"], whole["opex"], whole["operating_profit"],
                     whole["net_profit"], _pct_str(pct(whole["net_profit"], whole["net_sales"]))], "total"))
    return Table("Branch comparison", ["Branch", "Net sales", "Gross profit", "Operating expenses", "Operating profit",
                                       "Net profit", "Net margin"], rows, money_cols=(1, 2, 3, 4, 5))


# --------------------------------------------------------------------------
# Cash flow
# --------------------------------------------------------------------------
def cash_flow(f, **_):
    moves = rp.cash_movements(f, f.start, f.end)
    sections = {rp.OPERATING: {}, rp.INVESTING: {}, rp.FINANCING: {}}
    for sec, label, amt in moves:
        sections[sec][label] = sections[sec].get(label, ZERO) + amt
    rows, inflow, outflow = [], ZERO, ZERO
    for sec, items in sections.items():
        rows.append(Row([sec.upper(), ""], "section"))
        sub = ZERO
        for label in sorted(items):
            rows.append(Row([label, items[label]], indent=1))
            sub += items[label]
            inflow += items[label] if items[label] > 0 else ZERO
            outflow += -items[label] if items[label] < 0 else ZERO
        if not items:
            rows.append(Row(["No cash movements", ""], "muted", 1))
        rows.append(Row([f"Net cash from {sec.lower()}", sub], "subtotal"))
    opening = rp.opening_cash(f, f.start, f.end)
    net_move = inflow - outflow
    closing = opening + net_move
    summary = [Row(["Opening cash balance", opening]), Row(["Total cash inflows", inflow]),
               Row(["Total cash outflows", -outflow]), Row(["Net cash movement", net_move], "subtotal"),
               Row(["CLOSING CASH BALANCE", closing], "total")]
    bal = rp.cash_balances(f, as_of=f.end)
    ledger_total = sum(bal.values(), ZERO)
    rec = [Row(["Cash on hand", bal["CASH"]]), Row(["Bank balances", bal["BANK"]]), Row(["M-Pesa balances", bal["MPESA"]]),
           Row(["Total per ledger", ledger_total], "subtotal"),
           Row(["Difference from closing balance above", closing - ledger_total], "total")]
    d = _doc(f, "cash-flow", "Cash Flow Statement", basis_label="Actual cash movements (not affected by accounting basis)",
             tables=[Table("Cash flow", ["", "Amount"], rows, money_cols=(1,)),
                     Table("Summary", ["", "Amount"], summary, money_cols=(1,)),
                     Table("Reconciliation to cash, bank and M-Pesa accounts", ["", "Amount"], rec, money_cols=(1,))])
    # compare sales receipts with the POS sales table
    start = timezone.make_aware(datetime.datetime.combine(f.start, datetime.time.min))
    end = timezone.make_aware(datetime.datetime.combine(f.end + datetime.timedelta(days=1), datetime.time.min))
    oq = Order.all_objects.filter(tenant=f.tenant, created_at__gte=start, created_at__lt=end)
    if f.branch_ids is not None:
        oq = oq.filter(branch_id__in=f.branch_ids)
    cutover = rp.get_settings(f.tenant).opening_balance_date
    if cutover:
        oq = oq.filter(created_at__gte=timezone.make_aware(datetime.datetime.combine(cutover, datetime.time.min)))
    pos_total = oq.aggregate(t=Sum("total_revenue"))["t"] or ZERO
    ledger_sales = sections[rp.OPERATING].get("Customer payments (sales)", ZERO)
    diff = pos_total - ledger_sales
    d.notes.append("Customer payments agree with POS sales for this period." if diff == 0 else
                   f"POS sales total {fmt_money(pos_total)} but the ledger shows {fmt_money(ledger_sales)} "
                   f"(difference {fmt_money(diff)}). Open Accounting Ledger and run 'Catch up ledger'.")
    d.notes.append("Unpaid expenses are not cash outflows. They appear here only when they are actually paid.")
    d.reconciled = closing == ledger_total
    return d


# --------------------------------------------------------------------------
# Balance sheet
# --------------------------------------------------------------------------
def balance_sheet(f, **_):
    as_of = min(f.end, datetime.date.today()) if f.end > datetime.date.today() else f.end
    n = rp.balance_sheet_numbers(f, as_of)
    rows = [Row(["ASSETS", ""], "section")]
    rows += [Row([l, v], indent=1) for l, v in n["assets"] if v != 0 or l in ("Cash on hand", "Inventory")]
    rows.append(Row(["Total assets", n["total_assets"]], "total"))
    rows.append(Row(["LIABILITIES", ""], "section"))
    rows += [Row([l, v], indent=1) for l, v in n["liabilities"] if v != 0 or l in ("Accounts payable", "Loans")]
    rows.append(Row(["Total liabilities", n["total_liabilities"]], "subtotal"))
    rows.append(Row(["EQUITY", ""], "section"))
    rows += [Row([l, v], indent=1) for l, v in n["equity"] if v != 0 or l in ("Owner capital", "Current period profit")]
    rows.append(Row(["Total equity", n["total_equity"]], "subtotal"))
    rows.append(Row(["TOTAL LIABILITIES AND EQUITY", n["total_liabilities"] + n["total_equity"]], "total"))
    ok = n["difference"] == 0
    rows.append(Row(["Check: assets - (liabilities + equity)", n["difference"]], "muted" if ok else "total"))
    d = _doc(f, "balance-sheet", "Balance Sheet", period_label=f"As at {as_of:%d %b %Y}",
             basis_label="Ledger (accrual) balances",
             tables=[Table("Statement of financial position", ["", "Amount"], rows, money_cols=(1,))])
    d.balanced = ok
    d.alerts = rp.balance_diagnostics(f, as_of, n)
    d.notes.append("Current period profit is profit since the start of your financial year. Earlier profit is carried in Retained earnings.")
    if n["interbranch"]:
        d.notes.append("This branch's equity includes stock transferred between branches and how opening balances were allocated.")
    d.numbers = n
    return d


# --------------------------------------------------------------------------
# Trial balance / general ledger
# --------------------------------------------------------------------------
def trial_balance_doc(f, **_):
    accounts = rp.accounts_map(f.tenant)
    sums = rp.sums_by_account(rp.lines_qs(f, None, f.end))
    rows, td, tc = [], ZERO, ZERO
    for aid in sorted(sums, key=lambda i: accounts[i].code):
        a = accounts[aid]
        d, c = sums[aid]
        bal = d - c
        dr, cr = (bal, ZERO) if bal > 0 else (ZERO, -bal)
        if dr == 0 and cr == 0:
            continue
        rows.append(Row([a.code, a.name, dr, cr]))
        td += dr
        tc += cr
    rows.append(Row(["", "TOTAL", td, tc], "total"))
    doc = _doc(f, "trial-balance", "Trial Balance", period_label=f"As at {f.end:%d %b %Y}", basis_label="Ledger",
               tables=[Table("Trial balance", ["Code", "Account", "Debit", "Credit"], rows, money_cols=(2, 3))])
    doc.balanced = td == tc
    doc.notes.append("Debits equal credits." if td == tc else f"OUT OF BALANCE by {fmt_money(td - tc)}.")
    return doc


def general_ledger(f, account_id=None, **_):
    accounts = rp.accounts_map(f.tenant)
    ids = [int(account_id)] if account_id and str(account_id).isdigit() and int(account_id) in accounts else \
        sorted({r for r in rp.lines_qs(f, f.start, f.end).values_list("account_id", flat=True).distinct()},
               key=lambda i: accounts[i].code)
    tables, cap, used = [], 20000, 0
    for aid in ids:
        a = accounts[aid]
        before = rp.sums_by_account(rp.lines_qs(f, None, f.start - datetime.timedelta(days=1)).filter(account_id=aid)).get(aid, (ZERO, ZERO))
        running = rp.net(a, *before)
        rows = [Row(["", "", "Opening balance", "", "", running], "muted")]
        qs = rp.lines_qs(f, f.start, f.end).filter(account_id=aid).select_related("entry", "branch").order_by("entry__entry_date", "entry_id", "id")
        td = tc = ZERO
        for ln in qs[: max(0, cap - used)]:
            running += rp.net(a, ln.debit, ln.credit)
            td += ln.debit
            tc += ln.credit
            rows.append(Row([ln.entry.entry_date.strftime("%d %b %Y"), ln.entry.reference,
                             (ln.memo or ln.entry.description)[:70], ln.debit or "", ln.credit or "", running]))
            used += 1
        rows.append(Row(["", "", "Closing balance", td, tc, running], "total"))
        tables.append(Table(f"{a.code} {a.name}", ["Date", "Reference", "Description", "Debit", "Credit", "Balance"], rows,
                            money_cols=(3, 4, 5)))
    doc = _doc(f, "general-ledger", "General Ledger", basis_label="Ledger", tables=tables)
    if used >= cap:
        doc.notes.append(f"Showing the first {cap:,} lines. Narrow the dates or pick one account for the rest.")
    if not tables:
        doc.notes.append("No postings in this period.")
    return doc


# --------------------------------------------------------------------------
# Expenses
# --------------------------------------------------------------------------
def expense_qs(f, category=None, status=None, search=None):
    qs = Expense.all_objects.filter(tenant=f.tenant, expense_date__gte=f.start, expense_date__lte=f.end) \
        .select_related("category", "branch")
    if f.branch_ids is not None:
        qs = qs.filter(branch_id__in=f.branch_ids)
    if category:
        qs = qs.filter(category_id=category)
    if status == "OVERDUE":
        qs = qs.filter(status__in=["UNPAID", "PARTIAL"], due_date__lt=datetime.date.today())
    elif status:
        qs = qs.filter(status=status)
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(vendor__icontains=search) | Q(reference__icontains=search))
    return qs


def expense_register(f, category=None, status=None, **_):
    rows, ta, tp = [], ZERO, ZERO
    for e in expense_qs(f, category, status).order_by("expense_date", "id"):
        paid = e.amount_paid
        cancelled = e.status == "CANCELLED"
        rows.append(Row([e.expense_date.strftime("%d %b %Y"), e.title, e.category.name,
                         e.branch.name if e.branch else "Head office", e.vendor, e.amount, paid,
                         e.balance, e.display_status], "muted" if cancelled else "normal"))
        if not cancelled:
            ta += e.amount
            tp += paid
    rows.append(Row(["", "TOTAL (excluding cancelled)", "", "", "", ta, tp, ta - tp, ""], "total"))
    return _doc(f, "expense-register", "Expense Register", basis_label="Accrual (by expense date)",
                tables=[Table("Expenses", ["Date", "Expense", "Category", "Branch", "Payee", "Amount", "Paid", "Balance", "Status"],
                              rows, money_cols=(5, 6, 7))])


def expense_analysis(f, **_):
    ps, pe = rp.previous_range(f.start, f.end)
    base = Expense.all_objects.filter(tenant=f.tenant).exclude(status="CANCELLED")
    if f.branch_ids is not None:
        base = base.filter(branch_id__in=f.branch_ids)

    def by_cat(start, end):
        return {r["category__name"]: (r["t"] or ZERO) for r in base.filter(expense_date__gte=start, expense_date__lte=end)
                .values("category__name").annotate(t=Sum("amount"))}
    cur, prev = by_cat(f.start, f.end), by_cat(ps, pe)
    total = sum(cur.values(), ZERO)
    paid = {r["expense__category__name"]: (r["t"] or ZERO) for r in ExpensePayment.all_objects.filter(
        tenant=f.tenant, is_voided=False, expense__in=base.filter(expense_date__gte=f.start, expense_date__lte=f.end))
        .values("expense__category__name").annotate(t=Sum("amount"))}
    rows = [Row([n, v, paid.get(n, ZERO), _pct_str(pct(v, total)), prev.get(n, ZERO), _change(v, prev.get(n, ZERO))])
            for n, v in sorted(cur.items(), key=lambda kv: -kv[1])]
    rows.append(Row(["TOTAL", total, sum(paid.values(), ZERO), "100%" if total else "", sum(prev.values(), ZERO),
                     _change(total, sum(prev.values(), ZERO))], "total"))
    tables = [Table("By category", ["Category", "Incurred", "Paid", "Share", "Previous period", "Change"], rows, money_cols=(1, 2, 4))]
    brows = [Row([r["branch__name"] or "Head office", r["t"]]) for r in
             base.filter(expense_date__gte=f.start, expense_date__lte=f.end).values("branch__name").annotate(t=Sum("amount")).order_by("-t")]
    if brows:
        tables.append(Table("By branch", ["Branch", "Incurred"], brows, money_cols=(1,)))
    vrows = [Row([r["vendor"], r["t"], r["n"]]) for r in
             base.filter(expense_date__gte=f.start, expense_date__lte=f.end).exclude(vendor="").values("vendor")
             .annotate(t=Sum("amount"), n=Count("id")).order_by("-t")[:10]]
    if vrows:
        tables.append(Table("Top payees", ["Payee", "Incurred", "Expenses"], vrows, money_cols=(1,)))
    return _doc(f, "expense-analysis", "Expense Analysis", basis_label="Accrual (by expense date)", tables=tables,
                notes=[f"Previous period: {ps:%d %b %Y} to {pe:%d %b %Y}."])


# --------------------------------------------------------------------------
# Income, AR, AP
# --------------------------------------------------------------------------
def income_report(f, **_):
    accounts = rp.accounts_map(f.tenant)
    span = (f.end - f.start).days
    monthly = span > 62
    daily = {}
    for aid, kind in ((a.pk, a.system_key) for a in accounts.values() if a.system_key in ("SALES_REVENUE", "SALES_DISCOUNTS", "SALES_RETURNS")):
        for ln in rp.lines_qs(f, f.start, f.end).filter(account_id=aid).values("entry__entry_date").annotate(d=Sum("debit"), c=Sum("credit")):
            day = ln["entry__entry_date"]
            key = day.replace(day=1) if monthly else day
            rec = daily.setdefault(key, dict(g=ZERO, disc=ZERO, ret=ZERO))
            if kind == "SALES_REVENUE":
                rec["g"] += (ln["c"] or ZERO) - (ln["d"] or ZERO)
            elif kind == "SALES_DISCOUNTS":
                rec["disc"] += (ln["d"] or ZERO) - (ln["c"] or ZERO)
            else:
                rec["ret"] += (ln["d"] or ZERO) - (ln["c"] or ZERO)
    rows, tg, td, tr = [], ZERO, ZERO, ZERO
    for k in sorted(daily):
        r = daily[k]
        rows.append(Row([k.strftime("%b %Y" if monthly else "%d %b %Y"), r["g"], -r["disc"], -r["ret"], r["g"] - r["disc"] - r["ret"]]))
        tg, td, tr = tg + r["g"], td + r["disc"], tr + r["ret"]
    rows.append(Row(["TOTAL", tg, -td, -tr, tg - td - tr], "total"))
    oi = OtherIncome.all_objects.filter(tenant=f.tenant, is_cancelled=False, income_date__gte=f.start, income_date__lte=f.end).select_related("branch")
    if f.branch_ids is not None:
        oi = oi.filter(branch_id__in=f.branch_ids)
    orows = [Row([o.income_date.strftime("%d %b %Y"), o.title, o.get_kind_display(), o.branch.name if o.branch else "Head office",
                  o.amount, o.received_on.strftime("%d %b %Y") if o.received_on else "Not yet received"]) for o in oi.order_by("income_date")]
    orows.append(Row(["", "TOTAL", "", "", sum((o.amount for o in oi), ZERO), ""], "total"))
    return _doc(f, "income-report", "Income Report", basis_label="Accrual (by date earned)",
                tables=[Table("Sales", ["Period", "Gross sales", "Discounts", "Returns", "Net sales"], rows, money_cols=(1, 2, 3, 4)),
                        Table("Other income", ["Date earned", "Description", "Type", "Branch", "Amount", "Received"], orows, money_cols=(4,))])


def accounts_receivable(f, **_):
    end = min(f.end, datetime.date.today())
    qs = OtherIncome.all_objects.filter(tenant=f.tenant, is_cancelled=False, income_date__lte=end).filter(
        Q(received_on__isnull=True) | Q(received_on__gt=end)).select_related("branch")
    if f.branch_ids is not None:
        qs = qs.filter(branch_id__in=f.branch_ids)
    rows, tot = [], ZERO
    for o in qs.order_by("income_date"):
        age = (end - o.income_date).days
        bucket = "0-30 days" if age <= 30 else "31-60 days" if age <= 60 else "61-90 days" if age <= 90 else "Over 90 days"
        rows.append(Row([o.payer or "-", o.title, o.income_date.strftime("%d %b %Y"), age, bucket, o.amount]))
        tot += o.amount
    rows.append(Row(["", "TOTAL OWED TO YOU (listed)", "", "", "", tot], "total"))
    ledger = rp.balance_sheet_numbers(f, end)
    ar = dict(ledger["assets"]).get("Accounts receivable", ZERO)
    rec = [Row(["Receivables listed above", tot]), Row(["Accounts receivable in the ledger", ar]),
           Row(["Difference (opening balances or manual entries)", ar - tot], "muted")]
    d = _doc(f, "accounts-receivable", "Accounts Receivable", period_label=f"As at {end:%d %b %Y}", basis_label="Accrual",
             tables=[Table("Money owed to you", ["Customer / payer", "Description", "Date", "Days", "Age", "Amount"], rows, money_cols=(5,)),
                     Table("Reconciliation", ["", "Amount"], rec, money_cols=(1,))])
    d.notes.append("POS sales are paid in full at the till, so receivables come from other income not yet received.")
    return d


def accounts_payable(f, **_):
    end = min(f.end, datetime.date.today())
    po_rows, po_tot = [], ZERO
    pos = PurchaseOrder.all_objects.filter(tenant=f.tenant).exclude(status__in=["CANCELLED", "DRAFT"]).select_related("supplier", "branch") \
        .prefetch_related("items", "payments")
    if f.branch_ids is not None:
        pos = pos.filter(branch_id__in=f.branch_ids)
    for po in pos:
        if po.balance_payable > 0:
            po_rows.append(Row([po.supplier.name if po.supplier else "No supplier", po.po_number, po.created_at.strftime("%d %b %Y"),
                                po.total_cost, po.total_paid, po.balance_payable]))
            po_tot += po.balance_payable
    po_rows.append(Row(["", "TOTAL OWED TO SUPPLIERS", "", "", "", po_tot], "total"))
    ex = Expense.all_objects.filter(tenant=f.tenant, status__in=["UNPAID", "PARTIAL"], expense_date__lte=end).select_related("category", "branch")
    if f.branch_ids is not None:
        ex = ex.filter(branch_id__in=f.branch_ids)
    ex_rows, ex_tot = [], ZERO
    for e in ex.order_by("due_date", "expense_date"):
        ex_rows.append(Row([e.vendor or "-", e.title, e.category.name, e.due_date.strftime("%d %b %Y") if e.due_date else "-",
                            "Overdue" if e.is_overdue else "Not yet due", e.balance]))
        ex_tot += e.balance
    ex_rows.append(Row(["", "TOTAL UNPAID EXPENSES", "", "", "", ex_tot], "total"))
    n = rp.balance_sheet_numbers(f, end)
    led = dict(n["liabilities"])
    ledger_total = led.get("Accounts payable", ZERO) + led.get("Outstanding expenses", ZERO)
    rec = [Row(["Suppliers + unpaid expenses listed", po_tot + ex_tot]), Row(["Accounts payable + outstanding expenses in the ledger", ledger_total]),
           Row(["Difference (opening balances, advance payments or manual entries)", ledger_total - po_tot - ex_tot], "muted")]
    return _doc(f, "accounts-payable", "Accounts Payable", period_label=f"As at {end:%d %b %Y}", basis_label="Accrual",
                tables=[Table("Owed to suppliers", ["Supplier", "Purchase", "Date", "Total", "Paid", "Balance"], po_rows, money_cols=(3, 4, 5)),
                        Table("Unpaid expenses", ["Payee", "Expense", "Category", "Due", "Status", "Balance"], ex_rows, money_cols=(5,)),
                        Table("Reconciliation", ["", "Amount"], rec, money_cols=(1,))])


# --------------------------------------------------------------------------
# Assets and branch performance
# --------------------------------------------------------------------------
def asset_register(f, **_):
    qs = FixedAsset.all_objects.filter(tenant=f.tenant).select_related("branch")
    if f.branch_ids is not None:
        qs = qs.filter(branch_id__in=f.branch_ids)
    rows, tc, ta, tb = [], ZERO, ZERO, ZERO
    for a in qs.order_by("name"):
        acc, bv = a.accumulated_depreciation, a.book_value
        rows.append(Row([a.name, a.get_category_display(), a.branch.name if a.branch else "Head office",
                         a.purchase_date.strftime("%d %b %Y"), a.cost, acc, bv, a.get_status_display()],
                        "muted" if a.status == "DISPOSED" else "normal"))
        if a.status == "ACTIVE":
            tc, ta, tb = tc + a.cost, ta + acc, tb + bv
    rows.append(Row(["TOTAL (assets in use)", "", "", "", tc, ta, tb, ""], "total"))
    return _doc(f, "asset-register", "Fixed Asset Register", period_label=f"As at {datetime.date.today():%d %b %Y}", basis_label="Straight-line",
                tables=[Table("Fixed assets", ["Asset", "Category", "Branch", "Bought", "Cost", "Accumulated depreciation", "Book value", "Status"],
                              rows, money_cols=(4, 5, 6))])


def branch_performance(f, **_):
    accounts = rp.accounts_map(f.tenant)
    as_of = min(f.end, datetime.date.today())
    rows = []
    branches = list(Branch.all_objects.filter(tenant=f.tenant).order_by("name"))
    if f.branch_ids is not None:
        branches = [b for b in branches if b.pk in f.branch_ids]
    for b in branches:
        bf = f.with_branches({b.pk}, b.name)
        t = rp.totals_of(rp.income_numbers(bf, accounts=accounts))
        bs = rp.balance_sheet_numbers(bf, as_of)
        assets, liab = dict(bs["assets"]), dict(bs["liabilities"])
        cash = assets.get("Cash on hand", ZERO) + assets.get("Bank balances", ZERO) + assets.get("M-Pesa balances", ZERO)
        payables = liab.get("Accounts payable", ZERO) + liab.get("Outstanding expenses", ZERO)
        nbv = assets.get("Fixed assets (at cost)", ZERO) + assets.get("Less: accumulated depreciation", ZERO)
        rows.append(Row([b.name, t["net_sales"], t["gross_profit"], t["opex"], t["net_profit"], _pct_str(pct(t["net_profit"], t["net_sales"])),
                         cash, assets.get("Accounts receivable", ZERO), payables, assets.get("Inventory", ZERO), nbv]))
    return _doc(f, "branch-performance", "Branch Financial Performance",
                tables=[Table("Performance by branch", ["Branch", "Net sales", "Gross profit", "Operating expenses", "Net profit", "Net margin",
                                                       "Cash", "Receivables", "Payables", "Inventory", "Fixed assets (book)"],
                              rows, money_cols=(1, 2, 3, 4, 6, 7, 8, 9, 10))],
                notes=["Head-office costs that are not allocated to a branch are excluded here; they are shown on the Profit & Loss comparison."])


# --------------------------------------------------------------------------
# registry (the 12 exportable reports)
# --------------------------------------------------------------------------
REPORTS = {
    "profit-loss": dict(title="Profit & Loss", build=profit_loss, manager=True, blurb="Revenue, costs and profit for the period."),
    "cash-flow": dict(title="Cash Flow", build=cash_flow, manager=True, blurb="Where cash came from and went."),
    "balance-sheet": dict(title="Balance Sheet", build=balance_sheet, manager=False, blurb="What you own, owe and are worth."),
    "expense-analysis": dict(title="Expense Analysis", build=expense_analysis, manager=True, blurb="Spending by category, branch and payee."),
    "expense-register": dict(title="Expense Register", build=expense_register, manager=True, blurb="Every expense with its payment status."),
    "income-report": dict(title="Income Report", build=income_report, manager=True, blurb="Sales and other income."),
    "general-ledger": dict(title="General Ledger", build=general_ledger, manager=False, blurb="Every posting, account by account."),
    "trial-balance": dict(title="Trial Balance", build=trial_balance_doc, manager=False, blurb="Debits against credits."),
    "accounts-payable": dict(title="Accounts Payable", build=accounts_payable, manager=False, blurb="What you owe suppliers and payees."),
    "accounts-receivable": dict(title="Accounts Receivable", build=accounts_receivable, manager=False, blurb="What others owe you."),
    "asset-register": dict(title="Fixed Asset Register", build=asset_register, manager=False, blurb="Assets, depreciation and book value."),
    "branch-performance": dict(title="Branch Financial Performance", build=branch_performance, manager=True, blurb="Each branch side by side."),
}
