from django.views.generic import CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.utils.cache import patch_vary_headers
from django.http import JsonResponse
from django.urls import reverse

from .utils import log_action, AuditAction


class AuditLogMixin:
    """
    Automatically creates audit logs for CreateView, UpdateView
    and DeleteView.

    """

    def get_audit_label(self, obj):
        """
        Returns a human-readable description of the object.
        Example:
            Category: John Doe
            Product: Paracetamol
        """
        model_name = self.model._meta.verbose_name.title()
        return f"{model_name}: {obj}"

    def get_audit_action(self):
        """
        Determines the audit action based on the view type.
        """
        if isinstance(self, CreateView):
            return AuditAction.RECORD_CREATE

        if isinstance(self, UpdateView):
            return AuditAction.RECORD_UPDATE

        if isinstance(self, DeleteView):
            return AuditAction.RECORD_DELETE

        return None

    def form_valid(self, form):
        """
        Handles CreateView and UpdateView.
        """
        response = super().form_valid(form)

        action = self.get_audit_action()

        if action:
            log_action(
                user=self.request.user,
                action=action,
                details=self.get_audit_label(self.object),
                request=self.request
            )

        return response

    def delete(self, request, *args, **kwargs):
        """
        Handles DeleteView.
        """
        self.object = self.get_object()

        label = self.get_audit_label(self.object)

        response = super().delete(request, *args, **kwargs)

        log_action(
            user=request.user,
            action=AuditAction.RECORD_DELETE,
            details=f"{label} (id={self.object.pk})",
            request=request
        )

        return response


MANAGEMENT_ROLES = ("ADMIN", "MANAGER")


class AdminRequiredMixin(AccessMixin):
    """
    CBV mixin that verifies the current user is authenticated AND is a
    management user (ADMIN or MANAGER), or a superuser.
    """
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        user = request.user
        if getattr(user, "role", None) in MANAGEMENT_ROLES or user.is_superuser:
            return super().dispatch(request, *args, **kwargs)

        raise PermissionDenied("You do not have administrative permission to access this page.")


class CashierRequiredMixin(AccessMixin):
    """
    CBV mixin that verifies the current user is authenticated AND is a
    CASHIER, ADMIN or MANAGER, or a superuser.
    """
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        user = request.user
        if getattr(user, "role", None) in ("CASHIER",) + MANAGEMENT_ROLES or user.is_superuser:
            return super().dispatch(request, *args, **kwargs)

        raise PermissionDenied("You do not have cashier permissions to access this page.")

class TenantScopedQuerysetMixin:
    """
    Explicitly restricts a view's queryset to the request's tenant.
    Does not depend on the model's default manager. If there is no tenant
    (e.g. a superuser outside a support session), it returns nothing.
    """
    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        base = self.model._base_manager      
        if tenant is None:
            return base.none()
        return base.filter(tenant_id=tenant.pk)


def action_link(label, url, cls='edit-link'):
    """A normal link in the Actions column."""
    return {'label': label, 'url': url, 'cls': cls}


def action_post(label, url, cls='edit-link'):
    """A button that POSTs (with CSRF) - used for Restore."""
    return {'label': label, 'url': url, 'cls': cls, 'post': True}


class LiveSearchMixin:
    """
    Turns a ListView into a JSON endpoint for live-search.js.

    - Normal page request  -> renders the page shell only (no query is run).
    - Request from the JS   -> returns {rows, page, num_pages, count, empty}.
    - ?q=... filters the queryset; every word must match at least one of `search_fields`.

    Each view implements serialize_row(obj) -> {'cells': [...], 'actions': [...]}.
    A cell is a string, or a dict: {'t': text, 'c': colour, 'b': bold, 's': small second line, 'code': True}.
    Cells must be in the same order as the <th> headers in the template.
    Put this mixin FIRST in the view's base classes.
    """
    search_fields = ()
    search_distinct = False       # True if a search field crosses a reverse/m2m relation
    empty_noun = 'items'          # "No <noun> registered yet"
    create_url_name = None        # url name for the "Add one now" button in the empty state
    create_label = 'Add one now'

    # ---- request helpers
    def is_live_request(self):
        return self.request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    @property
    def archived(self):
        return self.request.GET.get('show') == 'archived'

    @property
    def has_filters(self):
        return any(v.strip() for k, v in self.request.GET.items() if k not in ('page', 'show'))

    # ---- searching
    def get_search_query(self):
        return " ".join(self.request.GET.get('q', '').split())[:100]

    def get_queryset(self):
        qs = super().get_queryset()
        query = self.get_search_query()
        if not query or not self.search_fields:
            return qs
        condition = Q()
        for word in query.split():
            word_q = Q()
            for field in self.search_fields:
                word_q |= Q(**{f"{field}__icontains": word})
            condition &= word_q
        qs = qs.filter(condition)
        return qs.distinct() if self.search_distinct else qs

    # ---- responses
    def get(self, request, *args, **kwargs):
        if self.is_live_request():
            return super().get(request, *args, **kwargs)
        # Plain page visit: the rows are fetched by JS, so don't query them here.
        self.object_list = self.get_queryset().none()
        return self.render_to_response(self.get_context_data())

    def serialize_row(self, obj):
        raise NotImplementedError

    def get_empty_state(self):
        noun = self.empty_noun
        if self.has_filters:
            return {'title': f'No {noun} match your search', 'hint': 'Try different words or clear the filters.'}
        if self.archived:
            return {'title': f'No deleted {noun}'}
        state = {'title': f'No {noun} registered yet', 'hint': 'Get started by adding your first one.'}
        if self.create_url_name:
            state['action'] = {'label': self.create_label, 'url': reverse(self.create_url_name)}
        return state

    def build_payload(self, context):
        page, paginator = context.get('page_obj'), context.get('paginator')
        rows = [self.serialize_row(o) for o in context['object_list']]
        return {
            'rows': rows,
            'page': page.number if page else 1,
            'num_pages': paginator.num_pages if paginator else 1,
            'count': paginator.count if paginator else len(rows),
            'empty': self.get_empty_state(),
        }

    def render_to_response(self, context, **kwargs):
        if self.is_live_request():
            response = JsonResponse(self.build_payload(context))
        else:
            response = super().render_to_response(context, **kwargs)
        # Same URL returns HTML or JSON, so caches / the back button must tell them apart.
        patch_vary_headers(response, ['X-Requested-With'])
        return response