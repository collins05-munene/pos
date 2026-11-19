"""
Business operations that create ledger entries: expenses, other income, owner money,
fixed assets, loans and register-movement classification.

Rules enforced here (spec phase 16): an expense is never 'paid' by being created; payments are
separate records; unpaid expenses sit in Outstanding Expenses (accrual) and never touch cash;
loan principal is never an expense; owner drawings are never an expense; asset purchases are
capitalised; every action is audited; all money is Decimal.
"""
import datetime
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from inventory.models import Branch

from .models import (METHOD_ACCOUNT_KEY, Account, AssetDepreciation, Expense, ExpenseAllocation, ExpenseCategory,
                     ExpensePayment, FixedAsset, Loan, LoanRepayment, OtherIncome, OwnerTransaction,
                     RecurringExpense)
from .scope import all_branch_access, branch_allowed
from .services import (ZERO, AccountingError, audit, get_account, get_settings, money, post_journal, reverse_entry)
from .models import JournalEntry

DEFAULT_CATEGORIES = [
    ("Rent", "EXP_RENT"), ("Salaries and wages", "EXP_SALARIES"), ("Delivery and transportation", "EXP_DELIVERY"),
    ("Utilities", "EXP_UTILITIES"), ("Marketing and advertising", "EXP_MARKETING"),
    ("Maintenance and repairs", "EXP_MAINTENANCE"), ("Communication", "EXP_COMMUNICATION"),
    ("Security", "EXP_SECURITY"), ("Licences and permits", "EXP_LICENCES"), ("Insurance", "EXP_INSURANCE"),
    ("Banking and transaction charges", "EXP_BANKING"), ("Packaging", "EXP_PACKAGING"),
    ("Professional services", "EXP_PROFESSIONAL"), ("Depreciation", "EXP_DEPRECIATION"),
    ("Loan interest and finance costs", "EXP_FINANCE"), ("Taxes", "TAX_EXPENSE"), ("Miscellaneous", "EXP_MISC"),
]


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def require_active(tenant):
    cfg = get_settings(tenant)
    if not cfg.opening_balance_date:
        raise AccountingError("Set up accounting first: choose the date your books start from on the Financial Overview.")
    return cfg


def _date(value, label="Date"):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        raise AccountingError(f"{label} is not a valid date.")


def _check_not_before_cutover(cfg, day, label):
    if day < cfg.opening_balance_date:
        raise AccountingError(f"{label} ({day:%d %b %Y}) is before your books start ({cfg.opening_balance_date:%d %b %Y}).")


def _branch(tenant, branch):
    if branch is None:
        return None
    if not isinstance(branch, Branch):
        branch = Branch.all_objects.filter(pk=branch, tenant=tenant).first()
    if branch is None or branch.tenant_id != tenant.pk:
        raise AccountingError("That branch does not belong to this business.")
    return branch


def _need_branch_rights(user, branch):
    if not branch_allowed(user, branch):
        raise AccountingError("You can only work with your own branch." if branch else
                              "Head-office entries are for the owner, admins and accountants.")


def _positive(amount, label="Amount"):
    amt = money(amount)
    if amt <= 0:
        raise AccountingError(f"{label} must be greater than zero.")
    return amt


def method_account(tenant, method, override=None):
    if override is not None:
        if override.tenant_id != tenant.pk or override.is_header or override.account_type != "ASSET":
            raise AccountingError("Choose one of your cash, bank or M-Pesa accounts.")
        return override
    if method not in METHOD_ACCOUNT_KEY:
        raise AccountingError("Choose how it was paid: cash, bank or M-Pesa.")
    return get_account(tenant, METHOD_ACCOUNT_KEY[method])


def _no_future(day, label):
    if day > datetime.date.today():
        raise AccountingError(f"{label} cannot be in the future.")


# --------------------------------------------------------------------------
# expense categories
# --------------------------------------------------------------------------
def seed_expense_categories(tenant):
    made = 0
    for name, key in DEFAULT_CATEGORIES:
        acct = Account.all_objects.filter(tenant=tenant, system_key=key).first()
        if acct is None:
            continue
        _, created = ExpenseCategory.all_objects.get_or_create(
            tenant=tenant, name=name, defaults=dict(account=acct, is_system=True))
        made += created
    return made


def create_category(*, tenant, user, name, account):
    name = (name or "").strip()
    if not name:
        raise AccountingError("Category name is required.")
    if ExpenseCategory.all_objects.filter(tenant=tenant, name__iexact=name).exists():
        raise AccountingError("A category with that name already exists.")
    cat = ExpenseCategory(tenant=tenant, name=name, account=account)
    try:
        cat.clean()
    except Exception as exc:
        raise AccountingError("; ".join(getattr(exc, "messages", [str(exc)])))
    cat.save()
    audit(tenant, user, "CATEGORY_CREATED", cat, name=name, account=account.code)
    return cat


def set_category_active(category, user, active):
    category.is_active = bool(active)
    category.save(update_fields=["is_active"])
    audit(category.tenant, user, "CATEGORY_ACTIVE_CHANGED", category, active=category.is_active)


# --------------------------------------------------------------------------
# expenses
# --------------------------------------------------------------------------
def _split(amount, parts):
    """parts: [(branch, percent)] -> [(branch, amount)] summing exactly to amount."""
    out, running = [], ZERO
    for i, (branch, pct) in enumerate(parts):
        share = amount - running if i == len(parts) - 1 else money(amount * Decimal(pct) / Decimal(100))
        out.append((branch, share))
        running += share
    return out


def _validate_allocations(tenant, allocations):
    if not allocations:
        return []
    clean = []
    for branch, pct in allocations:
        pct = Decimal(str(pct))
        if pct <= 0:
            raise AccountingError("Allocation percentages must be positive.")
        clean.append((_branch(tenant, branch), pct))
    if sum((p for _, p in clean), Decimal("0")) != Decimal("100"):
        raise AccountingError("Allocation percentages must add up to exactly 100.")
    if len({b.pk for b, _ in clean}) != len(clean):
        raise AccountingError("Each branch can appear only once in an allocation.")
    return clean


def _expense_lines(expense, allocations):
    cat_acct = expense.category.account
    if allocations:
        debit_lines = [dict(account=cat_acct, debit=amt, branch=b, memo=expense.title)
                       for b, amt in _split(expense.amount, allocations)]
    else:
        debit_lines = [dict(account=cat_acct, debit=expense.amount, branch=expense.branch, memo=expense.title)]
    credit = dict(account="ACCRUED_EXPENSES", credit=expense.amount, branch=expense.branch, memo=expense.title)
    return debit_lines + [credit]


def _post_expense(expense, user, allocations):
    entry, _ = post_journal(
        tenant=expense.tenant, entry_date=expense.expense_date, description=f"Expense: {expense.title}",
        lines=_expense_lines(expense, allocations), source_type="EXPENSE", source_id=expense.pk,
        source_key=f"expense:{expense.pk}:r{expense.revision}", branch=expense.branch, user=user)
    return entry


def _expense_entry(expense, revision=None):
    return JournalEntry.all_objects.filter(
        tenant=expense.tenant, source_key=f"expense:{expense.pk}:r{revision or expense.revision}").first()


def _allocations_of(expense):
    return [(a.branch, a.percent) for a in ExpenseAllocation.all_objects.filter(expense=expense).select_related("branch")]


def create_expense(*, tenant, user, title, category, amount, expense_date, branch=None, due_date=None, vendor="",
                   payment_method="", reference="", description="", receipt=None, allocations=None,
                   recurring=None, recurrence_date=None):
    cfg = require_active(tenant)
    if not (user.is_authenticated and (all_branch_access(user) or
                                       (user.role == "MANAGER" and cfg.managers_can_manage_expenses))):
        raise AccountingError("You do not have permission to record expenses.")
    title = (title or "").strip()
    if not title:
        raise AccountingError("Give the expense a title.")
    amount = _positive(amount)
    expense_date, due = _date(expense_date, "Expense date"), (_date(due_date, "Due date") if due_date else None)
    _check_not_before_cutover(cfg, expense_date, "Expense date")
    if due and due < expense_date:
        raise AccountingError("The due date cannot be before the expense date.")
    if category.tenant_id != tenant.pk:
        raise AccountingError("Unknown category.")
    if not category.is_active:
        raise AccountingError("That category is switched off. Choose another or reactivate it.")
    branch = _branch(tenant, branch)
    _need_branch_rights(user, branch)
    allocs = _validate_allocations(tenant, allocations)
    if allocs:
        if branch is not None:
            raise AccountingError("Allocate only head-office expenses (leave the branch blank).")
        for b, _ in allocs:
            _need_branch_rights(user, b)
    with transaction.atomic():
        expense = Expense.all_objects.create(
            tenant=tenant, title=title, category=category, amount=amount, expense_date=expense_date, due_date=due,
            branch=branch, vendor=vendor.strip(), payment_method=payment_method or "", reference=reference.strip(),
            description=description.strip(), created_by=user, recurring=recurring, recurrence_date=recurrence_date)
        if receipt:
            expense.receipt = receipt
            expense.save(update_fields=["receipt"])
        for b, pct in allocs:
            ExpenseAllocation.all_objects.create(tenant=tenant, expense=expense, branch=b, percent=pct)
        _post_expense(expense, user, allocs)
        audit(tenant, user, "EXPENSE_CREATED", expense, title=title, amount=str(amount),
              category=category.name, branch=getattr(branch, "name", "Head office"))
    return expense


def refresh_expense_status(expense):
    if expense.status == Expense.Status.CANCELLED:
        return expense.status
    paid = expense.amount_paid
    new = (Expense.Status.PAID if paid >= expense.amount else
           Expense.Status.PARTIAL if paid > 0 else Expense.Status.UNPAID)
    if new != expense.status:
        expense.status = new
        expense.save(update_fields=["status", "updated_at"])
    return new


FINANCIAL_FIELDS = ("amount", "category", "expense_date", "branch")


def update_expense(expense, *, user, allocations=None, **changes):
    """Edit an expense. Anything that changes the posted figures re-states the ledger via a reversal."""
    tenant = expense.tenant
    cfg = require_active(tenant)
    _need_branch_rights(user, expense.branch)
    if expense.status == Expense.Status.CANCELLED:
        raise AccountingError("A cancelled expense cannot be edited.")
    old = {f: getattr(expense, f) for f in ("title", "amount", "category", "expense_date", "due_date", "branch",
                                           "vendor", "payment_method", "reference", "description")}
    if "amount" in changes:
        changes["amount"] = _positive(changes["amount"])
    if "branch" in changes:
        changes["branch"] = _branch(tenant, changes["branch"])
        _need_branch_rights(user, changes["branch"])
    for f in ("expense_date", "due_date"):
        if changes.get(f):
            changes[f] = _date(changes[f], f.replace("_", " ").capitalize())
    if changes.get("expense_date"):
        _check_not_before_cutover(cfg, changes["expense_date"], "Expense date")
    receipt = changes.pop("receipt", None)
    new_allocs = _validate_allocations(tenant, allocations) if allocations is not None else None

    restate = any(f in changes and changes[f] != old[f] for f in FINANCIAL_FIELDS) or \
        (new_allocs is not None and new_allocs != _allocations_of(expense))
    with transaction.atomic():
        expense = Expense.all_objects.select_for_update().get(pk=expense.pk)
        if restate and expense.amount_paid > 0:
            raise AccountingError("This expense already has payments. Void them before changing the amount, "
                                  "category, date or branch.")
        if restate and changes.get("amount") and changes["amount"] < expense.amount_paid:
            raise AccountingError("The new amount is less than what has already been paid.")
        prior = _expense_entry(expense)
        for f, v in changes.items():
            setattr(expense, f, v)
        if expense.due_date and expense.due_date < expense.expense_date:
            raise AccountingError("The due date cannot be before the expense date.")
        if restate:
            if prior is not None and prior.status == "POSTED":
                reverse_entry(prior, user=user, reason="Expense corrected")
            expense.revision += 1
        if receipt:
            expense.receipt = receipt
        expense.save()
        if new_allocs is not None:
            ExpenseAllocation.all_objects.filter(expense=expense).delete()
            for b, pct in new_allocs:
                ExpenseAllocation.all_objects.create(tenant=tenant, expense=expense, branch=b, percent=pct)
        if restate:
            _post_expense(expense, user, new_allocs if new_allocs is not None else _allocations_of(expense))
        refresh_expense_status(expense)
        changed = {f: [str(old[f]), str(getattr(expense, f))] for f in old if old[f] != getattr(expense, f)}
        audit(tenant, user, "EXPENSE_UPDATED", expense, changed=changed, restated=restate)
    return expense


def record_expense_payment(expense, *, user, amount, payment_date, method, reference="", account=None):
    tenant = expense.tenant
    cfg = require_active(tenant)
    _need_branch_rights(user, expense.branch)
    amount = _positive(amount)
    payment_date = _date(payment_date, "Payment date")
    _no_future(payment_date, "Payment date")
    _check_not_before_cutover(cfg, payment_date, "Payment date")
    paying_acct = method_account(tenant, method, account)
    with transaction.atomic():
        locked = Expense.all_objects.select_for_update().get(pk=expense.pk)
        if locked.status == Expense.Status.CANCELLED:
            raise AccountingError("This expense was cancelled; it cannot be paid.")
        if payment_date < locked.expense_date:
            raise AccountingError("A payment cannot be dated before the expense.")
        if amount > locked.balance:
            raise AccountingError(f"Payment of {amount} is more than the balance still owed ({locked.balance}).")
        pay = ExpensePayment.all_objects.create(
            tenant=tenant, expense=locked, amount=amount, payment_date=payment_date, payment_method=method,
            account=paying_acct, reference=reference.strip(), paid_by=user)
        post_journal(
            tenant=tenant, entry_date=payment_date, description=f"Payment: {locked.title}",
            lines=[dict(account="ACCRUED_EXPENSES", debit=amount, branch=locked.branch, memo=locked.title),
                   dict(account=paying_acct, credit=amount, branch=locked.branch, memo=reference.strip())],
            source_type="EXPENSE_PAYMENT", source_id=pay.pk, source_key=f"expense-payment:{pay.pk}",
            branch=locked.branch, user=user)
        refresh_expense_status(locked)
        audit(tenant, user, "EXPENSE_PAYMENT", locked, payment=pay.pk, amount=str(amount), method=method)
    expense.refresh_from_db()
    return pay


def void_expense_payment(payment, *, user, reason):
    tenant = payment.tenant
    _need_branch_rights(user, payment.expense.branch)
    if not (reason or "").strip():
        raise AccountingError("Give a reason for voiding this payment.")
    with transaction.atomic():
        payment = ExpensePayment.all_objects.select_for_update().get(pk=payment.pk)
        if payment.is_voided:
            raise AccountingError("This payment is already voided.")
        entry = JournalEntry.all_objects.filter(tenant=tenant, source_key=f"expense-payment:{payment.pk}").first()
        if entry is not None:
            reverse_entry(entry, user=user, reason=f"Payment voided: {reason.strip()}")
        payment.is_voided, payment.voided_at, payment.voided_by = True, timezone.now(), user
        payment.void_reason = reason.strip()[:255]
        payment.save()
        refresh_expense_status(payment.expense)
        audit(tenant, user, "EXPENSE_PAYMENT_VOIDED", payment.expense, payment=payment.pk, reason=reason.strip())
    return payment


def cancel_expense(expense, *, user, reason):
    tenant = expense.tenant
    _need_branch_rights(user, expense.branch)
    if not (reason or "").strip():
        raise AccountingError("Give a reason for cancelling this expense.")
    with transaction.atomic():
        expense = Expense.all_objects.select_for_update().get(pk=expense.pk)
        if expense.status == Expense.Status.CANCELLED:
            raise AccountingError("This expense is already cancelled.")
        if expense.amount_paid > 0:
            raise AccountingError("This expense has payments. Void them first, then cancel.")
        entry = _expense_entry(expense)
        if entry is not None and entry.status == "POSTED":
            reverse_entry(entry, user=user, reason=f"Expense cancelled: {reason.strip()}")
        expense.status, expense.cancelled_at = Expense.Status.CANCELLED, timezone.now()
        expense.cancel_reason = reason.strip()[:255]
        expense.save()
        audit(tenant, user, "EXPENSE_CANCELLED", expense, reason=reason.strip())
    return expense


# --------------------------------------------------------------------------
# recurring expenses
# --------------------------------------------------------------------------
def _add_months(day, months, anchor_day):
    y, m = divmod(day.year * 12 + day.month - 1 + months, 12)
    m += 1
    import calendar
    return datetime.date(y, m, min(anchor_day, calendar.monthrange(y, m)[1]))


def occurrence_dates(rec, until):
    """Every date this schedule falls on, from its start up to `until` (inclusive) and its end date."""
    stop = min(until, rec.end_date) if rec.end_date else until
    out, i, d = [], 0, rec.start_date
    while d <= stop and i < 5000:
        out.append(d)
        i += 1
        if rec.frequency == "DAILY":
            d = rec.start_date + datetime.timedelta(days=i)
        elif rec.frequency == "WEEKLY":
            d = rec.start_date + datetime.timedelta(weeks=i)
        elif rec.frequency == "MONTHLY":
            d = _add_months(rec.start_date, i, rec.start_date.day)
        elif rec.frequency == "QUARTERLY":
            d = _add_months(rec.start_date, 3 * i, rec.start_date.day)
        else:
            d = _add_months(rec.start_date, 12 * i, rec.start_date.day)
    return out


def create_recurring_expense(*, tenant, user, title, category, amount, frequency, start_date, end_date=None,
                             branch=None, vendor="", payment_method="", due_after_days=0, description=""):
    require_active(tenant)
    if not all_branch_access(user):
        raise AccountingError("Only the owner, admins and accountants can set up recurring expenses.")
    start_date = _date(start_date, "Start date")
    if end_date and _date(end_date, "End date") < start_date:
        raise AccountingError("The end date cannot be before the start date.")
    rec = RecurringExpense.all_objects.create(
        tenant=tenant, title=title.strip(), category=category, amount=_positive(amount), frequency=frequency,
        start_date=start_date, end_date=_date(end_date) if end_date else None, branch=_branch(tenant, branch),
        vendor=vendor.strip(), payment_method=payment_method or "", due_after_days=due_after_days,
        description=description.strip(), created_by=user)
    audit(tenant, user, "RECURRING_CREATED", rec, title=rec.title, frequency=frequency, amount=str(rec.amount))
    return rec


def generate_recurring_expenses(tenant, today=None):
    """
    Create every due occurrence that does not exist yet. Safe to run any number of times and from
    several processes: the unique (recurring, recurrence_date) constraint makes a duplicate impossible.
    Generated expenses start UNPAID. They are never marked paid automatically.
    """
    cfg = get_settings(tenant)
    if not cfg.opening_balance_date:
        return 0
    today = today or datetime.date.today()
    created = 0
    for rec in RecurringExpense.all_objects.filter(tenant=tenant, is_active=True).select_related("category", "created_by"):
        if not rec.category.is_active:
            continue
        have = set(Expense.all_objects.filter(recurring=rec).values_list("recurrence_date", flat=True))
        for day in occurrence_dates(rec, today):
            if day in have or day < cfg.opening_balance_date:
                continue
            due = day + datetime.timedelta(days=rec.due_after_days) if rec.due_after_days else None
            try:
                with transaction.atomic():
                    _create_generated(rec, day, due)
                created += 1
            except IntegrityError:
                continue    # another worker made it first
    return created


def _create_generated(rec, day, due):
    tenant, user = rec.tenant, rec.created_by
    expense = Expense.all_objects.create(
        tenant=tenant, title=rec.title, category=rec.category, amount=rec.amount, expense_date=day, due_date=due,
        branch=rec.branch, vendor=rec.vendor, payment_method=rec.payment_method, description=rec.description,
        created_by=user, recurring=rec, recurrence_date=day)
    _post_expense(expense, user, [])
    audit(tenant, user, "EXPENSE_GENERATED", expense, recurring=rec.pk, date=day.isoformat())
    return expense


def set_recurring_active(rec, user, active):
    rec.is_active = bool(active)
    rec.save(update_fields=["is_active"])
    audit(rec.tenant, user, "RECURRING_ACTIVE_CHANGED", rec, active=rec.is_active)


# --------------------------------------------------------------------------
# other income
# --------------------------------------------------------------------------
def record_other_income(*, tenant, user, title, kind, amount, income_date, received_on=None, payment_method="",
                        branch=None, payer="", reference="", description=""):
    cfg = require_active(tenant)
    if not all_branch_access(user):
        raise AccountingError("Only the owner, admins and accountants can record other income.")
    amount = _positive(amount)
    income_date = _date(income_date, "Income date")
    _check_not_before_cutover(cfg, income_date, "Income date")
    branch = _branch(tenant, branch)
    received = _date(received_on, "Received date") if received_on else None
    if received:
        _no_future(received, "Received date")
        if received < income_date:
            raise AccountingError("Money cannot be received before it was earned.")
        _check_not_before_cutover(cfg, received, "Received date")
        method_account(tenant, payment_method)
    income_key = OtherIncome.KIND_ACCOUNT[kind]
    with transaction.atomic():
        inc = OtherIncome.all_objects.create(
            tenant=tenant, title=title.strip(), kind=kind, amount=amount, income_date=income_date,
            received_on=received, payment_method=payment_method if received else "", branch=branch,
            payer=payer.strip(), reference=reference.strip(), description=description.strip(), created_by=user)
        money_acct = method_account(tenant, payment_method) if received else None
        same_day = received == income_date
        # Earned: Dr (cash if already received that day, else Receivable)  Cr income
        post_journal(tenant=tenant, entry_date=income_date, description=f"Income: {inc.title}",
                     lines=[dict(account=money_acct if same_day else "AR", debit=amount, branch=branch, memo=inc.title),
                            dict(account=income_key, credit=amount, branch=branch, memo=inc.title)],
                     source_type="OTHER_INCOME", source_id=inc.pk, source_key=f"income:{inc.pk}",
                     branch=branch, user=user)
        if received and not same_day:      # money arrived on a later day
            post_journal(tenant=tenant, entry_date=received, description=f"Received: {inc.title}",
                         lines=[dict(account=money_acct, debit=amount, branch=branch),
                                dict(account="AR", credit=amount, branch=branch)],
                         source_type="OTHER_INCOME_RECEIPT", source_id=inc.pk, source_key=f"income-receipt:{inc.pk}",
                         branch=branch, user=user)
        audit(tenant, user, "OTHER_INCOME_RECORDED", inc, amount=str(amount), kind=kind, received=bool(received))
    return inc


def mark_income_received(income, *, user, received_on, payment_method):
    tenant = income.tenant
    cfg = require_active(tenant)
    if not all_branch_access(user):
        raise AccountingError("Only the owner, admins and accountants can record receipts.")
    received_on = _date(received_on, "Received date")
    _no_future(received_on, "Received date")
    _check_not_before_cutover(cfg, received_on, "Received date")
    acct = method_account(tenant, payment_method)
    with transaction.atomic():
        income = OtherIncome.all_objects.select_for_update().get(pk=income.pk)
        if income.is_cancelled:
            raise AccountingError("This income was cancelled.")
        if income.received_on:
            raise AccountingError("This income has already been received.")
        if received_on < income.income_date:
            raise AccountingError("Money cannot be received before it was earned.")
        post_journal(tenant=tenant, entry_date=received_on, description=f"Received: {income.title}",
                     lines=[dict(account=acct, debit=income.amount, branch=income.branch),
                            dict(account="AR", credit=income.amount, branch=income.branch)],
                     source_type="OTHER_INCOME_RECEIPT", source_id=income.pk, source_key=f"income-receipt:{income.pk}",
                     branch=income.branch, user=user)
        income.received_on, income.payment_method = received_on, payment_method
        income.save(update_fields=["received_on", "payment_method"])
        audit(tenant, user, "OTHER_INCOME_RECEIVED", income, amount=str(income.amount), method=payment_method)
    return income


def cancel_other_income(income, *, user, reason):
    tenant = income.tenant
    if not all_branch_access(user):
        raise AccountingError("Only the owner, admins and accountants can cancel income.")
    if not (reason or "").strip():
        raise AccountingError("Give a reason for cancelling this income.")
    with transaction.atomic():
        income = OtherIncome.all_objects.select_for_update().get(pk=income.pk)
        if income.is_cancelled:
            raise AccountingError("This income is already cancelled.")
        for key in (f"income-receipt:{income.pk}", f"income:{income.pk}"):
            entry = JournalEntry.all_objects.filter(tenant=tenant, source_key=key, status="POSTED").first()
            if entry:
                reverse_entry(entry, user=user, reason=f"Income cancelled: {reason.strip()}")
        income.is_cancelled = True
        income.save(update_fields=["is_cancelled"])
        audit(tenant, user, "OTHER_INCOME_CANCELLED", income, reason=reason.strip())
    return income


# --------------------------------------------------------------------------
# owner capital / drawings
# --------------------------------------------------------------------------
def record_owner_transaction(*, tenant, user, kind, amount, txn_date, payment_method, branch=None, reference="", notes=""):
    cfg = require_active(tenant)
    if not (user.is_owner or user.role == "ADMIN"):
        raise AccountingError("Only the owner or an admin can record owner capital and withdrawals.")
    amount = _positive(amount)
    txn_date = _date(txn_date, "Date")
    _no_future(txn_date, "Date")
    _check_not_before_cutover(cfg, txn_date, "Date")
    branch = _branch(tenant, branch)
    acct = method_account(tenant, payment_method)
    with transaction.atomic():
        t = OwnerTransaction.all_objects.create(
            tenant=tenant, kind=kind, amount=amount, txn_date=txn_date, payment_method=payment_method, branch=branch,
            reference=reference.strip(), notes=notes.strip(), created_by=user)
        if kind == "CAPITAL":
            lines = [dict(account=acct, debit=amount, branch=branch), dict(account="OWNER_CAPITAL", credit=amount, branch=branch)]
        else:
            lines = [dict(account="OWNER_DRAWINGS", debit=amount, branch=branch), dict(account=acct, credit=amount, branch=branch)]
        post_journal(tenant=tenant, entry_date=txn_date, description=t.get_kind_display(), lines=lines,
                     source_type="OWNER_" + kind, source_id=t.pk, source_key=f"owner:{t.pk}", branch=branch, user=user)
        audit(tenant, user, "OWNER_TRANSACTION", t, kind=kind, amount=str(amount))
    return t


# --------------------------------------------------------------------------
# fixed assets
# --------------------------------------------------------------------------
def register_asset(*, tenant, user, name, category, cost, purchase_date, useful_life_months, residual_value=0,
                   branch=None, paid_with="", existing_asset=False, opening_accumulated_depreciation=0, notes=""):
    cfg = require_active(tenant)
    if not all_branch_access(user):
        raise AccountingError("Only the owner, admins and accountants can manage fixed assets.")
    cost, residual = _positive(cost, "Cost"), money(residual_value)
    if residual < 0 or residual >= cost:
        raise AccountingError("Residual value must be zero or more, and less than the cost.")
    if int(useful_life_months) < 1:
        raise AccountingError("Useful life must be at least one month.")
    purchase_date = _date(purchase_date, "Purchase date")
    opening_dep = money(opening_accumulated_depreciation)
    if opening_dep < 0 or opening_dep > cost - residual:
        raise AccountingError("Opening accumulated depreciation cannot exceed cost minus residual value.")
    if existing_asset:
        if purchase_date >= cfg.opening_balance_date:
            raise AccountingError("An existing asset must have been bought before your books start.")
    else:
        opening_dep = ZERO
        _check_not_before_cutover(cfg, purchase_date, "Purchase date")
        if paid_with not in ("CASH", "BANK", "MPESA", "CREDIT"):
            raise AccountingError("Say how the asset was paid for (cash, bank, M-Pesa or on credit).")
    branch = _branch(tenant, branch)
    with transaction.atomic():
        asset = FixedAsset.all_objects.create(
            tenant=tenant, name=name.strip(), category=category, cost=cost, purchase_date=purchase_date, branch=branch,
            useful_life_months=int(useful_life_months), residual_value=residual, paid_with=paid_with or "",
            existing_asset=existing_asset, opening_accumulated_depreciation=opening_dep, notes=notes.strip(), created_by=user)
        if not existing_asset:        # capitalised, never expensed
            credit = get_account(tenant, "AP") if paid_with == "CREDIT" else method_account(tenant, paid_with)
            post_journal(tenant=tenant, entry_date=purchase_date, description=f"Asset purchase: {asset.name}",
                         lines=[dict(account="FIXED_ASSETS", debit=cost, branch=branch, memo=asset.name),
                                dict(account=credit, credit=cost, branch=branch, memo=asset.name)],
                         source_type="ASSET_PURCHASE", source_id=asset.pk, source_key=f"asset:{asset.pk}",
                         branch=branch, user=user)
        audit(tenant, user, "ASSET_REGISTERED", asset, name=asset.name, cost=str(cost), existing=existing_asset)
    return asset


def _month_end(day):
    import calendar
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _next_month_end(me):
    return _month_end(me + datetime.timedelta(days=1))


def generate_depreciation(tenant, up_to=None, user=None):
    """Post straight-line depreciation for every month-end up to `up_to`. Idempotent: one row per asset per month."""
    cfg = get_settings(tenant)
    if not cfg.opening_balance_date:
        return 0
    up_to = up_to or datetime.date.today()
    posted = 0
    tolerance = Decimal("0.50")        # absorbs cent rounding so the last month lands exactly on residual value
    for asset in FixedAsset.all_objects.filter(tenant=tenant, status="ACTIVE").select_related("branch"):
        monthly = money(asset.depreciable_amount / Decimal(asset.useful_life_months))
        me = _month_end(max(asset.purchase_date, cfg.opening_balance_date))
        while me <= up_to:
            if AssetDepreciation.all_objects.filter(asset=asset, period_end=me).exists():
                me = _next_month_end(me)
                continue
            remaining = asset.depreciable_amount - asset.accumulated_depreciation
            if remaining <= 0:
                break
            amount = remaining if remaining <= monthly + tolerance else monthly
            with transaction.atomic():
                AssetDepreciation.all_objects.create(tenant=tenant, asset=asset, period_end=me, amount=amount)
                post_journal(tenant=tenant, entry_date=me, description=f"Depreciation: {asset.name}",
                             lines=[dict(account="EXP_DEPRECIATION", debit=amount, branch=asset.branch),
                                    dict(account="ACCUM_DEPRECIATION", credit=amount, branch=asset.branch)],
                             source_type="DEPRECIATION", source_id=asset.pk,
                             source_key=f"depr:{asset.pk}:{me.isoformat()}", branch=asset.branch, user=user,
                             audit_it=False)
            posted += 1
            me = _next_month_end(me)
    if posted:
        audit(tenant, user, "DEPRECIATION_RUN", None, months_posted=posted, up_to=up_to.isoformat())
    return posted


def dispose_asset(asset, *, user, disposal_date, proceeds=0, method=""):
    tenant = asset.tenant
    cfg = require_active(tenant)
    if not all_branch_access(user):
        raise AccountingError("Only the owner, admins and accountants can dispose of assets.")
    disposal_date = _date(disposal_date, "Disposal date")
    _no_future(disposal_date, "Disposal date")
    _check_not_before_cutover(cfg, disposal_date, "Disposal date")
    proceeds = money(proceeds)
    if proceeds < 0:
        raise AccountingError("Proceeds cannot be negative.")
    if proceeds > 0:
        method_account(tenant, method)
    with transaction.atomic():
        asset = FixedAsset.all_objects.select_for_update().get(pk=asset.pk)
        if asset.status == "DISPOSED":
            raise AccountingError("This asset is already disposed of.")
        if disposal_date < asset.purchase_date:
            raise AccountingError("Disposal cannot be before the purchase date.")
        prev_month_end = disposal_date.replace(day=1) - datetime.timedelta(days=1)
        generate_depreciation(tenant, up_to=prev_month_end, user=user)
        asset.refresh_from_db()
        accum, cost = asset.accumulated_depreciation, asset.cost
        book = cost - accum
        diff = proceeds - book            # >0 gain, <0 loss
        lines = [dict(account="FIXED_ASSETS", credit=cost, branch=asset.branch, memo=asset.name)]
        if accum > 0:
            lines.append(dict(account="ACCUM_DEPRECIATION", debit=accum, branch=asset.branch))
        if proceeds > 0:
            lines.append(dict(account=method_account(tenant, method), debit=proceeds, branch=asset.branch))
        if diff > 0:
            lines.append(dict(account="INCOME_OTHER", credit=diff, branch=asset.branch, memo="Gain on disposal"))
        elif diff < 0:
            lines.append(dict(account="EXP_MISC", debit=-diff, branch=asset.branch, memo="Loss on disposal"))
        post_journal(tenant=tenant, entry_date=disposal_date, description=f"Asset disposal: {asset.name}", lines=lines,
                     source_type="ASSET_DISPOSAL", source_id=asset.pk, source_key=f"asset-disposal:{asset.pk}",
                     branch=asset.branch, user=user)
        asset.status, asset.disposal_date, asset.disposal_proceeds = "DISPOSED", disposal_date, proceeds
        asset.disposal_method = method if proceeds > 0 else ""
        asset.save()
        audit(tenant, user, "ASSET_DISPOSED", asset, proceeds=str(proceeds), book_value=str(book))
    return asset


# --------------------------------------------------------------------------
# loans
# --------------------------------------------------------------------------
PERIODS_PER_YEAR = {"WEEKLY": 52, "MONTHLY": 12, "QUARTERLY": 4}


def record_loan(*, tenant, user, lender, principal, interest_rate_percent, start_date, installments, frequency,
                received_via="", branch=None, existing_loan=False, opening_outstanding=None, notes=""):
    cfg = require_active(tenant)
    if not (user.is_owner or user.role in ("ADMIN", "ACCOUNTANT")):
        raise AccountingError("Only the owner, admins and accountants can record loans.")
    principal = _positive(principal, "Principal")
    start_date = _date(start_date, "Start date")
    if Decimal(str(interest_rate_percent)) < 0:
        raise AccountingError("Interest rate cannot be negative.")
    if int(installments) < 1:
        raise AccountingError("A loan needs at least one repayment instalment.")
    branch = _branch(tenant, branch)
    if existing_loan:
        if start_date >= cfg.opening_balance_date:
            raise AccountingError("An existing loan must have started before your books start.")
        opening_outstanding = _positive(opening_outstanding if opening_outstanding is not None else 0,
                                        "Outstanding balance at the start of your books")
        if opening_outstanding > principal:
            raise AccountingError("The outstanding balance cannot exceed the original principal.")
    else:
        opening_outstanding = None
        _check_not_before_cutover(cfg, start_date, "Start date")
        method_account(tenant, received_via)
    with transaction.atomic():
        loan = Loan.all_objects.create(
            tenant=tenant, lender=lender.strip(), principal=principal, interest_rate_percent=Decimal(str(interest_rate_percent)),
            start_date=start_date, installments=int(installments), frequency=frequency, received_via=received_via or "",
            branch=branch, existing_loan=existing_loan, opening_outstanding=opening_outstanding, notes=notes.strip(), created_by=user)
        if not existing_loan:
            post_journal(tenant=tenant, entry_date=start_date, description=f"Loan received: {loan.lender}",
                         lines=[dict(account=method_account(tenant, received_via), debit=principal, branch=branch),
                                dict(account="LOANS", credit=principal, branch=branch, memo=loan.lender)],
                         source_type="LOAN_RECEIPT", source_id=loan.pk, source_key=f"loan:{loan.pk}", branch=branch, user=user)
        audit(tenant, user, "LOAN_RECORDED", loan, lender=loan.lender, principal=str(principal), existing=existing_loan)
    return loan


def repay_loan(loan, *, user, paid_on, principal_paid, interest_paid, payment_method, reference=""):
    tenant = loan.tenant
    cfg = require_active(tenant)
    if not (user.is_owner or user.role in ("ADMIN", "ACCOUNTANT")):
        raise AccountingError("Only the owner, admins and accountants can record loan repayments.")
    principal_paid, interest_paid = money(principal_paid), money(interest_paid)
    if principal_paid < 0 or interest_paid < 0 or principal_paid + interest_paid <= 0:
        raise AccountingError("Enter the principal and/or interest being paid.")
    paid_on = _date(paid_on, "Payment date")
    _no_future(paid_on, "Payment date")
    _check_not_before_cutover(cfg, paid_on, "Payment date")
    acct = method_account(tenant, payment_method)
    with transaction.atomic():
        loan = Loan.all_objects.select_for_update().get(pk=loan.pk)
        if loan.is_closed:
            raise AccountingError("This loan is fully repaid.")
        if paid_on < loan.start_date:
            raise AccountingError("A repayment cannot be dated before the loan started.")
        if principal_paid > loan.outstanding_principal:
            raise AccountingError(f"Principal paid ({principal_paid}) exceeds what is still owed ({loan.outstanding_principal}).")
        rep = LoanRepayment.all_objects.create(
            tenant=tenant, loan=loan, paid_on=paid_on, principal_paid=principal_paid, interest_paid=interest_paid,
            payment_method=payment_method, reference=reference.strip(), paid_by=user)
        lines = []
        if principal_paid > 0:        # principal reduces the liability: it is NOT an expense
            lines.append(dict(account="LOANS", debit=principal_paid, branch=loan.branch, memo="Principal"))
        if interest_paid > 0:
            lines.append(dict(account="EXP_FINANCE", debit=interest_paid, branch=loan.branch, memo="Interest"))
        lines.append(dict(account=acct, credit=principal_paid + interest_paid, branch=loan.branch, memo=reference.strip()))
        post_journal(tenant=tenant, entry_date=paid_on, description=f"Loan repayment: {loan.lender}", lines=lines,
                     source_type="LOAN_REPAYMENT", source_id=rep.pk, source_key=f"loan-repay:{rep.pk}",
                     branch=loan.branch, user=user)
        if loan.outstanding_principal <= 0:
            loan.is_closed = True
            loan.save(update_fields=["is_closed"])
        audit(tenant, user, "LOAN_REPAYMENT", loan, principal=str(principal_paid), interest=str(interest_paid))
    return rep


def loan_schedule(loan):
    """Planned repayments (equal principal, interest on the reducing balance). A guide, not a posting."""
    n, per_year = loan.installments, Decimal(PERIODS_PER_YEAR[loan.frequency])
    start = loan.opening_outstanding if loan.existing_loan and loan.opening_outstanding is not None else loan.principal
    balance, rows = start, []
    step = {"WEEKLY": 7, "MONTHLY": 30, "QUARTERLY": 91}[loan.frequency]
    base = money(start / n)
    for i in range(1, n + 1):
        interest = money(balance * (loan.interest_rate_percent / Decimal(100)) / per_year)
        principal = balance if i == n else base
        if loan.frequency == "MONTHLY":
            due = _add_months(loan.start_date, i, loan.start_date.day)
        else:
            due = loan.start_date + datetime.timedelta(days=step * i)
        rows.append(dict(no=i, due=due, opening=balance, principal=principal, interest=interest,
                         payment=principal + interest, closing=balance - principal))
        balance -= principal
    return rows


# --------------------------------------------------------------------------
# cash register movements that need a ledger account
# --------------------------------------------------------------------------
def classify_cash_movement(cash_txn, *, user, account):
    """
    A manual Cash In / Cash Out in the till is just 'money moved'. The accountant says what it was
    (owner capital, a bank deposit, a petty expense...) and this posts it. Idempotent.
    """
    tenant = cash_txn.tenant
    require_active(tenant)
    if not all_branch_access(user):
        raise AccountingError("Only the owner, admins and accountants can classify register movements.")
    if account.tenant_id != tenant.pk or account.is_header or not account.is_active:
        raise AccountingError("Choose a valid account.")
    if hasattr(cash_txn, "purchase_payment"):
        raise AccountingError("This is a supplier payment and is already recorded.")
    cash = get_account(tenant, "CASH")
    if account.pk == cash.pk:
        raise AccountingError("Choose the account the money came from or went to, not Cash on Hand.")
    amount = money(cash_txn.amount)
    branch = cash_txn.session.branch
    day = timezone.localdate(cash_txn.created_at)
    if cash_txn.transaction_type == "CASH_IN":
        lines = [dict(account=cash, debit=amount, branch=branch, memo=cash_txn.reason),
                 dict(account=account, credit=amount, branch=branch, memo=cash_txn.reason)]
    else:
        lines = [dict(account=account, debit=amount, branch=branch, memo=cash_txn.reason),
                 dict(account=cash, credit=amount, branch=branch, memo=cash_txn.reason)]
    entry, created = post_journal(tenant=tenant, entry_date=max(day, get_settings(tenant).opening_balance_date),
                                  description=f"Till {cash_txn.get_transaction_type_display()}: {cash_txn.reason}"[:255],
                                  lines=lines, source_type="CASH_MOVEMENT", source_id=cash_txn.pk,
                                  source_key=f"cashtxn:{cash_txn.pk}", branch=branch, user=user)
    return entry
