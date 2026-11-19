"""
The only code allowed to write to the ledger.

    post_journal()      balanced, atomic, idempotent posting
    reverse_entry()     the one way to correct a posted entry
    trial_balance()     debits/credits by account, summed live from journal lines
"""
import calendar
import datetime
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum

from tenants.models import next_number

from .defaults import ACCOUNTS, HEADERS
from .models import (Account, AccountingSettings, FinancialAuditLog, FiscalPeriod, JournalEntry, JournalLine)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class AccountingError(Exception):
    """A posting or ledger rule was broken. The message is safe to show the user."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def money(value):
    """Decimal at 2dp. Floats are refused: they cannot represent money exactly."""
    if isinstance(value, float):
        raise AccountingError("Money must be a Decimal or string, never a float.")
    try:
        return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:
        raise AccountingError(f"Invalid amount: {value!r}")


def audit(tenant, user, action, obj=None, **details):
    return FinancialAuditLog.all_objects.create(
        tenant=tenant, user=user if getattr(user, "pk", None) else None, action=action,
        object_type=obj.__class__.__name__ if obj is not None else "",
        object_id=str(obj.pk) if obj is not None else "", details=details)


def get_settings(tenant):
    obj, _ = AccountingSettings.all_objects.get_or_create(tenant=tenant)
    return obj


def get_account(tenant, system_key):
    try:
        return Account.all_objects.get(tenant=tenant, system_key=system_key)
    except Account.DoesNotExist:
        raise AccountingError(f"Account '{system_key}' is missing. Run accounting setup for this business.")


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------
@transaction.atomic
def seed_chart_of_accounts(tenant):
    """Create the default chart. Safe to run again: existing accounts are left exactly as they are."""
    headers = {}
    for code, name, typ in HEADERS:
        acct, _ = Account.all_objects.get_or_create(
            tenant=tenant, code=code,
            defaults=dict(name=name, account_type=typ, is_header=True, is_system=True))
        headers[code[0]] = acct
    created = 0
    for code, name, typ, key, contra in ACCOUNTS:
        existing = Account.all_objects.filter(tenant=tenant).filter(Q(system_key=key) | Q(code=code)).first()
        if existing:
            continue
        Account.all_objects.create(tenant=tenant, code=code, name=name, account_type=typ, system_key=key,
                                   is_contra=contra, is_system=True, parent=headers[code[0]])
        created += 1
    from .operations import seed_expense_categories
    seed_expense_categories(tenant)
    settings_row = get_settings(tenant)
    if not settings_row.is_initialised:
        settings_row.is_initialised = True
        settings_row.save(update_fields=["is_initialised", "updated_at"])
    return created


def period_for(tenant, day):
    """The calendar-month fiscal period containing `day` (created on first use)."""
    start = day.replace(day=1)
    end = day.replace(day=calendar.monthrange(day.year, day.month)[1])
    period, _ = FiscalPeriod.all_objects.get_or_create(
        tenant=tenant, start_date=start, defaults=dict(name=start.strftime("%b %Y"), end_date=end))
    return period


# --------------------------------------------------------------------------
# posting
# --------------------------------------------------------------------------
def _normalise_lines(tenant, lines, default_branch):
    out = []
    for raw in lines:
        acct = raw["account"]
        if isinstance(acct, str):
            acct = get_account(tenant, acct)
        if acct.tenant_id != tenant.pk:
            raise AccountingError("Account belongs to another business.")
        if acct.is_header or not acct.is_active:
            raise AccountingError(f"Cannot post to {acct.code} {acct.name}: it is a heading or inactive.")
        debit, credit = money(raw.get("debit", 0)), money(raw.get("credit", 0))
        if debit < 0 or credit < 0:
            raise AccountingError("Debit and credit amounts cannot be negative.")
        if debit == credit:      # both zero, or both set
            if debit == 0:
                continue         # drop empty lines silently: they carry no meaning
            raise AccountingError("A line must be either a debit or a credit, not both.")
        if debit and credit:
            raise AccountingError("A line must be either a debit or a credit, not both.")
        out.append(dict(account=acct, debit=debit, credit=credit,
                        branch=raw.get("branch", default_branch), memo=(raw.get("memo") or "")[:255]))
    return out


def post_journal(*, tenant, entry_date, description, lines, source_type, source_id="", source_key=None,
                 branch=None, user=None, allow_before_cutover=False, reverses=None, reversal_reason="", audit_it=True):
    """
    Post a balanced journal entry atomically. Returns (entry, created).

    Idempotent: if `source_key` was already posted for this tenant the existing entry is
    returned with created=False and nothing is written. Use a key that identifies the
    business event, e.g. f"sale:{order.pk}" or f"expense-payment:{payment.pk}".
    """
    if source_key:
        existing = JournalEntry.all_objects.filter(tenant=tenant, source_key=source_key).first()
        if existing:
            return existing, False

    if isinstance(entry_date, datetime.datetime):
        entry_date = entry_date.date()
    norm = _normalise_lines(tenant, lines, branch)
    if len(norm) < 2:
        raise AccountingError("A journal entry needs at least two lines.")
    total_dr = sum((l["debit"] for l in norm), ZERO)
    total_cr = sum((l["credit"] for l in norm), ZERO)
    if total_dr != total_cr:
        raise AccountingError(f"Entry does not balance: debits {total_dr} and credits {total_cr}.")
    if total_dr <= 0:
        raise AccountingError("A journal entry must have a non-zero amount.")

    cfg = get_settings(tenant)
    if not allow_before_cutover and cfg.opening_balance_date and entry_date < cfg.opening_balance_date:
        raise AccountingError(f"The ledger starts on {cfg.opening_balance_date:%d %b %Y}; "
                              f"earlier dates cannot be posted.")

    try:
        with transaction.atomic():
            period = period_for(tenant, entry_date)
            if period.status == FiscalPeriod.Status.CLOSED:
                raise AccountingError(f"{period.name} is closed. Post in an open period or reopen it.")
            entry = JournalEntry.all_objects.create(
                tenant=tenant, reference=next_number(tenant, "journal", "JE-"), entry_date=entry_date,
                description=description[:255], branch=branch, period=period, status=JournalEntry.Status.DRAFT,
                source_type=source_type, source_id=str(source_id or ""), source_key=source_key or None,
                reverses=reverses, reversal_reason=reversal_reason,
                created_by=user if getattr(user, "pk", None) else None)
            # lines are written while the entry is still a DRAFT, then the entry is sealed
            JournalLine.all_objects.bulk_create([
                JournalLine(tenant=tenant, entry=entry, account=l["account"], branch=l["branch"],
                            debit=l["debit"], credit=l["credit"], memo=l["memo"]) for l in norm])
            entry.status = JournalEntry.Status.POSTED
            entry.posted_at = datetime.datetime.now(datetime.timezone.utc)
            entry.save()
            if audit_it:
                audit(tenant, user, "JOURNAL_POSTED", entry, reference=entry.reference, source_type=source_type,
                      source_id=str(source_id or ""), amount=str(total_dr))
            return entry, True
    except IntegrityError:
        # Lost a race against a concurrent post of the same source_key: return the winner.
        if source_key:
            winner = JournalEntry.all_objects.filter(tenant=tenant, source_key=source_key).first()
            if winner:
                return winner, False
        raise


def reverse_entry(entry, *, user, reason, entry_date=None):
    """Post the mirror image of `entry` and mark the original REVERSED. Returns the reversal."""
    if not reason or not reason.strip():
        raise AccountingError("A reason is required to reverse an entry.")
    with transaction.atomic():
        entry = JournalEntry.all_objects.select_for_update().get(pk=entry.pk)
        if entry.status == JournalEntry.Status.REVERSED:
            raise AccountingError("This entry has already been reversed.")
        if entry.status != JournalEntry.Status.POSTED:
            raise AccountingError("Only posted entries can be reversed.")
        if entry.reverses_id:
            raise AccountingError("A reversal entry cannot itself be reversed. Post a new entry instead.")
        mirrored = [dict(account=l.account, debit=l.credit, credit=l.debit, branch=l.branch,
                         memo=f"Reversal: {l.memo}".strip(": ")) for l in entry.get_lines()]
        reversal, created = post_journal(
            tenant=entry.tenant, entry_date=entry_date or entry.entry_date,
            description=f"Reversal of {entry.reference}: {reason.strip()}"[:255], lines=mirrored,
            source_type="REVERSAL", source_id=entry.pk, source_key=f"reversal:{entry.pk}",
            branch=entry.branch, user=user, allow_before_cutover=True,
            reverses=entry, reversal_reason=reason.strip()[:255])
        if created:
            entry.status = JournalEntry.Status.REVERSED   # the one edit a posted entry allows
            entry.save()
            audit(entry.tenant, user, "JOURNAL_REVERSED", entry, reference=entry.reference,
                  reversal=reversal.reference, reason=reason.strip())
        return reversal


# --------------------------------------------------------------------------
# opening balances
# --------------------------------------------------------------------------
def set_opening_balances(*, tenant, as_of, balances, user=None, branch=None):
    """
    Start the ledger on `as_of` (decision: existing tenants begin from an opening-balance date,
    history is not back-filled). `balances` maps system_key -> amount in that account's natural
    direction, e.g. {"CASH": "50000", "INVENTORY": "120000", "AP": "30000"}.
    The balancing figure goes to Opening Balance Equity.
    """
    cfg = get_settings(tenant)
    if cfg.opening_balance_date:
        raise AccountingError("Opening balances are already set for this business.")
    seed_chart_of_accounts(tenant)
    lines = []
    for key, amount in balances.items():
        acct = get_account(tenant, key)
        amt = money(amount)
        if amt <= 0:
            continue
        natural_debit = acct.normal_side == "DEBIT"
        lines.append(dict(account=acct, debit=amt if natural_debit else ZERO, credit=ZERO if natural_debit else amt,
                          memo="Opening balance", branch=branch))
    dr = sum((l["debit"] for l in lines), ZERO)
    cr = sum((l["credit"] for l in lines), ZERO)
    plug = get_account(tenant, "OPENING_EQUITY")
    if dr > cr:
        lines.append(dict(account=plug, credit=dr - cr, memo="Opening balance equity", branch=branch))
    elif cr > dr:
        lines.append(dict(account=plug, debit=cr - dr, memo="Opening balance equity", branch=branch))
    with transaction.atomic():
        entry = None
        if lines:
            entry, _ = post_journal(tenant=tenant, entry_date=as_of, description="Opening balances",
                                    lines=lines, source_type="OPENING", source_key="opening-balances",
                                    user=user, allow_before_cutover=True, branch=branch)
        cfg.opening_balance_date = as_of
        cfg.save(update_fields=["opening_balance_date", "updated_at"])
        audit(tenant, user, "OPENING_BALANCES_SET", entry, as_of=as_of.isoformat())
    return entry


# --------------------------------------------------------------------------
# reading the ledger
# --------------------------------------------------------------------------
def trial_balance(tenant, *, start=None, end=None, branch=None):
    """
    Rows of (account, debit_total, credit_total, balance) over [start, end], summed from journal lines.
    `balance` is positive in the account's normal direction. Returns (rows, total_debit, total_credit).
    Reversed entries are included on purpose: the original and its reversal cancel out.
    """
    qs = JournalLine.all_objects.filter(tenant=tenant, entry__status__in=["POSTED", "REVERSED"])
    if start:
        qs = qs.filter(entry__entry_date__gte=start)
    if end:
        qs = qs.filter(entry__entry_date__lte=end)
    if branch is not None:
        qs = qs.filter(branch=branch)
    sums = {r["account"]: r for r in qs.values("account").annotate(d=Sum("debit"), c=Sum("credit"))}
    rows, td, tc = [], ZERO, ZERO
    for acct in Account.all_objects.filter(tenant=tenant, is_header=False).order_by("code"):
        r = sums.get(acct.pk)
        if not r:
            continue
        d, c = r["d"] or ZERO, r["c"] or ZERO
        bal = d - c if acct.normal_side == "DEBIT" else c - d
        rows.append(dict(account=acct, debit=d, credit=c, balance=bal))
        td += d
        tc += c
    return rows, td, tc
