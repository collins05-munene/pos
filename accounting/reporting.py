"""
Financial statements built straight from the journal (plus the sub-ledgers needed for cash basis).

Every figure comes from posted journal lines or from the source records they came from, so each
number can be traced. Nothing is cached and nothing is hard-coded.
"""
import calendar
import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Q, Sum

from .models import (Account, AccountingSettings, Expense, ExpenseAllocation, ExpensePayment, JournalEntry,
                     JournalLine, LoanRepayment, OtherIncome)
from .scope import all_branch_access, allowed_branch_ids
from .services import ZERO, get_settings

CASH_KEYS = ("CASH", "BANK", "MPESA")
LIVE = ("POSTED", "REVERSED")          # a reversed entry and its reversal cancel each other out


# --------------------------------------------------------------------------
# document model shared by screens and exports
# --------------------------------------------------------------------------
@dataclass
class Row:
    cells: list
    style: str = "normal"      # normal | section | subtotal | total | muted
    indent: int = 0


@dataclass
class Table:
    title: str
    columns: list
    rows: list = field(default_factory=list)
    note: str = ""
    money_cols: tuple = ()     # column indexes holding money (right aligned, formatted)


@dataclass
class Doc:
    key: str
    title: str
    tenant: object
    branch_label: str
    period_label: str
    basis_label: str
    tables: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    generated_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    currency: str = "KES"


def fmt_money(value):
    if value is None or value == "":
        return ""
    d = Decimal(value)
    s = f"{abs(d):,.2f}"
    return f"({s})" if d < 0 else s


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------
@dataclass
class Filter:
    tenant: object
    start: datetime.date
    end: datetime.date
    kind: str = "monthly"
    branch_ids: object = None          # None = every branch (consolidated)
    branch_label: str = "All branches (consolidated)"
    basis: str = "ACCRUAL"
    currency: str = "KES"
    restricted: bool = False           # user is limited to their own branch(es)
    fy_month: int = 1

    @property
    def period_label(self):
        if self.start == self.end:
            return f"{self.start:%d %b %Y}"
        return f"{self.start:%d %b %Y} to {self.end:%d %b %Y}"

    @property
    def basis_label(self):
        return "Cash basis" if self.basis == "CASH" else "Accrual basis"

    def with_branches(self, ids, label):
        import copy
        f = copy.copy(self)
        f.branch_ids, f.branch_label = ids, label
        return f

    def with_range(self, start, end):
        import copy
        f = copy.copy(self)
        f.start, f.end = start, end
        return f


def period_bounds(kind, anchor):
    if kind == "daily":
        return anchor, anchor
    if kind == "weekly":
        start = anchor - datetime.timedelta(days=anchor.weekday())
        return start, start + datetime.timedelta(days=6)
    if kind == "quarterly":
        q = (anchor.month - 1) // 3
        start = datetime.date(anchor.year, q * 3 + 1, 1)
        end_m = q * 3 + 3
        return start, datetime.date(anchor.year, end_m, calendar.monthrange(anchor.year, end_m)[1])
    if kind == "annual":
        return datetime.date(anchor.year, 1, 1), datetime.date(anchor.year, 12, 31)
    return anchor.replace(day=1), anchor.replace(day=calendar.monthrange(anchor.year, anchor.month)[1])


def previous_range(start, end):
    """The period of equal length immediately before [start, end] (calendar-aware for whole months)."""
    if start.day == 1 and end == end.replace(day=calendar.monthrange(end.year, end.month)[1]):
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        y, m = divmod(start.year * 12 + start.month - 1 - months, 12)
        pstart = datetime.date(y, m + 1, 1)
        return pstart, start - datetime.timedelta(days=1)
    span = (end - start).days + 1
    pend = start - datetime.timedelta(days=1)
    return pend - datetime.timedelta(days=span - 1), pend


def year_ago(start, end):
    def shift(d):
        try:
            return d.replace(year=d.year - 1)
        except ValueError:
            return d.replace(year=d.year - 1, day=28)
    return shift(start), shift(end)


def build_filter(request=None, *, tenant=None, user=None, params=None):
    """Make a Filter from GET params, clamped to what this user may see."""
    from inventory.models import Branch
    tenant = tenant or request.tenant
    user = user or request.user
    p = params if params is not None else request.GET
    cfg = get_settings(tenant)
    today = datetime.date.today()
    kind = p.get("period", "monthly")
    if kind not in ("daily", "weekly", "monthly", "quarterly", "annual", "custom"):
        kind = "monthly"
    try:
        anchor = datetime.date.fromisoformat(p.get("date", "")) if p.get("date") else today
    except ValueError:
        anchor = today
    if kind == "custom":
        try:
            start = datetime.date.fromisoformat(p.get("start", ""))
            end = datetime.date.fromisoformat(p.get("end", ""))
        except ValueError:
            start, end = period_bounds("monthly", today)
        if end < start:
            start, end = end, start
    else:
        start, end = period_bounds(kind, anchor)

    allowed = allowed_branch_ids(user)
    raw = p.get("branch", "all")
    branches = {b.pk: b for b in Branch.all_objects.filter(tenant=tenant)}
    if allowed is None:
        if raw not in ("all", "", None) and str(raw).isdigit() and int(raw) in branches:
            ids, label = {int(raw)}, branches[int(raw)].name
        else:
            ids, label = None, "All branches (consolidated)"
        restricted = False
    else:                      # manager: only their own branch, whatever the URL says
        ids = set(allowed) & set(branches)
        label = ", ".join(branches[i].name for i in ids) or "No branch"
        restricted = True
    basis = p.get("basis") if p.get("basis") in ("CASH", "ACCRUAL") else cfg.reporting_basis
    return Filter(tenant=tenant, start=start, end=end, kind=kind, branch_ids=ids, branch_label=label, basis=basis,
                  currency=cfg.currency_code, restricted=restricted, fy_month=cfg.fiscal_year_start_month)


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
def accounts_map(tenant):
    return {a.pk: a for a in Account.all_objects.filter(tenant=tenant)}


def lines_qs(f, start=None, end=None):
    qs = JournalLine.all_objects.filter(tenant=f.tenant, entry__status__in=LIVE)
    if f.branch_ids is not None:
        qs = qs.filter(branch_id__in=f.branch_ids)
    if start:
        qs = qs.filter(entry__entry_date__gte=start)
    if end:
        qs = qs.filter(entry__entry_date__lte=end)
    return qs


def sums_by_account(qs):
    return {r["account_id"]: (r["d"] or ZERO, r["c"] or ZERO)
            for r in qs.values("account_id").annotate(d=Sum("debit"), c=Sum("credit"))}


def net(acct, d, c):
    """Balance in the account's own direction (contra accounts handled by normal_side)."""
    return d - c if acct.normal_side == "DEBIT" else c - d


def _branch_q(f, field):
    return Q() if f.branch_ids is None else Q(**{f"{field}__in": f.branch_ids})


# --------------------------------------------------------------------------
# income statement
# --------------------------------------------------------------------------
def income_numbers(f, start=None, end=None, accounts=None):
    """Returns the lines of a P&L as plain numbers, on the filter's basis."""
    start, end = start or f.start, end or f.end
    accounts = accounts or accounts_map(f.tenant)
    out = dict(gross_sales=ZERO, discounts=ZERO, returns=ZERO, other_income=[], cogs=[], opex=[], finance=[], tax=[])
    sums = sums_by_account(lines_qs(f, start, end))
    cash = f.basis == "CASH"
    for aid, (d, c) in sums.items():
        a = accounts[aid]
        k = a.system_key
        if k == "SALES_REVENUE":
            out["gross_sales"] += c - d
        elif k == "SALES_DISCOUNTS":
            out["discounts"] += d - c
        elif k == "SALES_RETURNS":
            out["returns"] += d - c
        elif a.account_type == "REVENUE":
            if not cash:
                out["other_income"].append([a.name, c - d])
        elif a.account_type == "COGS":
            out["cogs"].append([a.name, d - c])
        elif not cash:
            if a.account_type == "OPEX":
                out["opex"].append([a.name, d - c])
            elif a.account_type == "OTHER_EXPENSE":
                (out["tax"] if k == "TAX_EXPENSE" else out["finance"]).append([a.name, d - c])
    if cash:
        _apply_cash_basis(f, start, end, accounts, out)
    for key in ("other_income", "cogs", "opex", "finance", "tax"):
        out[key] = sorted(([n, v] for n, v in out[key] if v != 0), key=lambda r: r[0])
    return out


def _add(rows, name, amount):
    for r in rows:
        if r[0] == name:
            r[1] += amount
            return
    rows.append([name, amount])


def _apply_cash_basis(f, start, end, accounts, out):
    """Income when money is received, expenses when money is paid. Depreciation (not cash) is excluded."""
    inc = OtherIncome.all_objects.filter(tenant=f.tenant, is_cancelled=False, received_on__gte=start, received_on__lte=end)
    inc = inc.filter(_branch_q(f, "branch"))
    names = {a.system_key: a.name for a in accounts.values() if a.system_key}
    for o in inc:
        _add(out["other_income"], names.get(OtherIncome.KIND_ACCOUNT[o.kind], o.get_kind_display()), o.amount)

    pays = ExpensePayment.all_objects.filter(tenant=f.tenant, is_voided=False, payment_date__gte=start,
                                             payment_date__lte=end).select_related("expense__category__account")
    allocs = {}
    for a in ExpenseAllocation.all_objects.filter(tenant=f.tenant, expense__in=[p.expense_id for p in pays]):
        allocs.setdefault(a.expense_id, []).append(a)
    for p in pays:
        e, share = p.expense, Decimal(1)
        if e.pk in allocs:
            if f.branch_ids is not None:
                share = sum((a.percent for a in allocs[e.pk] if a.branch_id in f.branch_ids), Decimal(0)) / Decimal(100)
        elif f.branch_ids is not None and e.branch_id not in f.branch_ids:
            continue
        amount = (p.amount * share).quantize(Decimal("0.01"))
        acct = e.category.account
        if acct.system_key == "EXP_DEPRECIATION" or amount == 0:
            continue
        bucket = ("tax" if acct.system_key == "TAX_EXPENSE" else
                  "finance" if acct.account_type == "OTHER_EXPENSE" else
                  "cogs" if acct.account_type == "COGS" else "opex")
        _add(out[bucket], acct.name, amount)

    reps = LoanRepayment.all_objects.filter(tenant=f.tenant, paid_on__gte=start, paid_on__lte=end, interest_paid__gt=0)
    for r in reps.filter(_branch_q(f, "loan__branch")):
        _add(out["finance"], names.get("EXP_FINANCE", "Loan interest"), r.interest_paid)

    # postings made directly in the ledger (manual journals, till movements) count when posted. So does
    # income carried on a sale entry (a restaurant's service charge and delivery fee): the customer paid
    # it at the till, exactly like the sale itself.
    manual = ("MANUAL", "CASH_MOVEMENT", "SALE")
    q = Q(entry__source_type__in=manual) | Q(entry__source_type="REVERSAL", entry__reverses__source_type__in=manual)
    for aid, (d, c) in sums_by_account(lines_qs(f, start, end).filter(q)).items():
        a = accounts[aid]
        if a.account_type == "REVENUE" and a.system_key not in ("SALES_REVENUE", "SALES_DISCOUNTS", "SALES_RETURNS"):
            _add(out["other_income"], a.name, c - d)
        elif a.account_type == "OPEX":
            _add(out["opex"], a.name, d - c)
        elif a.account_type == "OTHER_EXPENSE":
            _add(out["tax"] if a.system_key == "TAX_EXPENSE" else out["finance"], a.name, d - c)


def totals_of(n):
    s = lambda rows: sum((v for _, v in rows), ZERO)
    net_sales = n["gross_sales"] - n["discounts"] - n["returns"]
    other_inc, cogs, opex, fin, tax = s(n["other_income"]), s(n["cogs"]), s(n["opex"]), s(n["finance"]), s(n["tax"])
    gross_profit = net_sales - cogs
    operating = gross_profit - opex
    pbt = operating + other_inc - fin
    return dict(net_sales=net_sales, other_income=other_inc, total_revenue=net_sales + other_inc, cogs=cogs,
                gross_profit=gross_profit, opex=opex, operating_profit=operating, finance=fin, pbt=pbt, tax=tax,
                net_profit=pbt - tax)


def pct(part, whole):
    return (part / whole * Decimal(100)).quantize(Decimal("0.1")) if whole else None


def net_profit_ledger(f, start=None, end=None):
    """Accrual net profit straight from the ledger over a range (for retained earnings)."""
    accounts = accounts_map(f.tenant)
    total = ZERO
    for aid, (d, c) in sums_by_account(lines_qs(f, start, end)).items():
        a = accounts[aid]
        if a.account_type == "REVENUE":
            total += c - d
        elif a.account_type in ("COGS", "OPEX", "OTHER_EXPENSE"):
            total -= d - c
    return total


# --------------------------------------------------------------------------
# cash flow
# --------------------------------------------------------------------------
OPERATING, INVESTING, FINANCING = "Operating activities", "Investing activities", "Financing activities"
EXPENSE_LABELS = {"EXP_RENT": "Rent paid", "EXP_SALARIES": "Salaries paid", "EXP_UTILITIES": "Utilities paid",
                  "EXP_DELIVERY": "Delivery and transport paid"}
ASSET_LABELS = {"EQUIPMENT": "Equipment purchases", "VEHICLE": "Vehicle purchases", "FURNITURE": "Furniture purchases",
                "COMPUTER": "Computer purchases", "OTHER": "Other asset purchases"}


def cash_account_ids(tenant):
    return set(Account.all_objects.filter(tenant=tenant, system_key__in=CASH_KEYS).values_list("pk", flat=True))


def cash_movements(f, start, end):
    """
    [(section, label, amount)] where amount is + for money in and - for money out, built from the
    cash/bank/M-Pesa lines of the ledger. Moves between your own cash accounts are internal and skipped.
    """
    from .models import FixedAsset
    cash_ids = cash_account_ids(f.tenant)
    accounts = accounts_map(f.tenant)
    entry_ids = list(lines_qs(f, start, end).filter(account_id__in=cash_ids)
                     .exclude(entry__source_type="OPENING").values_list("entry_id", flat=True).distinct())
    if not entry_ids:
        return []
    entries = {e.pk: e for e in JournalEntry.all_objects.filter(pk__in=entry_ids).select_related("reverses")}
    by_entry = {}
    base = JournalLine.all_objects.filter(entry_id__in=entry_ids)
    if f.branch_ids is not None:
        base = base.filter(branch_id__in=f.branch_ids)
    for ln in base:
        by_entry.setdefault(ln.entry_id, []).append(ln)

    pay_cat = {p.pk: p.expense.category.account.system_key for p in ExpensePayment.all_objects.filter(
        tenant=f.tenant, pk__in=[e.source_id for e in entries.values() if e.source_type == "EXPENSE_PAYMENT" and str(e.source_id).isdigit()]
    ).select_related("expense__category__account")}
    asset_cat = {a.pk: a.category for a in FixedAsset.all_objects.filter(tenant=f.tenant)}

    out = []
    for eid, e in entries.items():
        lines = by_entry.get(eid, [])
        delta = sum((l.debit - l.credit for l in lines if l.account_id in cash_ids), ZERO)
        counters = [(accounts[l.account_id], l.debit - l.credit) for l in lines if l.account_id not in cash_ids]
        if delta == 0 or not counters:
            continue
        st = e.source_type
        if st == "REVERSAL" and e.reverses_id:
            st = e.reverses.source_type
        try:
            sid = int(e.source_id or 0)
        except ValueError:
            sid = 0
        if st == "SALE":
            out.append((OPERATING, "Customer payments (sales)", delta))
        elif st == "REFUND":
            out.append((OPERATING, "Refunds paid to customers", delta))
        elif st in ("OTHER_INCOME", "OTHER_INCOME_RECEIPT"):
            out.append((OPERATING, "Other income received", delta))
        elif st == "PO_PAYMENT":
            out.append((OPERATING, "Supplier payments", delta))
        elif st == "EXPENSE_PAYMENT":
            out.append((OPERATING, EXPENSE_LABELS.get(pay_cat.get(sid), "Other operating payments"), delta))
        elif st == "ASSET_PURCHASE":
            out.append((INVESTING, ASSET_LABELS.get(asset_cat.get(sid, "OTHER"), "Other asset purchases"), delta))
        elif st == "ASSET_DISPOSAL":
            out.append((INVESTING, "Proceeds from asset disposals", delta))
        elif st == "LOAN_RECEIPT":
            out.append((FINANCING, "Loans received", delta))
        elif st == "OWNER_CAPITAL":
            out.append((FINANCING, "Owner capital contributions", delta))
        elif st == "OWNER_DRAWING":
            out.append((FINANCING, "Owner withdrawals", delta))
        else:     # LOAN_REPAYMENT, manual journals, till movements: split by what the other side is
            weights = [(a, abs(v)) for a, v in counters if v != 0]
            total_w = sum((w for _, w in weights), ZERO) or Decimal(1)
            for a, w in weights:
                share = (delta * w / total_w).quantize(Decimal("0.01"))
                k = a.system_key
                if k == "LOANS":
                    out.append((FINANCING, "Loan principal repaid" if share < 0 else "Loans received", share))
                elif k == "EXP_FINANCE":
                    out.append((OPERATING, "Interest paid", share))
                elif k in ("OWNER_CAPITAL", "OWNER_DRAWINGS", "OPENING_EQUITY"):
                    out.append((FINANCING, "Owner capital contributions" if share > 0 else "Owner withdrawals", share))
                elif k in ("FIXED_ASSETS", "ACCUM_DEPRECIATION"):
                    out.append((INVESTING, "Other investing", share))
                else:
                    out.append((OPERATING, "Other operating receipts" if share > 0 else "Other operating payments", share))
    return out


def cash_balances(f, as_of=None, before=None):
    """Cash, bank and M-Pesa balances: as of a date (inclusive) or before one (exclusive)."""
    accounts = accounts_map(f.tenant)
    ids = cash_account_ids(f.tenant)
    qs = lines_qs(f).filter(account_id__in=ids)
    if as_of:
        qs = qs.filter(entry__entry_date__lte=as_of)
    if before:
        qs = qs.filter(entry__entry_date__lt=before)
    res = {k: ZERO for k in CASH_KEYS}
    for aid, (d, c) in sums_by_account(qs).items():
        res[accounts[aid].system_key] = d - c
    return res


def opening_cash(f, start, end):
    """Cash at the start of the period. Opening-balance entries dated inside the period count as the start."""
    ids = cash_account_ids(f.tenant)
    q = Q(entry__entry_date__lt=start) | Q(entry__source_type="OPENING", entry__entry_date__lte=end)
    agg = lines_qs(f).filter(account_id__in=ids).filter(q).aggregate(d=Sum("debit"), c=Sum("credit"))
    return (agg["d"] or ZERO) - (agg["c"] or ZERO)


# --------------------------------------------------------------------------
# balance sheet
# --------------------------------------------------------------------------
def fiscal_year_start(day, month):
    y = day.year if day.month >= month else day.year - 1
    return datetime.date(y, month, 1)


def balance_sheet_numbers(f, as_of):
    accounts = accounts_map(f.tenant)
    sums = sums_by_account(lines_qs(f, None, as_of))
    bal = {}
    for aid, (d, c) in sums.items():
        bal[aid] = net(accounts[aid], d, c)
    by_key = {a.system_key: bal.get(a.pk, ZERO) for a in accounts.values() if a.system_key}

    fy_start = fiscal_year_start(as_of, f.fy_month)
    prior_profit = net_profit_ledger(f, None, fy_start - datetime.timedelta(days=1))
    current_profit = net_profit_ledger(f, fy_start, as_of)
    named_assets = {"CASH", "BANK", "MPESA", "AR", "INVENTORY", "FIXED_ASSETS", "ACCUM_DEPRECIATION"}
    named_liab = {"AP", "ACCRUED_EXPENSES", "LOANS", "TAX_PAYABLE", "OTHER_LIABILITIES"}
    named_eq = {"OWNER_CAPITAL", "RETAINED_EARNINGS", "OWNER_DRAWINGS", "OPENING_EQUITY"}
    extra = dict(assets=[], liabilities=[], equity=[])
    for a in accounts.values():
        v = bal.get(a.pk, ZERO)
        if a.is_header or v == 0 or a.system_key in named_assets | named_liab | named_eq:
            continue
        if a.account_type == "ASSET":
            extra["assets"].append((a.name, v))
        elif a.account_type == "LIABILITY":
            extra["liabilities"].append((a.name, v))
        elif a.account_type == "EQUITY":
            extra["equity"].append((a.name, v))
    g = lambda k: by_key.get(k, ZERO)
    assets = [("Cash on hand", g("CASH")), ("Bank balances", g("BANK")), ("M-Pesa balances", g("MPESA")),
              ("Accounts receivable", g("AR")), ("Inventory", g("INVENTORY")), ("Fixed assets (at cost)", g("FIXED_ASSETS")),
              ("Less: accumulated depreciation", -g("ACCUM_DEPRECIATION"))] + extra["assets"]
    liabilities = [("Accounts payable", g("AP")), ("Outstanding expenses", g("ACCRUED_EXPENSES")), ("Loans", g("LOANS")),
                   ("Tax liabilities", g("TAX_PAYABLE")), ("Other obligations", g("OTHER_LIABILITIES"))] + extra["liabilities"]
    equity = [("Owner capital", g("OWNER_CAPITAL")), ("Opening balance equity", g("OPENING_EQUITY")),
              ("Retained earnings", g("RETAINED_EARNINGS") + prior_profit),
              ("Current period profit", current_profit), ("Less: owner drawings", -g("OWNER_DRAWINGS"))] + extra["equity"]
    total_a = sum((v for _, v in assets), ZERO)
    total_l = sum((v for _, v in liabilities), ZERO)
    total_e = sum((v for _, v in equity), ZERO)
    residual = total_a - total_l - total_e
    interbranch = None
    if f.branch_ids is not None and residual != 0:
        interbranch = ("Inter-branch balance (stock transfers and opening allocation)", residual)
        equity.append(interbranch)
        total_e += residual
        residual = ZERO
    return dict(assets=assets, liabilities=liabilities, equity=equity, total_assets=total_a, total_liabilities=total_l,
                total_equity=total_e, difference=residual, fy_start=fy_start, interbranch=interbranch)


def balance_diagnostics(f, as_of, nums):
    """Plain-English reasons a balance sheet might not balance."""
    notes = []
    if nums["difference"] == 0:
        return notes
    notes.append(f"Assets differ from liabilities plus equity by {fmt_money(nums['difference'])}.")
    bad = []
    for e in JournalEntry.all_objects.filter(tenant=f.tenant, entry_date__lte=as_of, status__in=LIVE):
        if e.total_debit != e.total_credit:
            bad.append(e.reference)
    if bad:
        notes.append("Journal entries that do not balance: " + ", ".join(bad[:10]) + ".")
    else:
        notes.append("Every journal entry balances, so the gap comes from the filter. Try All branches.")
    return notes
