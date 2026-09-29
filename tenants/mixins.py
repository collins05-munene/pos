from django.contrib.auth.mixins import UserPassesTestMixin


class OwnerRequiredMixin(UserPassesTestMixin):
    """The shop's billing/staff owner -- the only staff member who can manage other staff
    or see billing. Not the same as role=ADMIN: is_owner is set once at sign-up."""

    def test_func(self):
        u = self.request.user
        return u.is_authenticated and getattr(u, "is_owner", False) and u.tenant_id is not None