"""
The "current tenant" for this request/thread, stored in a ContextVar.

Set by TenantMiddleware for web requests; set explicitly with
`tenant_context(tenant)` in management commands, cron jobs and tests.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_current_tenant: ContextVar = ContextVar("current_tenant", default=None)


def get_current_tenant():
    return _current_tenant.get()


def set_current_tenant(tenant):
    """Returns a token; pass it to reset_current_tenant() when done."""
    return _current_tenant.set(tenant)


def reset_current_tenant(token):
    _current_tenant.reset(token)


@contextmanager
def tenant_context(tenant):
    token = _current_tenant.set(tenant)
    try:
        yield tenant
    finally:
        _current_tenant.reset(token)
