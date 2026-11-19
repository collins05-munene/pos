"""Which branches a user may see or post for. None means every branch of their business."""
from .permissions import FINANCE_ROLES


def all_branch_access(user):
    return bool(user.is_authenticated and not user.is_superuser and getattr(user, "tenant_id", None)
                and (user.is_owner or user.role in FINANCE_ROLES))


def allowed_branch_ids(user):
    """None = all branches. A set (possibly empty) = only those branches."""
    if all_branch_access(user):
        return None
    if user.is_authenticated and getattr(user, "role", None) == "MANAGER" and getattr(user, "branch_id", None):
        return {user.branch_id}
    return set()


def branch_allowed(user, branch):
    """Head office (branch=None) is only for all-access users."""
    ids = allowed_branch_ids(user)
    if ids is None:
        return True
    return branch is not None and branch.pk in ids
