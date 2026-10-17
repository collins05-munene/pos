from django.contrib import messages
from django.db import transaction
from django.db.models import ProtectedError
from django.shortcuts import redirect


class SafeDeleteMixin:
    """Hard-delete if nothing references the object; otherwise archive it."""

    def form_valid(self, form):          
        obj = self.object
        try:
            with transaction.atomic():
                obj.delete()
        except ProtectedError:
            self.archive(obj)
            messages.warning(
                self.request,
                f"'{obj}' has been deleted successfully but some related objects still reference it. It has been archived instead of being permanently deleted."
            )
        else:
            messages.success(self.request, f"'{obj}' was deleted.")
        return redirect(self.get_success_url())

    def archive(self, obj):
        obj.is_active = False
        obj.save(update_fields=['is_active'])


class ArchiveFilterMixin:
    """Adds ?show=archived support to a ListView."""

    def get_queryset(self):
        qs = super().get_queryset()   
        archived = self.request.GET.get('show') == 'archived'
        return qs.filter(is_active=not archived)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['show_archived'] = self.request.GET.get('show') == 'archived'
        return ctx