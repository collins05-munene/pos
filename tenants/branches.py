"""
Branch rules in one place: who can be where, how many a shop may have, what adding one costs.

Views, signup and the Django admin all go through these functions so the rules can't drift.
"""
import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q

from .branch_context import ACTIVE_BRANCH_KEY, get_current_branch

CODE_RE = re.compile(r"^[A-Za-z0-9]{2,10}$")


def _Branch():
    from inventory.models import Branch
    return Branch


def tenant_branches(tenant, include_archived=True):
    qs = _Branch().all_objects.filter(tenant=tenant)
    if not include_archived:
        qs = qs.filter(Q(is_active=True) | Q(awaiting_payment=True))
    return qs.order_by("-is_main", "name")


def main_branch(tenant):
    Branch = _Branch()
    return (Branch.all_objects.filter(tenant=tenant, is_main=True).first()
            or Branch.all_objects.filter(tenant=tenant, is_active=True).order_by("pk").first())


def active_branches(tenant):
    return tenant_branches(tenant).filter(is_active=True)



def user_home_branch(user):
    """Assigned branch if it is still usable, else the shop's main branch."""
    b = getattr(user, "branch", None)
    if b is not None and b.is_active:
        return b
    return main_branch(user.tenant) if user.tenant_id else None


def resolve_active_branch(user, session=None):
    """The branch this user is working at right now (see module docstring)."""
    if user is None or not user.is_authenticated or not user.tenant_id:
        return None
    if user.can_switch_branch and session is not None:
        chosen = session.get(ACTIVE_BRANCH_KEY)
        if chosen:
            b = active_branches(user.tenant).filter(pk=chosen).first()
            if b:
                return b
    return user_home_branch(user)


def branch_for_user(user):
    """What sales/inventory code should use when it only has a user."""
    current = get_current_branch()
    if current is not None and user.can_switch_branch:
        return current
    return user_home_branch(user)


def switch_branch(request, branch_id):
    user = request.user
    if not user.can_switch_branch:
        raise ValidationError("Cashiers are fixed to their own branch.")
    b = active_branches(request.tenant).filter(pk=branch_id).first()
    if b is None:
        raise ValidationError("That branch isn't available.")
    request.session[ACTIVE_BRANCH_KEY] = b.pk
    return b


# ---------------------------------------------------------------- limits and cost
def branch_summary(tenant):
    from billing import conf
    from billing.services import billable_branches
    sub = getattr(tenant, "subscription", None)
    slots = sub.branch_slots if sub else 1
    qs = tenant_branches(tenant)
    return {
        "paid_slots": slots,
        "in_use": qs.filter(is_active=True).count(),
        "awaiting_payment": qs.filter(awaiting_payment=True).count(),
        "billable": billable_branches(tenant),
        "max": conf.get("MAX_BRANCHES"),
        "extra_branch_monthly": conf.get("BRANCH_MONTHLY_PRICE"),
        "currency": conf.get("CURRENCY"),
    }


# ---------------------------------------------------------------- create / edit
def _clean(name, code, location="", phone=""):
    name = (name or "").strip()
    if not name:
        raise ValidationError("Branch name is required.")
    if len(name) > 100:
        raise ValidationError("Branch name is too long.")
    code = (code or "").strip().upper()
    if code and not CODE_RE.match(code):
        raise ValidationError("Code: 2-10 letters or digits.")
    if phone:
        from billing.mpesa import normalize_phone
        try:
            phone = normalize_phone(phone)
        except ValueError as exc:
            raise ValidationError(str(exc))
    return name, code, (location or "").strip()[:255], phone


def create_branch(tenant, *, name, code="", location="", phone="", is_main=False,
                  active=None, allow_pending=True):
    """
    Add a branch.

    Within the shop's paid allowance it is active immediately. Beyond it, the branch is created
    as 'awaiting payment' (inactive) and the caller should issue the upgrade invoice -- the
    branch switches on when that invoice is paid. allow_pending=False raises instead.
    """
    from billing import conf

    Branch = _Branch()
    name, code, location, phone = _clean(name, code, location, phone)
    with transaction.atomic():
        existing = list(tenant_branches(tenant, include_archived=False))
        if len(existing) >= conf.get("MAX_BRANCHES"):
            raise ValidationError(f"A shop can have at most {conf.get('MAX_BRANCHES')} branches.")
        sub = getattr(tenant, "subscription", None)
        slots = sub.branch_slots if sub else 1
        in_use = sum(1 for b in existing if b.is_active)
        pending = active is None and in_use >= slots and not is_main
        if pending and not allow_pending:
            raise ValidationError("Your plan has no free branch slot.")
        try:
            return Branch.all_objects.create(
                tenant=tenant, name=name, code=code, location=location, phone=phone,
                is_main=is_main,
                is_active=(not pending) if active is None else active,
                awaiting_payment=pending,
            )
        except IntegrityError:
            raise ValidationError("You already have a branch with that name.")


def update_branch(branch, *, name, code="", location="", phone=""):
    name, code, location, phone = _clean(name, code, location, phone)
    branch.name, branch.code, branch.location, branch.phone = name, code, location, phone
    try:
        branch.save(update_fields=["name", "code", "location", "phone"])
    except IntegrityError:
        raise ValidationError("You already have a branch with that name.")
    return branch


def archive_branch(branch):
    """History (sales, stock, purchases) stays; staff move to the main branch."""
    if branch.is_main:
        raise ValidationError("The main branch can't be archived.")
    if not branch.is_active and not branch.awaiting_payment:
        return branch
    from sales.models import CashRegisterSession
    if CashRegisterSession.all_objects.filter(branch=branch, status="OPEN").exists():
        raise ValidationError("Close this branch's cash register before archiving it.")
    main = main_branch(branch.tenant)
    with transaction.atomic():
        branch.is_active = False
        branch.awaiting_payment = False
        branch.save(update_fields=["is_active", "awaiting_payment"])
        branch.staff.update(branch=main)
    return branch


def restore_branch(branch):
    """Bring an archived branch back; if the plan has no free slot it waits for payment."""
    if branch.is_active or branch.awaiting_payment:
        return branch
    sub = getattr(branch.tenant, "subscription", None)
    slots = sub.branch_slots if sub else 1
    in_use = active_branches(branch.tenant).count()
    if in_use < slots:
        branch.is_active = True
    else:
        branch.awaiting_payment = True
    branch.save(update_fields=["is_active", "awaiting_payment"])
    return branch


def assign_user_branch(user, branch):
    """Cashiers need a branch; managers/admins may have none (they start at main)."""
    if branch is not None and (branch.tenant_id != user.tenant_id or not branch.is_active):
        raise ValidationError("Pick one of your active branches.")
    if branch is None and user.role == user.Roles.CASHIER:
        raise ValidationError("A cashier must be assigned to a branch.")
    user.branch = branch
    user.save(update_fields=["branch"])
    return user
