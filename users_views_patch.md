# users/views.py -- three edits

1. Rate-limit keys must include the business code, otherwise cashier "john" of shop A and
   "john" of shop B share one bucket:

```python
def get_post_username(group, request):
    code = request.POST.get('business_code', '').lower().strip()
    return f"{code}:{request.POST.get('username', '').lower().strip()}"

def get_login_identifier(group, request):
    return get_post_username(group, request)
```

2. Failed PIN attempts should show up in the right shop's audit log:

```python
from tenants.models import Tenant
...
        else:
            tenant = Tenant.objects.filter(
                slug=(form.cleaned_data.get("business_code") or "").lower()).first()
            log_action(None, "PIN_LOGIN_FAILED",
                       f"Failed PIN login attempt for username: {username}",
                       self.request, tenant=tenant)
            form.add_error(None, "Invalid business code, username or PIN")
            return self.form_invalid(form)
```

3. `LogoutView` should be POST-only (a GET logout can be triggered by any <img src>). Keep
   GET for now if your templates link to it; then switch when convenient.

`ActivityLogListView` / `ActivityLogDetailView` need no change: `ActivityLog.objects` is scoped,
so a shop admin sees only their own logs and cannot open another shop's log by guessing an id.
