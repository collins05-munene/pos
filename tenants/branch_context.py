"""
The branch the current request is working at, in a ContextVar next to the current tenant.

Cashiers are pinned to user.branch. Owner/admin/manager start at user.branch (or the main
branch) and may switch; the choice lives in the session under ACTIVE_BRANCH_KEY.
"""
from contextlib import contextmanager
from contextvars import ContextVar

ACTIVE_BRANCH_KEY = "active_branch_id"

_current_branch: ContextVar = ContextVar("current_branch", default=None)


def get_current_branch():
    return _current_branch.get()


def set_current_branch(branch):
    return _current_branch.set(branch)


def reset_current_branch(token):
    _current_branch.reset(token)


@contextmanager
def branch_context(branch):
    token = _current_branch.set(branch)
    try:
        yield branch
    finally:
        _current_branch.reset(token)
