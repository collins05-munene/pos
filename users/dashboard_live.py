import time
from django.core.cache import cache
from django.db import transaction


def _key(tenant_id):
    return f"dash:ver:{tenant_id}"


def get_version(tenant_id):
    v = cache.get(_key(tenant_id))
    if v is None:
        cache.add(_key(tenant_id), time.time_ns(), timeout=None)
        v = cache.get(_key(tenant_id))
    return v


def bump(tenant_id):
    cache.set(_key(tenant_id), time.time_ns(), timeout=None)
    # IMPORTANT: also clear the snapshot cache used inside build_dashboard_context
    # (see "One thing I need from you" below)


def _already_queued(tid):
    # Django keeps pending on_commit callbacks on the connection. Rolled-back
    # savepoints remove their entries, so this can't get stuck the way a flag would.
    for entry in transaction.get_connection().run_on_commit:
        if getattr(entry[1], "_dash_tid", None) == tid:
            return True
    return False


def bump_on_commit(sender, instance, **kwargs):
    tid = getattr(instance, "tenant_id", None)
    if not tid or _already_queued(tid):
        return

    def _bump():
        bump(tid)

    _bump._dash_tid = tid
    transaction.on_commit(_bump)