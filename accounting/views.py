import datetime
import uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from inventory.models import Branch

from . import dashboard as dash
from . import documents as docs
from . import exports, forms as F, integration as ig, operations as ops, reporting as rp
from .models import (Account, Expense, ExpenseAllocation, ExpenseCategory, ExpensePayment, FinancialAuditLog, FixedAsset,
                     JournalEntry, Loan, LoanRepayment, OtherIncome, OwnerTransaction, RecurringExpense)
from .permissions import FinanceAccessMixin, can_access_area, can_configure_accounting
from .scope import all_branch_access, allowed_branch_ids
from .services import (AccountingError, audit, get_settings, post_journal, reverse_entry, seed_chart_of_accounts,
                       set_opening_balances, money)

OPENING_FIELDS = [
    ("Cash on hand", "CASH"), ("Bank balance", "BANK"), ("M-Pesa balance", "MPESA"),
    ("Money customers owe you", "AR"), ("Stock value at cost", "INVENTORY"),
    ("Fixed assets at cost", "FIXED_ASSETS"), ("Depreciation already taken on them", "ACCUM_DEPRECIATION"),
    ("Money you owe suppliers", "AP"), ("Unpaid expenses", "ACCRUED_EXPENSES"), ("Loans you owe", "LOANS"),
    ("Tax you owe", "TAX_PAYABLE"),
]


class FinView(FinanceAccessMixin, TemplateView):
    """Base for every finance page: sets the tenant-scoped helpers and runs light upkeep."""
    section = ""

    def ctx(self, **extra):
        user = self.request.user
        base = dict(section=self.section, can_configure=can_configure_accounting(user), all_access=all_branch_access(user),
                    cfg=get_settings(self.request.tenant), nav=nav_for(user))
        base.update(extra)
        return base

    def filter(self):
        return rp.build_filter(self.request)

    def filter_ctx(self, f):
        branches = list(Branch.all_objects.filter(tenant=f.tenant).order_by("name")) if not f.restricted else []
        return dict(f=f, filter_branches=branches, qs=self.request.GET.urlencode(), today=datetime.date.today())


def nav_for(user):
    items = [("overview", "Financial Overview", "accounting:overview"), ("expenses", "Expenses", "accounting:expenses"),
             ("income", "Other Income", "accounting:income"), ("pnl", "Profit & Loss", "accounting:profit-loss"),
             ("cashflow", "Cash Flow", "accounting:cash-flow"), ("balance", "Balance Sheet", "accounting:balance-sheet"),
             ("assets", "Assets & Liabilities", "accounting:assets"), ("ledger", "Accounting Ledger", "accounting:ledger"),
             ("analysis", "Financial Analysis", "accounting:analysis"), ("reports", "Financial Reports", "accounting:reports")]
    return [(k, label, reverse(url)) for k, label, url in items if can_access_area(user, k)]


# ---------------------------------------------------------------- overview / setup
class OverviewView(FinView):
    template_name = "accounting/overview.html"
    area = section = "overview"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tenant, cfg = self.request.tenant, get_settings(self.request.tenant)
        extra = dict(opening_fields=OPENING_FIELDS, today=datetime.date.today(),
                     branches=Branch.all_objects.filter(tenant=tenant).order_by("-is_main", "name"),
                     stock_value=ig.stock_value(tenant))
        if cfg.opening_balance_date:
            ig.housekeeping(tenant)
            f = self.filter()
            extra.update(self.filter_ctx(f))
            extra["d"] = dash.dashboard(f)
        return {**ctx, **self.ctx(**extra)}


class SetupView(FinanceAccessMixin, View):
    area = "overview"

    def post(self, request):
        if not can_configure_accounting(request.user):
            messages.error(request, "Only the owner or an admin can set up accounting.")
            return redirect("accounting:overview")
        tenant = request.tenant
        if get_settings(tenant).opening_balance_date:
            messages.info(request, "Accounting is already set up for this business.")
            return redirect("accounting:overview")
        try:
            as_of = datetime.date.fromisoformat(request.POST.get("opening_date", ""))
        except ValueError:
            messages.error(request, "Choose the date your books start from.")
            return redirect("accounting:overview")
        if as_of > datetime.date.today():
            messages.error(request, "The starting date cannot be in the future.")
            return redirect("accounting:overview")
        balances = {}
        for label, key in OPENING_FIELDS:
            raw = (request.POST.get(key) or "").strip().replace(",", "")
            if not raw:
                continue
            try:
                amount = Decimal(raw)
            except InvalidOperation:
                messages.error(request, f"{label}: '{raw}' is not a valid amount.")
                return redirect("accounting:overview")
            if amount < 0:
                messages.error(request, f"{label} cannot be negative.")
                return redirect("accounting:overview")
            balances[key] = amount
        branch = Branch.all_objects.filter(tenant=tenant, pk=request.POST.get("opening_branch") or 0).first() or \
            Branch.all_objects.filter(tenant=tenant).order_by("-is_main", "id").first()
        try:
            seed_chart_of_accounts(tenant)
            set_opening_balances(tenant=tenant, as_of=as_of, balances=balances, user=request.user, branch=branch)
        except AccountingError as exc:
            messages.error(request, str(exc))
            return redirect("accounting:overview")
        messages.success(request, "Accounting is ready. Your books start on %s." % as_of.strftime("%d %b %Y"))
        return redirect("accounting:overview")


# ---------------------------------------------------------------- generic service form
class ServiceFormView(FinView):
    template_name = "accounting/form_page.html"
    form_class = None
    title, subtitle, submit_label = "", "", "Save"
    cancel_url = "accounting:overview"
    multipart = False

    def form_kwargs(self):
        return {}

    def get_form(self, data=None, files=None):
        kw = dict(self.form_kwargs())
        if issubclass(self.form_class, F.TenantForm):
            kw.update(tenant=self.request.tenant, user=self.request.user)
        return self.form_class(data, files, **kw) if data is not None else self.form_class(**kw)

    def page(self, form):
        return render(self.request, self.template_name, self.ctx(form=form, title=self.title, subtitle=self.subtitle,
                                                                 submit_label=self.submit_label, cancel_url=reverse(self.cancel_url) if ":" in self.cancel_url else self.cancel_url,
                                                                 multipart=self.multipart))

    def get(self, request, *a, **kw):
        return self.page(self.get_form())

    def post(self, request, *a, **kw):
        form = self.get_form(request.POST, request.FILES)
        if form.is_valid():
            try:
                return self.perform(form)
            except AccountingError as exc:
                form.add_error(None, str(exc))
        return self.page(form)

    def perform(self, form):
        raise NotImplementedError


class ActionView(FinanceAccessMixin, View):
    """POST-only actions (void, cancel, toggle...) that report the outcome with a message."""
    success_message = "Done."
    redirect_to = "accounting:overview"

    def post(self, request, *args, **kwargs):
        try:
            self.act(request, *args, **kwargs)
            messages.success(request, self.success_message)
        except AccountingError as exc:
            messages.error(request, str(exc))
        return redirect(self.next_url(request, *args, **kwargs))

    def next_url(self, request, *a, **kw):
        return reverse(self.redirect_to)


# ---------------------------------------------------------------- expenses
class ExpenseListView(FinView):
    template_name = "accounting/expense_list.html"
    area = section = "expenses"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tenant = self.request.tenant
        ig.housekeeping(tenant)
        f = self.filter()
        g = self.request.GET
        qs = docs.expense_qs(f, g.get("category") or None, g.get("status") or None, g.get("q") or None).order_by("-expense_date", "-id")
        active = qs.exclude(status="CANCELLED")
        incurred = active.aggregate(t=Sum("amount"))["t"] or Decimal("0")
        paid = ExpensePayment.all_objects.filter(expense__in=active, is_voided=False).aggregate(t=Sum("amount"))["t"] or Decimal("0")
        overdue = active.filter(status__in=["UNPAID", "PARTIAL"], due_date__lt=datetime.date.today()).count()
        page = Paginator(qs, 25).get_page(g.get("page"))
        pays = dict(ExpensePayment.all_objects.filter(expense__in=list(page), is_voided=False).values_list("expense_id").annotate(t=Sum("amount")))
        for e in page:
            p = pays.get(e.pk, Decimal("0"))
            e.paid_cache, e.balance_cache = p, (Decimal("0") if e.status == "CANCELLED" else e.amount - p)
        cats = ExpenseCategory.all_objects.filter(tenant=tenant).order_by("name")
        return {**ctx, **self.ctx(page=page, categories=cats, incurred=incurred, paid=paid, outstanding=incurred - paid,
                                  overdue=overdue, status_choices=list(Expense.Status.choices) + [("OVERDUE", "Overdue")],
                                  **self.filter_ctx(f))}


class ExpenseCreateView(ServiceFormView):
    form_class = F.ExpenseForm
    area = section = "expenses"
    title, subtitle, submit_label = "Record an expense", "An expense starts unpaid. Record payments separately, in full or in parts.", "Save expense"
    cancel_url = "accounting:expenses"
    multipart = True
    template_name = "accounting/expense_form.html"

    def form_kwargs(self):
        return {"show_recurring": all_branch_access(self.request.user)}

    def perform(self, form):
        d, tenant, user = form.cleaned_data, self.request.tenant, self.request.user
        if d.get("repeat"):
            if form.allocations():
                raise AccountingError("Repeating expenses cannot be split across branches yet. Record this one without repeating.")
            rec = ops.create_recurring_expense(tenant=tenant, user=user, title=d["title"], category=d["category"], amount=d["amount"],
                                               frequency=d["repeat"], start_date=d["expense_date"], end_date=d.get("repeat_until"),
                                               branch=d.get("branch"), vendor=d.get("vendor", ""), payment_method=d.get("payment_method", ""),
                                               due_after_days=(d["due_date"] - d["expense_date"]).days if d.get("due_date") else 0,
                                               description=d.get("description", ""))
            made = ops.generate_recurring_expenses(tenant)
            messages.success(self.request, f"Repeating expense set up. {made} expense(s) created so far, all unpaid.")
            return redirect("accounting:recurring")
        e = ops.create_expense(tenant=tenant, user=user, title=d["title"], category=d["category"], amount=d["amount"],
                               expense_date=d["expense_date"], due_date=d.get("due_date"), branch=d.get("branch"),
                               vendor=d.get("vendor", ""), payment_method=d.get("payment_method", ""),
                               reference=d.get("reference", ""), description=d.get("description", ""),
                               receipt=d.get("receipt"), allocations=form.allocations() or None)
        messages.success(self.request, "Expense recorded. It is unpaid until you record a payment.")
        return redirect("accounting:expense-detail", pk=e.pk)


def _expense_for(request, pk):
    e = Expense.all_objects.filter(tenant=request.tenant, pk=pk).select_related("category", "branch", "created_by").first()
    if e is None:
        raise Http404
    ids = allowed_branch_ids(request.user)
    if ids is not None and (e.branch_id is None or e.branch_id not in ids):
        raise Http404        # other branches' expenses do not exist as far as a manager is concerned
    return e


class ExpenseDetailView(FinView):
    template_name = "accounting/expense_detail.html"
    area = section = "expenses"

    def get_context_data(self, pk, **kwargs):
        ctx = super().get_context_data(**kwargs)
        e = _expense_for(self.request, pk)
        payments = ExpensePayment.all_objects.filter(expense=e).select_related("paid_by", "account").order_by("-payment_date", "-id")
        history = FinancialAuditLog.all_objects.filter(tenant=e.tenant, object_type="Expense", object_id=str(e.pk)).select_related("user").order_by("-created_at")[:30]
        entries = JournalEntry.all_objects.filter(tenant=e.tenant, source_type__in=["EXPENSE", "EXPENSE_PAYMENT"]).filter(
            Q(source_type="EXPENSE", source_id=str(e.pk)) | Q(source_type="EXPENSE_PAYMENT", source_id__in=[str(p.pk) for p in payments]))
        form = F.PaymentForm(tenant=e.tenant, user=self.request.user, initial=dict(amount=e.balance, payment_method=e.payment_method or "CASH"))
        return {**ctx, **self.ctx(e=e, payments=payments, history=history, form=form, entries=entries,
                                  allocations=ExpenseAllocation.all_objects.filter(expense=e).select_related("branch"))}


class ExpenseEditView(ExpenseCreateView):
    title, submit_label = "Edit expense", "Save changes"

    def get_form(self, data=None, files=None):
        self.expense = _expense_for(self.request, self.kwargs["pk"])
        form = super().get_form(data, files)
        if data is None:
            e = self.expense
            for k in ("title", "category", "amount", "expense_date", "due_date", "branch", "vendor", "payment_method", "reference", "description"):
                form.initial[k] = getattr(e, k)
            for a in ExpenseAllocation.all_objects.filter(expense=e):
                form.initial[f"alloc_{a.branch_id}"] = a.percent
        form.fields.pop("repeat", None)
        form.fields.pop("repeat_until", None)
        return form

    def form_kwargs(self):
        return {"show_recurring": False}

    def perform(self, form):
        d = form.cleaned_data
        changes = {k: d.get(k) for k in ("title", "category", "amount", "expense_date", "due_date", "branch", "vendor",
                                         "payment_method", "reference", "description")}
        changes["due_date"] = d.get("due_date")
        if d.get("receipt"):
            changes["receipt"] = d["receipt"]
        ops.update_expense(self.expense, user=self.request.user, allocations=form.allocations() if self.expense.branch_id is None and form.branch_list else None, **changes)
        messages.success(self.request, "Expense updated.")
        return redirect("accounting:expense-detail", pk=self.expense.pk)


class ExpensePayView(ActionView):
    area = "expenses"

    def act(self, request, pk):
        e = _expense_for(request, pk)
        form = F.PaymentForm(request.POST, tenant=request.tenant, user=request.user)
        if not form.is_valid():
            raise AccountingError("; ".join(f"{k}: {' '.join(v)}" for k, v in form.errors.items()))
        d = form.cleaned_data
        ops.record_expense_payment(e, user=request.user, amount=d["amount"], payment_date=d["payment_date"], method=d["payment_method"],
                                   reference=d.get("reference", ""), account=d.get("account"))
        self.success_message = "Payment recorded."

    def next_url(self, request, pk):
        return reverse("accounting:expense-detail", args=[pk])


class PaymentVoidView(ActionView):
    area = "expenses"
    success_message = "Payment voided. The money is back on the expense as unpaid."

    def act(self, request, pk):
        pay = ExpensePayment.all_objects.filter(tenant=request.tenant, pk=pk).select_related("expense").first()
        if pay is None:
            raise Http404
        _expense_for(request, pay.expense_id)
        self.eid = pay.expense_id
        ops.void_expense_payment(pay, user=request.user, reason=request.POST.get("reason", ""))

    def next_url(self, request, pk):
        return reverse("accounting:expense-detail", args=[getattr(self, "eid", 0) or ExpensePayment.all_objects.get(pk=pk).expense_id])


class ExpenseCancelView(ActionView):
    area = "expenses"
    success_message = "Expense cancelled."

    def act(self, request, pk):
        ops.cancel_expense(_expense_for(request, pk), user=request.user, reason=request.POST.get("reason", ""))

    def next_url(self, request, pk):
        return reverse("accounting:expense-detail", args=[pk])


class ReceiptView(FinanceAccessMixin, View):
    area = "expenses"

    def get(self, request, pk):
        e = _expense_for(request, pk)
        if not e.receipt:
            raise Http404
        audit(e.tenant, request.user, "RECEIPT_VIEWED", e)
        return FileResponse(e.receipt.open("rb"), as_attachment=request.GET.get("download") == "1", filename=e.receipt.name.rsplit("/", 1)[-1])


class CategoryView(ServiceFormView):
    area = section = "expenses"
    form_class = F.CategoryForm
    template_name = "accounting/categories.html"
    title, cancel_url = "Expense categories", "accounting:expenses"

    def page(self, form):
        cats = ExpenseCategory.all_objects.filter(tenant=self.request.tenant).select_related("account").order_by("name")
        return render(self.request, self.template_name, self.ctx(form=form, categories=cats, title=self.title))

    def post(self, request, *a, **kw):
        if not can_configure_accounting(request.user) and not all_branch_access(request.user):
            raise PermissionDenied
        return super().post(request, *a, **kw)

    def perform(self, form):
        ops.create_category(tenant=self.request.tenant, user=self.request.user, name=form.cleaned_data["name"], account=form.cleaned_data["account"])
        messages.success(self.request, "Category added.")
        return redirect("accounting:categories")


class CategoryToggleView(ActionView):
    area = "expenses"
    success_message = "Category updated."
    redirect_to = "accounting:categories"

    def act(self, request, pk):
        if not all_branch_access(request.user):
            raise AccountingError("Only the owner, admins and accountants can change categories.")
        cat = get_object_or_404(ExpenseCategory.all_objects.filter(tenant=request.tenant), pk=pk)
        ops.set_category_active(cat, request.user, not cat.is_active)


class RecurringListView(FinView):
    template_name = "accounting/recurring.html"
    area = section = "expenses"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        recs = RecurringExpense.all_objects.filter(tenant=self.request.tenant).select_related("category", "branch").order_by("title")
        return {**ctx, **self.ctx(recs=recs)}


class RecurringToggleView(ActionView):
    area = "expenses"
    success_message = "Repeating expense updated."
    redirect_to = "accounting:recurring"

    def act(self, request, pk):
        if not all_branch_access(request.user):
            raise AccountingError("Only the owner, admins and accountants can change repeating expenses.")
        rec = get_object_or_404(RecurringExpense.all_objects.filter(tenant=request.tenant), pk=pk)
        ops.set_recurring_active(rec, request.user, not rec.is_active)


# ---------------------------------------------------------------- other income
class IncomeListView(FinView):
    template_name = "accounting/income_list.html"
    area = section = "income"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        f = self.filter()
        qs = OtherIncome.all_objects.filter(tenant=f.tenant, income_date__gte=f.start, income_date__lte=f.end).select_related("branch")
        if f.branch_ids is not None:
            qs = qs.filter(branch_id__in=f.branch_ids)
        total = qs.filter(is_cancelled=False).aggregate(t=Sum("amount"))["t"] or Decimal("0")
        owed = qs.filter(is_cancelled=False, received_on__isnull=True).aggregate(t=Sum("amount"))["t"] or Decimal("0")
        page = Paginator(qs.order_by("-income_date", "-id"), 25).get_page(self.request.GET.get("page"))
        return {**ctx, **self.ctx(page=page, total=total, owed=owed, receive_form=F.ReceiveIncomeForm(), **self.filter_ctx(f))}


class IncomeCreateView(ServiceFormView):
    area = section = "income"
    form_class = F.IncomeForm
    title, subtitle, cancel_url = "Record other income", "Rent, interest, services and other income that does not come through the POS.", "accounting:income"

    def perform(self, form):
        d = form.cleaned_data
        ops.record_other_income(tenant=self.request.tenant, user=self.request.user, title=d["title"], kind=d["kind"], amount=d["amount"],
                                income_date=d["income_date"], received_on=d.get("received_on"), payment_method=d.get("payment_method", ""),
                                branch=d.get("branch"), payer=d.get("payer", ""), reference=d.get("reference", ""), description=d.get("description", ""))
        messages.success(self.request, "Income recorded.")
        return redirect("accounting:income")


class IncomeReceiveView(ActionView):
    area = "income"
    success_message = "Receipt recorded."
    redirect_to = "accounting:income"

    def act(self, request, pk):
        inc = get_object_or_404(OtherIncome.all_objects.filter(tenant=request.tenant), pk=pk)
        form = F.ReceiveIncomeForm(request.POST)
        if not form.is_valid():
            raise AccountingError("Enter a valid date and method.")
        ops.mark_income_received(inc, user=request.user, received_on=form.cleaned_data["received_on"], payment_method=form.cleaned_data["payment_method"])


class IncomeCancelView(ActionView):
    area = "income"
    success_message = "Income cancelled."
    redirect_to = "accounting:income"

    def act(self, request, pk):
        inc = get_object_or_404(OtherIncome.all_objects.filter(tenant=request.tenant), pk=pk)
        ops.cancel_other_income(inc, user=request.user, reason=request.POST.get("reason", ""))


# ---------------------------------------------------------------- statements and reports
REPORT_AREA = {"profit-loss": "pnl", "cash-flow": "cashflow", "balance-sheet": "balance"}
AREA_SECTION = {"profit-loss": "pnl", "cash-flow": "cashflow", "balance-sheet": "balance"}


def can_view_report(user, key):
    meta = docs.REPORTS.get(key)
    if meta is None:
        return False
    if all_branch_access(user):
        return True
    return bool(meta["manager"]) and can_access_area(user, REPORT_AREA.get(key, "reports"))


class ReportView(FinView):
    """Shows any report on screen (fmt=None) or sends it as PDF/Excel."""
    template_name = "accounting/report.html"
    key = None
    fmt = None

    def dispatch(self, request, *args, **kwargs):
        key = kwargs.get("key") or self.key
        self.report_key = key
        self.area = REPORT_AREA.get(key, "reports")
        self.section = AREA_SECTION.get(key, "reports")
        if request.user.is_authenticated and key in docs.REPORTS and not can_view_report(request.user, key):
            raise PermissionDenied("You do not have permission to view this report.")
        return super().dispatch(request, *args, **kwargs)

    def build(self, f):
        meta = docs.REPORTS[self.report_key]
        g = self.request.GET
        opts = {}
        if self.report_key == "profit-loss":
            opts["compare"] = g.get("compare", "1") != "0"
        if self.report_key == "expense-register":
            opts.update(category=g.get("category") or None, status=g.get("status") or None)
        if self.report_key == "general-ledger":
            opts["account_id"] = g.get("account") or None
        return meta["build"](f, **opts)

    def get(self, request, *args, key=None, fmt=None, **kwargs):
        key = key or self.key
        if key not in docs.REPORTS:
            raise Http404
        ig.housekeeping(request.tenant)
        f = self.filter()
        doc = self.build(f)
        fmt = fmt or self.fmt
        if fmt in ("pdf", "xlsx"):
            data = exports.to_pdf(doc) if fmt == "pdf" else exports.to_xlsx(doc)
            audit(request.tenant, request.user, "REPORT_EXPORTED", None, report=key, format=fmt, period=f.period_label,
                  branch=f.branch_label, basis=f.basis)
            ctype = "application/pdf" if fmt == "pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            resp = HttpResponse(data, content_type=ctype)
            fname = f"{key}-{f.start:%Y%m%d}-{f.end:%Y%m%d}.{fmt}"
            resp["Content-Disposition"] = f'attachment; filename="{fname}"'
            return resp
        extra = {}
        if key == "expense-register":
            extra.update(categories=ExpenseCategory.all_objects.filter(tenant=f.tenant).order_by("name"), status_choices=list(Expense.Status.choices) + [("OVERDUE", "Overdue")])
        if key == "general-ledger":
            extra["gl_accounts"] = Account.all_objects.filter(tenant=f.tenant, is_header=False).order_by("code")
        return render(request, self.template_name, {**self.ctx(doc=doc, report_key=key, report_title=docs.REPORTS[key]["title"],
                                                               show_basis=key in ("profit-loss", "expense-analysis", "income-report"),
                                                               compare=self.request.GET.get("compare", "1") != "0", **extra), **self.filter_ctx(f)})


class ReportsHubView(FinView):
    template_name = "accounting/reports_hub.html"
    area = section = "reports"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        f = self.filter()
        items = [(k, m["title"], m["blurb"]) for k, m in docs.REPORTS.items() if can_view_report(self.request.user, k)]
        return {**ctx, **self.ctx(items=items, show_basis=True, **self.filter_ctx(f))}


class AnalysisView(FinView):
    template_name = "accounting/analysis.html"
    area = section = "analysis"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ig.housekeeping(self.request.tenant)
        f = self.filter()
        d = dash.analysis(f)
        series = d.series
        charts = dict(labels=[s["label"] for s in series], sales=[float(s["net_sales"]) for s in series], opex=[float(s["opex"]) for s in series],
                      net=[float(s["net_profit"]) for s in series], net_cash=[float(s["net_cash"]) for s in series])
        return {**ctx, **self.ctx(doc=d, charts=charts, show_basis=True, **self.filter_ctx(f))}


# ---------------------------------------------------------------- assets & liabilities
class AssetsView(FinView):
    template_name = "accounting/assets.html"
    area = section = "assets"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tab = self.request.GET.get("tab", "assets")
        tenant = self.request.tenant
        ig.housekeeping(tenant)
        extra = dict(tab=tab)
        if tab == "loans":
            extra["loans"] = Loan.all_objects.filter(tenant=tenant).select_related("branch").order_by("-start_date")
        elif tab == "owner":
            extra["owner_txns"] = OwnerTransaction.all_objects.filter(tenant=tenant).select_related("branch")[:100]
        else:
            extra["assets"] = FixedAsset.all_objects.filter(tenant=tenant).select_related("branch").order_by("name")
            a = [x for x in extra["assets"] if x.status == "ACTIVE"]
            extra["total_cost"] = sum((x.cost for x in a), Decimal("0"))
            extra["total_book"] = sum((x.book_value for x in a), Decimal("0"))
        return {**ctx, **self.ctx(**extra)}


class AssetCreateView(ServiceFormView):
    area = section = "assets"
    form_class = F.AssetForm
    title, subtitle, cancel_url = "Register a fixed asset", "Assets are capitalised and depreciated over their useful life. They are never booked as an expense.", "accounting:assets"

    def perform(self, form):
        d = form.cleaned_data
        ops.register_asset(tenant=self.request.tenant, user=self.request.user, name=d["name"], category=d["category"], cost=d["cost"],
                           purchase_date=d["purchase_date"], useful_life_months=d["useful_life_months"], residual_value=d["residual_value"],
                           branch=d.get("branch"), paid_with=d.get("paid_with", ""), existing_asset=d.get("existing_asset", False),
                           opening_accumulated_depreciation=d.get("opening_accumulated_depreciation") or 0, notes=d.get("notes", ""))
        messages.success(self.request, "Asset registered.")
        return redirect("accounting:assets")


class AssetDisposeView(ServiceFormView):
    area = section = "assets"
    form_class = F.DisposeForm
    title, cancel_url, submit_label = "Dispose of an asset", "accounting:assets", "Record disposal"

    def get_form(self, data=None, files=None):
        self.asset = get_object_or_404(FixedAsset.all_objects.filter(tenant=self.request.tenant), pk=self.kwargs["pk"])
        self.subtitle = f"{self.asset.name}: book value {rp.fmt_money(self.asset.book_value)}"
        return super().get_form(data, files)

    def perform(self, form):
        d = form.cleaned_data
        ops.dispose_asset(self.asset, user=self.request.user, disposal_date=d["disposal_date"], proceeds=d["proceeds"], method=d.get("method", ""))
        messages.success(self.request, "Disposal recorded.")
        return redirect("accounting:assets")


class DepreciationRunView(ActionView):
    area = "assets"
    redirect_to = "accounting:assets"

    def act(self, request):
        if not all_branch_access(request.user):
            raise AccountingError("Not allowed.")
        up_to = datetime.date.today().replace(day=1) - datetime.timedelta(days=1)
        n = ops.generate_depreciation(request.tenant, up_to=up_to, user=request.user)
        self.success_message = f"Depreciation posted for {n} asset-month(s)." if n else "Depreciation is already up to date."


class LoanCreateView(ServiceFormView):
    area = section = "assets"
    form_class = F.LoanForm
    title, subtitle, cancel_url = "Record a loan", "Principal and interest are tracked separately: only interest is an expense.", "accounting:assets"

    def perform(self, form):
        d = form.cleaned_data
        ops.record_loan(tenant=self.request.tenant, user=self.request.user, lender=d["lender"], principal=d["principal"],
                        interest_rate_percent=d["interest_rate_percent"], start_date=d["start_date"], installments=d["installments"],
                        frequency=d["frequency"], received_via=d.get("received_via", ""), branch=d.get("branch"),
                        existing_loan=d.get("existing_loan", False), opening_outstanding=d.get("opening_outstanding"), notes=d.get("notes", ""))
        messages.success(self.request, "Loan recorded.")
        return redirect(reverse("accounting:assets") + "?tab=loans")


class LoanDetailView(ServiceFormView):
    area = section = "assets"
    form_class = F.RepayForm
    template_name = "accounting/loan_detail.html"
    title, submit_label = "Record a repayment", "Record repayment"

    def page(self, form):
        loan = get_object_or_404(Loan.all_objects.filter(tenant=self.request.tenant), pk=self.kwargs["pk"])
        return render(self.request, self.template_name, self.ctx(form=form, loan=loan, schedule=ops.loan_schedule(loan),
                                                                 repayments=LoanRepayment.all_objects.filter(loan=loan).select_related("paid_by")[:50], title=self.title,
                                                                 submit_label=self.submit_label))

    def perform(self, form):
        loan = get_object_or_404(Loan.all_objects.filter(tenant=self.request.tenant), pk=self.kwargs["pk"])
        d = form.cleaned_data
        ops.repay_loan(loan, user=self.request.user, paid_on=d["paid_on"], principal_paid=d["principal_paid"], interest_paid=d["interest_paid"],
                       payment_method=d["payment_method"], reference=d.get("reference", ""))
        messages.success(self.request, "Repayment recorded.")
        return redirect("accounting:loan", pk=loan.pk)


class OwnerCreateView(ServiceFormView):
    area = section = "assets"
    form_class = F.OwnerForm
    title, subtitle, cancel_url = "Owner capital or withdrawal", "Money you put into the business, or take out for yourself. Withdrawals are not expenses.", "accounting:assets"

    def perform(self, form):
        d = form.cleaned_data
        ops.record_owner_transaction(tenant=self.request.tenant, user=self.request.user, kind=d["kind"], amount=d["amount"], txn_date=d["txn_date"],
                                     payment_method=d["payment_method"], branch=d.get("branch"), reference=d.get("reference", ""), notes=d.get("notes", ""))
        messages.success(self.request, "Recorded.")
        return redirect(reverse("accounting:assets") + "?tab=owner")


# ---------------------------------------------------------------- ledger
class LedgerView(FinView):
    template_name = "accounting/ledger.html"
    area = section = "ledger"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        tenant, g = self.request.tenant, self.request.GET
        ig.housekeeping(tenant)
        tab = g.get("tab", "journal")
        extra = dict(tab=tab)
        if tab == "accounts":
            extra["accounts"] = Account.all_objects.filter(tenant=tenant).order_by("code")
        elif tab == "till":
            extra["movements"] = ig.unclassified_cash_movements(tenant)
            extra["classify_accounts"] = Account.all_objects.filter(tenant=tenant, is_header=False, is_active=True).exclude(system_key="CASH").order_by("code")
        elif tab == "tools":
            extra["inventory_rows"] = ig.inventory_reconciliation(tenant)
        else:
            f = self.filter()
            qs = JournalEntry.all_objects.filter(tenant=tenant, entry_date__gte=f.start, entry_date__lte=f.end).select_related("branch", "created_by")
            if f.branch_ids is not None:
                qs = qs.filter(branch_id__in=f.branch_ids)
            if g.get("source"):
                qs = qs.filter(source_type=g["source"])
            if g.get("q"):
                qs = qs.filter(Q(reference__icontains=g["q"]) | Q(description__icontains=g["q"]))
            page = Paginator(qs.order_by("-entry_date", "-id"), 30).get_page(g.get("page"))
            for e in page:
                e.amount = e.total_debit
            sources = JournalEntry.all_objects.filter(tenant=tenant).values_list("source_type", flat=True).distinct().order_by("source_type")
            extra.update(page=page, sources=list(sources), **self.filter_ctx(f))
        return {**ctx, **self.ctx(**extra)}


class JournalDetailView(FinView):
    template_name = "accounting/journal_detail.html"
    area = section = "ledger"

    def get_context_data(self, pk, **kwargs):
        ctx = super().get_context_data(**kwargs)
        e = get_object_or_404(JournalEntry.all_objects.filter(tenant=self.request.tenant).select_related("branch", "created_by", "reverses"), pk=pk)
        lines = e.get_lines().select_related("branch").order_by("id")
        return {**ctx, **self.ctx(e=e, lines=lines, reversal=getattr(e, "reversed_by", None))}


class JournalReverseView(ActionView):
    area = "ledger"
    success_message = "Entry reversed. Both the original and the reversal stay on the books."

    def act(self, request, pk):
        if not all_branch_access(request.user):
            raise AccountingError("Not allowed.")
        e = get_object_or_404(JournalEntry.all_objects.filter(tenant=request.tenant), pk=pk)
        reverse_entry(e, user=request.user, reason=request.POST.get("reason", ""))

    def next_url(self, request, pk):
        return reverse("accounting:journal", args=[pk])


class JournalCreateView(FinView):
    template_name = "accounting/journal_form.html"
    area = section = "ledger"

    def _ctx(self, **extra):
        tenant = self.request.tenant
        return self.ctx(accounts=Account.all_objects.filter(tenant=tenant, is_header=False, is_active=True).order_by("code"),
                        branches=Branch.all_objects.filter(tenant=tenant).order_by("name"), token=uuid.uuid4().hex,
                        today=datetime.date.today(), **extra)

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), **self._ctx()}

    def post(self, request):
        if not all_branch_access(request.user):
            raise PermissionDenied
        p, tenant = request.POST, request.tenant
        lines, ids = [], p.getlist("account")
        for i, aid in enumerate(ids):
            if not aid:
                continue
            acct = Account.all_objects.filter(tenant=tenant, pk=aid).first()
            if acct is None:
                continue
            dr, cr = (p.getlist("debit")[i] or "0").replace(",", ""), (p.getlist("credit")[i] or "0").replace(",", "")
            lines.append(dict(account=acct, debit=dr, credit=cr, memo=p.getlist("memo")[i]))
        try:
            day = datetime.date.fromisoformat(p.get("entry_date", ""))
            branch = Branch.all_objects.filter(tenant=tenant, pk=p.get("branch") or 0).first()
            if not (p.get("description") or "").strip():
                raise AccountingError("Describe the entry.")
            for l in lines:
                l["debit"], l["credit"] = money(Decimal(l["debit"] or 0)), money(Decimal(l["credit"] or 0))
                l["branch"] = branch
            entry, created = post_journal(tenant=tenant, entry_date=day, description=p["description"].strip(), lines=lines,
                                          source_type="MANUAL", source_id=p.get("token", ""), source_key=f"manual:{p.get('token')}" if p.get("token") else None,
                                          branch=branch, user=request.user)
        except (AccountingError, ValueError, InvalidOperation) as exc:
            messages.error(request, str(exc) if isinstance(exc, AccountingError) else "Check the date and amounts.")
            return render(request, self.template_name, {**self.ctx(), **self._ctx(posted=p)})
        messages.success(request, f"Entry {entry.reference} posted.")
        return redirect("accounting:journal", pk=entry.pk)


class AccountCreateView(ServiceFormView):
    area = section = "ledger"
    form_class = F.AccountForm
    title, subtitle, cancel_url = "Add an account", "Extra accounts for your own chart. Codes must match the type.", "accounting:ledger"

    def perform(self, form):
        if not can_configure_accounting(self.request.user):
            raise AccountingError("Only the owner or an admin can change the chart of accounts.")
        d = form.cleaned_data
        a = Account(tenant=self.request.tenant, code=d["code"].strip(), name=d["name"].strip(), account_type=d["account_type"], description=d.get("description", ""))
        try:
            a.clean()
        except Exception as exc:
            raise AccountingError("; ".join(getattr(exc, "messages", [str(exc)])))
        if Account.all_objects.filter(tenant=a.tenant, code=a.code).exists():
            raise AccountingError("That account code is already used.")
        a.parent = Account.all_objects.filter(tenant=a.tenant, code=a.code[0] + "000", is_header=True).first()
        a.save()
        audit(a.tenant, self.request.user, "ACCOUNT_CREATED", a, code=a.code, name=a.name)
        messages.success(self.request, "Account added.")
        return redirect(reverse("accounting:ledger") + "?tab=accounts")


class AccountToggleView(ActionView):
    area = "ledger"
    success_message = "Account updated."

    def act(self, request, pk):
        if not can_configure_accounting(request.user):
            raise AccountingError("Only the owner or an admin can change the chart of accounts.")
        a = get_object_or_404(Account.all_objects.filter(tenant=request.tenant), pk=pk)
        if a.is_header:
            raise AccountingError("Group headings cannot be switched off.")
        a.is_active = not a.is_active
        a.save(update_fields=["is_active"])
        audit(a.tenant, request.user, "ACCOUNT_ACTIVE_CHANGED", a, active=a.is_active)

    def next_url(self, request, pk):
        return reverse("accounting:ledger") + "?tab=accounts"


class CatchUpView(ActionView):
    area = "ledger"

    def act(self, request):
        from django.core.cache import cache
        cache.delete(f"acct-hk:{request.tenant.pk}")
        made = ig.sync_ledger(request.tenant)
        ops.generate_recurring_expenses(request.tenant)
        total = sum(made.values())
        self.success_message = f"Ledger is up to date. {total} missing entr{'y' if total == 1 else 'ies'} posted."
        audit(request.tenant, request.user, "LEDGER_CATCH_UP", None, made=made)

    def next_url(self, request):
        return reverse("accounting:ledger") + "?tab=tools"


class ClassifyView(ActionView):
    area = "ledger"
    success_message = "Till movement posted to the ledger."

    def act(self, request, pk):
        from sales.models import CashTransaction
        t = get_object_or_404(CashTransaction.all_objects.filter(tenant=request.tenant).select_related("session__branch"), pk=pk)
        acct = Account.all_objects.filter(tenant=request.tenant, pk=request.POST.get("account") or 0).first()
        if acct is None:
            raise AccountingError("Choose an account.")
        ops.classify_cash_movement(t, user=request.user, account=acct)

    def next_url(self, request, pk):
        return reverse("accounting:ledger") + "?tab=till"


class RevalueView(ActionView):
    area = "ledger"

    def act(self, request):
        if not all_branch_access(request.user):
            raise AccountingError("Not allowed.")
        n = ig.post_inventory_revaluation(request.tenant, request.user)
        self.success_message = f"Inventory brought in line with stock value for {n} branch(es)." if n else "Inventory already matches stock value."

    def next_url(self, request):
        return reverse("accounting:ledger") + "?tab=tools"


class SettingsView(ServiceFormView):
    area = section = "ledger"
    form_class = F.SettingsForm
    title, subtitle, cancel_url = "Accounting settings", "Reporting basis, currency and financial year.", "accounting:ledger"

    def get_form(self, data=None, files=None):
        inst = get_settings(self.request.tenant)
        return F.SettingsForm(data, instance=inst) if data is not None else F.SettingsForm(instance=inst)

    def perform(self, form):
        if not can_configure_accounting(self.request.user):
            raise AccountingError("Only the owner or an admin can change accounting settings.")
        changed = {f: [str(form.initial.get(f)), str(form.cleaned_data[f])] for f in form.changed_data}
        form.save()
        audit(self.request.tenant, self.request.user, "SETTINGS_CHANGED", form.instance, changed=changed)
        messages.success(self.request, "Settings saved. Recorded entries are unchanged.")
        return redirect("accounting:ledger")
