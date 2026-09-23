from django.shortcuts import render
from django.views import View
from django.views.generic import FormView, ListView, TemplateView
from django.urls import reverse_lazy
from django.contrib.auth import login, logout, authenticate
from django.shortcuts import redirect, render, get_object_or_404
from django.contrib.auth.mixins import UserPassesTestMixin, LoginRequiredMixin

from .models import ActivityLog, User
from .forms import StandardLoginForm, CashierPinLoginForm
from .utils import log_action, AuditAction
from .dashboard import build_dashboard_context

# Create your views here.
def activity_log_detail(request, pk):
    log = get_object_or_404(ActivityLog, pk=pk)
    return render(request, 'users/audit_detail_page.html', {'log': log})

class StandardLoginView(FormView):
    template_name = 'users/login.html'
    form_class = StandardLoginForm
    success_url = reverse_lazy('admin-dashboard')

    def form_valid(self, form):
        user = form.get_user()
        if user.role in ['ADMIN', 'MANAGER']:
            login(self.request, user)
            log_action(user, "Standard Login", "Logged into management dashboard", self.request)
            return super().form_valid(form)
        else:
            form.add_error(None, "Cashiers must use the POS Login terminal.")
            return self.form_invalid(form)

class CashierPINLoginView(FormView):
    template_name = 'users/pin_login.html'
    form_class = CashierPinLoginForm
    success_url = reverse_lazy('pos_terminal')

    def form_valid(self, form):
        username = form.cleaned_data.get('username')
        pin = form.cleaned_data.get('pin')

        user = authenticate(self.request,username=username, pin=pin )

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
    

class AdminRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_admin
    

class ManagerRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_authenticated and self.request.user.is_manager
    

class ActivityLogListView(AdminRequiredMixin, ListView):
    model = ActivityLog
    template_name = 'users/activity_logs.html'
    context_object_name = 'logs'
    paginate_by = 50


class AdminDashboardView(AdminRequiredMixin, TemplateView):
    template_name = 'users/admin-dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(build_dashboard_context())
        return context


class CashierDashboardView(ListView):
    model = User
    template_name = 'users/cashier_dashboard.html'