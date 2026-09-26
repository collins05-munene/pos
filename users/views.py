from django.shortcuts import render
from django.views import View
from django.views.generic import FormView, ListView, TemplateView, DetailView
from django.urls import reverse_lazy
from django.contrib.auth import login, logout, authenticate
from django.shortcuts import redirect, render, get_object_or_404
from django.contrib.auth.mixins import UserPassesTestMixin
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit

from .models import ActivityLog, User
from .forms import StandardLoginForm, CashierPinLoginForm
from .utils import log_action, AuditAction
from .dashboard import build_dashboard_context
from users.mixins import AdminRequiredMixin, CashierRequiredMixin


# Create your views here.
def get_post_username(group, request):
    return request.POST.get('username', '').lower().strip()

def get_login_identifier(group, request):
    return request.POST.get('username', '').lower().strip()

@method_decorator(
    ratelimit(key='ip', rate='10/m', method='POST', block=False), 
    name='post'
)
@method_decorator(
    ratelimit(key=get_login_identifier, rate='5/m', method='POST', block=False), 
    name='post'
)
class StandardLoginView(FormView):
    template_name = 'users/login.html'
    form_class = StandardLoginForm
    success_url = reverse_lazy('admin-dashboard')

    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            form = self.get_form()
            form.add_error(
                None, 
                "Too many login attempts. Please wait a minute before trying again."
            )
            return self.form_invalid(form)
            
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        user = form.get_user()
        if user.role in ['ADMIN', 'MANAGER']:
            login(self.request, user)
            log_action(user, "Standard Login", "Logged into management dashboard", self.request)
            return super().form_valid(form)
        else:
            form.add_error(None, "Cashiers must use the POS Login terminal.")
            return self.form_invalid(form)


@method_decorator(
    ratelimit(key='ip', rate='10/m', method='POST', block=False), 
    name='post'
)
@method_decorator(
    ratelimit(key=get_post_username, rate='5/m', method='POST', block=False), 
    name='post'
)
class CashierPINLoginView(FormView):
    template_name = 'users/pin_login.html'
    form_class = CashierPinLoginForm
    success_url = reverse_lazy('pos_terminal')

    def post(self, request, *args, **kwargs):
        if getattr(request, 'limited', False):
            form = self.get_form()
            form.add_error(
                None, 
                "Too many login attempts. Please wait a minute before trying again."
            )
            return self.form_invalid(form)
            
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        username = form.cleaned_data.get('username')
        pin = form.cleaned_data.get('pin')

        user = authenticate(self.request, username=username, pin=pin)

        if user is not None:
            login(self.request, user)
            log_action(user, AuditAction.LOGIN_PIN, f"Logged in to POS terminal via pin.", self.request)
            return super().form_valid(form)
        else:
            log_action(None, "PIN_LOGIN_FAILED", f"Failed PIN login attempt for username: {username}")
            form.add_error(None, "Invalid Username or PIN")
            return self.form_invalid(form)

class LogoutView(View):
    def get(self, request):
        if request.user.is_authenticated:
            log_action(request.user, 'Logout', 'Logged out of session', request)
            logout(request)
        return redirect('pin_login')
    

class ActivityLogListView(AdminRequiredMixin, ListView):
    model = ActivityLog
    template_name = 'users/activity_logs.html'
    context_object_name = 'logs'
    paginate_by = 10


class ActivityLogDetailView(AdminRequiredMixin, DetailView):
    model = ActivityLog
    template_name = 'users/audit_detail_page.html'
    context_object_name = 'log'

class AdminDashboardView(AdminRequiredMixin, TemplateView):
    template_name = 'users/admin-dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(build_dashboard_context())
        return context


class CashierDashboardView(CashierRequiredMixin, ListView):
    model = User
    template_name = 'users/cashier_dashboard.html'

class HomepageView(TemplateView):
    template_name = 'users/pin_login.html'