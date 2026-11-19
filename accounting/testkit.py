"""Shared fixtures for the Financial Management tests (not a test module itself)."""
import datetime
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum

from inventory.models import Branch
from sales.tests import make_shop
from tenants.services import create_staff_user

from .models import Account, JournalLine
from .services import get_settings, seed_chart_of_accounts, set_opening_balances

D = Decimal
PW = "S3cure-pass-99"
TODAY = datetime.date.today()


def ago(days):
    return TODAY - timedelta(days=days)


def finance_shop(open_days_ago=40, cash="10000", inventory="200", extra_branch=True, **shop_over):
    """A shop with books opened `open_days_ago` days back, cash and stock on the shelves, and one of each role."""
    shop = make_shop(**shop_over)
    t, owner, main = shop["tenant"], shop["owner"], shop["branch"]
    seed_chart_of_accounts(t)
    set_opening_balances(tenant=t, as_of=ago(open_days_ago), user=owner, branch=main,
                         balances={"CASH": cash, "INVENTORY": inventory})
    shop["second"] = Branch.all_objects.create(tenant=t, name="Westlands") if extra_branch else None
    mk = lambda handle, role, branch=None: create_staff_user(tenant=t, handle=handle, role=role, password=PW, branch=branch)
    shop["accountant"] = mk("wanjiru", "ACCOUNTANT")
    shop["manager"] = mk("peter", "MANAGER", main)
    shop["cashier"] = mk("amina", "CASHIER", main)
    return shop


def other_shop():
    return finance_shop(business_name="Kamau Fresh Mart", email="kamau@example.com", phone="254722000111",
                        owner_name="Kamau K")


def balance(tenant, key, branch="ALL", end=None):
    acct = Account.all_objects.get(tenant=tenant, system_key=key)
    qs = JournalLine.all_objects.filter(tenant=tenant, account=acct, entry__status__in=["POSTED", "REVERSED"])
    if branch is None:                       # head office / in transit: lines with no branch
        qs = qs.filter(branch__isnull=True)
    elif branch != "ALL":
        qs = qs.filter(branch=branch)
    if end:
        qs = qs.filter(entry__entry_date__lte=end)
    a = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    d, c = a["d"] or D("0"), a["c"] or D("0")
    return d - c if acct.normal_side == "DEBIT" else c - d


def category(tenant, name):
    from .models import ExpenseCategory
    return ExpenseCategory.all_objects.get(tenant=tenant, name=name)
