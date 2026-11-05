from django.conf import settings
from django.contrib.auth import login, logout
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.generic import DetailView, FormView, ListView, RedirectView, TemplateView
from django_ratelimit.decorators import ratelimit

from . import password_reset, turnstile
from .dashboard import build_dashboard_context
from .dashboard_live import get_version
from .forms import LoginForm, PasswordResetRequestForm, SetNewPasswordForm
from .models import ActivityLog, User
from .utils import AuditAction, log_action
from notifications.models import Notification
from notifications.utils import MANAGEMENT, notify, who
from tenants.services import SIGNUP_EMAIL_KEY
from users.mixins import AdminRequiredMixin, CashierRequiredMixin, TenantScopedQuerysetMixin

# Create your views here.
def get_login_identifier(group, request):
    """Rate-limit key: one counter per shop+username, so one cashier can't lock out the whole shop."""
    code = request.POST.get('shop_code', '').lower().strip()
    return f"{code}__{request.POST.get('username', '').lower().strip()}"


def home_for(user):
    """Where a freshly signed-in user lands, by who they are."""
    if user.is_superuser:
        return reverse('platform_admin:overview')
    if user.role == User.Roles.CASHIER:
        return reverse('pos_terminal')
    return reverse('admin-dashboard')


def _client_ip(request):
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
            or request.META.get('REMOTE_ADDR'))


@method_decorator(
    ratelimit(key='ip', rate='10/m', method='POST', block=False),
    name='post'
)
@method_decorator(
    ratelimit(key=get_login_identifier, rate='5/m', method='POST', block=False),
    name='post'
)
class LoginView(FormView):
    """One login for everyone: owner, admin, manager, cashier (and the platform admin)."""
    template_name = 'users/login.html'
    form_class = LoginForm

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and request.method == 'GET':
            return redirect(home_for(request.user))
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        # Arriving from the "email confirmed" page: save them typing their own email.
        if self.request.GET.get('verified') == '1':
            email = self.request.session.pop(SIGNUP_EMAIL_KEY, None)
            if email:
                initial['username'] = email
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['next'] = self.request.POST.get('next') or self.request.GET.get('next', '')
        ctx['verified'] = self.request.GET.get('verified') == '1'
        ctx['password_reset_done'] = self.request.GET.get('reset') == '1'
        return ctx

    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            form = self.get_form()
            form.add_error(
                None,
                "Too many login attempts. Please wait a minute before trying again."
            )
            return self.form_invalid(form)

        # Bot check comes before the password is even looked at.
        if not turnstile.verify(request.POST.get(turnstile.FIELD), _client_ip(request)):
            self._bot_check_failed = True
            form = self.get_form()
            form.add_error(None, turnstile.ERROR_MESSAGE)
            return self.form_invalid(form)

        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        login(self.request, user)
        log_action(user, AuditAction.LOGIN, "Signed in with username and password.", self.request)

        target = self.request.POST.get('next', '')
        if not (target and url_has_allowed_host_and_scheme(
                target, allowed_hosts={self.request.get_host()},
                require_https=self.request.is_secure())):
            target = home_for(user)
        return redirect(target)

    def form_invalid(self, form):
        username = form.cleaned_data.get('username') if hasattr(form, 'cleaned_data') else None
        if getattr(self, '_bot_check_failed', False):
            username = None   # no password was tried, so don't log it or count it as a failed sign-in
        failed_user = User.objects.select_related('tenant').filter(username=username).first() if username else None
        if failed_user is not None and failed_user.tenant_id:
            tenant = failed_user.tenant
            log_action(None, AuditAction.LOGIN_FAILED,
                       f"Failed sign-in for username: {username}",
                       request=self.request, tenant=tenant)
            # Tell the shop's admins when someone seems to be guessing one account's password.
            key = f"loginfail:{tenant.pk}:{username}"
            cache.add(key, 0, timeout=15 * 60)
            try:
                fails = cache.incr(key)
            except ValueError:
                fails = 1
            if fails == 5:   # fire once when the threshold is crossed
                notify(tenant, "Repeated failed sign-ins",
                       f"5 failed sign-in attempts for '{who(failed_user)}' in the last 15 minutes. "
                       "Someone may be guessing a password.",
                       level=Notification.Level.WARNING, roles=MANAGEMENT)
        return super().form_invalid(form)


class LegacyPosLoginRedirect(RedirectView):
    """/users/pos-login/ was the cashier PIN screen. Old bookmarks, installed PWAs and the offline
    pages still point at it; everyone now signs in at /users/login/."""
    pattern_name = 'login'
    query_string = True
    permanent = False


class HomepageView(View):
    """'/' sends signed-in people to their home and everyone else to the login page."""
    def get(self, request):
        if request.user.is_authenticated:
            return redirect(home_for(request.user))
        return redirect('login')


@method_decorator(
    ratelimit(key='ip', rate='10/h', method='POST', block=False),
    name='post'
)
@method_decorator(
    ratelimit(key=get_login_identifier, rate='3/h', method='POST', block=False),
    name='post'
)
class PasswordResetRequestView(FormView):
    """'Forgot password?' -- files a request that the platform team must approve."""
    template_name = 'users/password_reset_request.html'
    form_class = PasswordResetRequestForm

    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            form = self.get_form()
            form.add_error(None, "Too many requests. Please try again later.")
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        password_reset.request_reset(
            username=form.cleaned_data['username'], ip=_client_ip(self.request), request=self.request)
        # Same answer whether or not the account exists, so this page can't be used to find usernames.
        return redirect('password-reset-sent')


class PasswordResetSentView(TemplateView):
    template_name = 'users/password_reset_sent.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['hours'] = getattr(settings, 'PASSWORD_RESET_LINK_HOURS', 24)
        return ctx


@method_decorator(
    ratelimit(key='ip', rate='20/h', method='POST', block=True),
    name='post'
)
class PasswordResetConfirmView(View):
    """The emailed link: choose a new password. Works once."""
    template_name = 'users/password_reset_confirm.html'

    def _lookup(self, token):
        try:
            return password_reset.get_request_for_token(token), None
        except password_reset.ResetLinkExpired:
            return None, 'expired'
        except password_reset.ResetLinkInvalid:
            return None, 'invalid'

    def get(self, request, token):
        req, problem = self._lookup(token)
        if problem:
            return render(request, self.template_name, {'problem': problem},
                          status=410 if problem == 'expired' else 404)
        return render(request, self.template_name, {'form': SetNewPasswordForm(user=req.user)})

    def post(self, request, token):
        req, problem = self._lookup(token)
        if problem:
            return render(request, self.template_name, {'problem': problem},
                          status=410 if problem == 'expired' else 404)
        form = SetNewPasswordForm(request.POST, user=req.user)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form}, status=400)
        try:
            password_reset.complete_reset(token, form.cleaned_data['password1'], request=request)
        except password_reset.ResetLinkExpired:
            return render(request, self.template_name, {'problem': 'expired'}, status=410)
        except password_reset.ResetLinkInvalid:
            return render(request, self.template_name, {'problem': 'invalid'}, status=404)
        except ValidationError as exc:
            form.add_error('password1', exc)
            return render(request, self.template_name, {'form': form}, status=400)
        return redirect(f"{reverse('login')}?reset=1")


class LogoutView(View):
    def get(self, request):
        if request.user.is_authenticated:
            User.objects.filter(pk=request.user.pk).update(last_seen=None)
            log_action(request.user, 'Logout', 'Logged out of session', request)
            logout(request)
        return redirect('login')
    

class ActivityLogListView(AdminRequiredMixin, TenantScopedQuerysetMixin, ListView):
    model = ActivityLog
    template_name = 'users/activity_logs.html'
    context_object_name = 'logs'
    paginate_by = 10


class ActivityLogDetailView(AdminRequiredMixin, TenantScopedQuerysetMixin, DetailView):
    model = ActivityLog
    template_name = 'users/audit_detail_page.html'
    context_object_name = 'log'

class AdminDashboardView(AdminRequiredMixin, TemplateView):
    template_name = 'users/admin-dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenant = self.request.tenant
        if tenant is None:
            raise PermissionDenied("No active business selected.")
        version = get_version(tenant.pk)             # read BEFORE building the data
        context.update(build_dashboard_context(tenant))
        context["dash_version"] = version
        return context


@method_decorator(never_cache, name='dispatch')
class DashboardVersionView(AdminRequiredMixin, View):
    def get(self, request):
        if request.tenant is None:
            raise PermissionDenied("No active business selected.")
        return JsonResponse({"v": get_version(request.tenant.pk)})


@method_decorator(never_cache, name='dispatch')
class DashboardBodyView(AdminRequiredMixin, TemplateView):
    template_name = 'users/_dashboard_body.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tenant = self.request.tenant
        if tenant is None:
            raise PermissionDenied("No active business selected.")
        context["dash_version"] = get_version(tenant.pk)   # read first, then build
        context.update(build_dashboard_context(tenant))
        return context

    def render_to_response(self, context, **kwargs):
        resp = super().render_to_response(context, **kwargs)
        resp["X-Dash-Version"] = str(context["dash_version"])
        return resp


class CashierDashboardView(CashierRequiredMixin, ListView):
    model = User
    template_name = 'users/cashier_dashboard.html'

    def get_queryset(self):
        return User.tenant_objects.filter(role=User.Roles.CASHIER, is_active=True)

