from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Q, OuterRef, Subquery
from django.core.paginator import Paginator
from django.views.generic import TemplateView, DetailView
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.db.models import Sum, F, ExpressionWrapper, DecimalField
from django.contrib import messages
from django.views import View
import logging

from products.models import ProductVariant, Category
from .cart import POSCart
from .models import Order, OrderItem, CashRegisterSession, CashTransaction
from .exceptions import InsufficientStockError
from .utils import resolve_user_branch
from users.mixins import AdminRequiredMixin, CashierRequiredMixin
from inventory.models import Branch


logger = logging.getLogger(__name__)

CASH_OUT_REASON_LABELS = {
    'BANK_DEPOSIT': 'Bank Deposit / Skim',
    'RENT': 'Rent Payment',
    'PETTY_CASH': 'Petty Cash Expense',
    'SUPPLIER_PAYMENT': 'Direct Cash Supplier Payment',
    'OTHER': 'Other Cash Outflow',
}

DASHBOARD_TABLE_PARTIALS = {
    'sessions': 'sales/sessions_table.html',
    'txns': 'sales/cash_txns_table.html',
    'orders': 'sales/orders_table.html',
}


class OpenRegisterView(CashierRequiredMixin, View):
    """
    Opens the ONE shared, continuous cash pool for the cashier's branch.

    The opening float is NOT freely editable: every session after the
    branch's very first one automatically carries forward the prior
    session's physically-counted closing_balance, regardless of which
    cashier opens it. This is what makes the pool continuous rather
    than a fresh per-session/per-cashier drawer. The only time a human
    enters a number here is the branch's first-ever session, to seed
    the pool with its true starting float.
    """

    def get(self, request):
        branch = resolve_user_branch(request.user)

        existing_session = CashRegisterSession.get_active_session(branch)
        if existing_session:
            messages.info(request, "This branch's cash register is already open.")
            return redirect('pos_terminal')

        last_session = CashRegisterSession.objects.filter(
            branch=branch,
            status='CLOSED'
        ).order_by('-closed_at').first()

        is_first_session = last_session is None
        carried_over_float = CashRegisterSession.get_branch_pool_balance(branch)

        return render(request, 'sales/open_register.html', {
            'branch': branch,
            'carried_over_float': carried_over_float,
            'is_first_session': is_first_session,
        })

    def post(self, request):
        branch = resolve_user_branch(request.user)

        existing_session = CashRegisterSession.get_active_session(branch)
        if existing_session:
            return redirect('pos_terminal')

        last_session = CashRegisterSession.objects.filter(
            branch=branch,
            status='CLOSED'
        ).order_by('-closed_at').first()

        if last_session is None:
            raw_float = request.POST.get('opening_balance', '').strip()
            try:
                opening_float = Decimal(raw_float) if raw_float != "" else Decimal('0.00')
            except (InvalidOperation, ValueError, TypeError):
                opening_float = Decimal('0.00')
        else:
            opening_float = CashRegisterSession.get_branch_pool_balance(branch)

        CashRegisterSession.objects.create(
            opened_by=request.user,
            branch=branch,
            opening_balance=opening_float,
            status='OPEN'
        )

        return redirect('pos_terminal')


class CloseRegisterView(CashierRequiredMixin, View):
    def get(self, request):
        branch = resolve_user_branch(request.user)
        session = CashRegisterSession.get_active_session(branch)
        if not session:
            messages.warning(request, "No active cash register session found for this branch.")
            return redirect('open_register')

        context = {
            'session': session,
            'opening_balance': session.opening_balance,
            'total_cash_sales': session.total_cash_sales,
            'total_cash_in': session.total_cash_in,
            'total_cash_out': session.total_cash_out,
            'expected_cash': session.get_current_expected_cash(),
        }
        return render(request, 'sales/close_register.html', context)

    def post(self, request):
        branch = resolve_user_branch(request.user)
        session = CashRegisterSession.get_active_session(branch)
        if not session:
            return redirect('open_register')

        try:
            closing_balance = Decimal(request.POST.get('closing_balance', '0.00'))
        except (ValueError, TypeError):
            closing_balance = Decimal('0.00')

        expected_cash = session.get_current_expected_cash()
        discrepancy = closing_balance - expected_cash
        notes = request.POST.get('notes', '').strip()

        session.closing_balance = closing_balance
        session.expected_closing_balance = expected_cash
        session.discrepancy = discrepancy
        session.notes = notes
        session.status = 'CLOSED'
        session.closed_by = request.user
        session.closed_at = timezone.now()

        session.save()

        messages.success(request, f"Register closed. Discrepancy: KSH {discrepancy:.2f}")
        return redirect('pos_terminal')


class CashTransactionView(AdminRequiredMixin, View):
    """Handles Cash-In (float top-ups) into the branch's shared cash pool."""
    def post(self, request):
        branch = resolve_user_branch(request.user)
        session = CashRegisterSession.get_active_session(branch)
        if not session:
            messages.error(request, "No active session. Please open the register first.")
            return redirect('open_register')

        reason = request.POST.get('reason', '').strip()
        try:
            amount = Decimal(request.POST.get('amount', '0.00'))
        except (ValueError, TypeError):
            amount = Decimal('0.00')

        if amount <= 0:
            messages.error(request, "Invalid transaction details.")
            return redirect('pos_terminal')

        CashTransaction.objects.create(
            session=session,
            user=request.user,
            transaction_type='CASH_IN',
            amount=amount,
            reason=reason
        )
        messages.success(request, f"Cash In of KSH {amount} recorded.")
        return redirect('pos_terminal')


class RecordCashOutView(AdminRequiredMixin, View):
    """
    Admin-only cash withdrawal from the branch's shared pool — bank
    deposits, rent, petty cash, supplier payments, etc. The amount is
    deducted from the OVERALL branch pool (via
    CashRegisterSession.get_current_expected_cash, which nets every
    cash-in/out and sale against the carried-forward balance) rather
    than treated as belonging only to "this session". Matches
    sales/record_cash_out.html (category dropdown + free-text notes).
    """

    def get(self, request):
        branch = resolve_user_branch(request.user)
        active_session = CashRegisterSession.get_active_session(branch)
        return render(request, 'sales/record_cash_out.html', {
            'active_session': active_session,
        })

    def post(self, request):
        branch = resolve_user_branch(request.user)
        session = CashRegisterSession.get_active_session(branch)
        if not session:
            messages.error(request, "No active register session — open the register before recording a cash out.")
            return redirect('sales-dashboard')

        reason_code = request.POST.get('reason', '').strip()
        notes = request.POST.get('notes', '').strip()

        try:
            amount = Decimal(request.POST.get('amount', '0.00'))
        except (InvalidOperation, ValueError, TypeError):
            amount = Decimal('0.00')

        if amount <= 0:
            messages.error(request, "Enter a valid amount to withdraw.")
            return redirect('record-cash-out')

        available_cash = session.get_current_expected_cash()
        if amount > available_cash:
            messages.error(
                request,
                f"Cannot withdraw KSH {amount}. Only KSH {available_cash} is currently available in the pool."
            )
            return redirect('record-cash-out')

        reason_label = CASH_OUT_REASON_LABELS.get(reason_code, reason_code or 'Cash Out')
        full_reason = f"{reason_label} — {notes}" if notes else reason_label

        CashTransaction.objects.create(
            session=session,
            user=request.user,
            transaction_type='CASH_OUT',
            amount=amount,
            reason=full_reason
        )

        messages.success(request, f"KSH {amount} recorded as Cash Out ({reason_label}).")
        return redirect('sales-dashboard')


class PosTerminalView(CashierRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        branch = resolve_user_branch(request.user)
        session = CashRegisterSession.get_active_session(branch)
        if not session:
            return redirect('open_register')

        q = request.GET.get('q', '').strip().lower()
        category_id = request.GET.get('category', '').strip()

        categories = Category.objects.filter(parent=None)
        products = ProductVariant.objects.select_related(
            'product', 'product__unit_of_measure'
        ).filter(is_active=True, product__is_active=True)

        active_category = None
        if category_id:
            try:
                products = products.filter(
                    Q(product__category_id=category_id) | Q(product__category__parent_id=category_id)
                )
                active_category = int(category_id)
            except (ValueError, TypeError):
                pass

        if q:
            products = products.filter(
                Q(sku__icontains=q) | Q(product__name__icontains=q)
            )
        else:
            products = products[:24]

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            html = render_to_string(
                'sales/product_list.html', {'products': products}, request=request
            )
            return JsonResponse({'ok': True, 'products_html': html})

        cart = POSCart(request)
        context = {
            'categories': categories,
            'products': products,
            'cart': cart,
            'search_query': q,
            'active_category': active_category,
            'active_session': session,
            'current_expected_cash': session.get_current_expected_cash(),
        }
        return render(request, 'sales/terminal.html', context)


def _cart_json_response(request):
    cart = POSCart(request)
    html = render_to_string(
        'sales/cart_contents.html',
        {'cart': cart},
        request=request,
    )
    return JsonResponse({
        'ok': True,
        'cart_html': html,
        'total_items': cart.total_items,
    })


def _stock_error_response(request, error):
    message = (
        f"Only {error.available} of {error.variant.sku} left in stock (you tried to have {error.requested})"
    )

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        cart = POSCart(request)
        html = render_to_string(
            'sales/cart_contents.html',
            {'cart': cart},
            request=request
        )

        return JsonResponse({
            'ok': False,
            'error': message,
            'cart_html': html,
            'total_items': cart.total_items
        }, status=400)

    messages.error(request, message)
    return redirect(request.META.get('HTTP_REFERER', '/pos/'))


@require_POST
def cart_add(request):
    cart = POSCart(request)
    variant_id = request.POST.get('variant_id')
    sku = request.POST.get('sku')

    try:
        if sku:
            variant = ProductVariant.objects.filter(
                sku__iexact=sku.strip(), is_active=True
            ).first()
            if variant:
                cart.add(variant_id=variant.id, quantity=1)
        elif variant_id:
            cart.add(variant_id=variant_id, quantity=1)

    except InsufficientStockError as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return _stock_error_response(request, e)

        messages.error(request, str(e))
        return redirect(request.META.get('HTTP_REFERER', '/pos/'))

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return _cart_json_response(request)

    return redirect(request.META.get('HTTP_REFERER', '/pos/'))


@require_POST
def cart_update(request):
    cart = POSCart(request)
    variant_id = request.POST.get('variant_id')
    quantity = request.POST.get('quantity', 1)

    try:
        if variant_id:
            cart.update_quantity(variant_id=variant_id, quantity=quantity)
    except InsufficientStockError as e:
        return _stock_error_response(request, e)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return _cart_json_response(request)

    return redirect(request.META.get('HTTP_REFERER', '/pos/'))


@require_POST
def cart_remove(request):
    cart = POSCart(request)
    variant_id = request.POST.get('variant_id')

    if variant_id:
        cart.remove(variant_id)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return _cart_json_response(request)

    return redirect(request.META.get('HTTP_REFERER', '/pos/'))


def _resolve_report_period(request, now):
    """
    Resolves the dashboard's reporting window from ?period_start= and
    ?period_end= query params (datetime-local strings, e.g.
    "2026-09-22T06:00"), falling back to month-to-date when they're
    absent or invalid.

    Supports both whole-day ranges ("Monday to Tuesday" -> pick
    00:00 on each date) and partial-day ranges ("Monday 6am to Monday
    12pm") since the client sends full datetimes either way — the
    dashboard's quick-pick buttons just fill in the date+00:00 for the
    whole-day case.

    Returns (start, end, label, is_custom).
    """
    raw_start = request.GET.get('period_start', '').strip()
    raw_end = request.GET.get('period_end', '').strip()

    if raw_start and raw_end:
        start_dt = parse_datetime(raw_start)
        end_dt = parse_datetime(raw_end)

        if start_dt and end_dt:
            if timezone.is_naive(start_dt):
                start_dt = timezone.make_aware(start_dt, timezone.get_current_timezone())
            if timezone.is_naive(end_dt):
                end_dt = timezone.make_aware(end_dt, timezone.get_current_timezone())

            if end_dt > start_dt:
                local_start = timezone.localtime(start_dt)
                local_end = timezone.localtime(end_dt)
                label = f"{local_start.strftime('%b %d, %Y %H:%M')} — {local_end.strftime('%b %d, %Y %H:%M')}"
                return start_dt, end_dt, label, True

    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_of_month, now, f"Month to Date — {now.strftime('%B %Y')}", False


def _paginate(request, queryset, page_param, page_size=15):
    """
    Paginates queryset using page_param as the query-string key for
    this specific table (each of the dashboard's three tables has its
    own key: sessions_page / txns_page / orders_page) so paging one
    table doesn't reset the others, and get_page() clamps out-of-range
    or non-integer values instead of raising.
    """
    paginator = Paginator(queryset, page_size)
    raw_page = request.GET.get(page_param, 1)
    return paginator.get_page(raw_page)


def _page_link_qs(request, page_param, page_number):
    """
    Query string for a pagination link: the current GET params (which
    already include the report period and every table's current page)
    with just this one table's page key swapped out. This is what
    lets "next page" on the sessions table keep the selected report
    period AND the other two tables' current pages intact — and it's
    also exactly the URL the AJAX pagination JS fetches from.
    """
    params = request.GET.copy()
    params[page_param] = page_number
    return params.urlencode()


class SalesDashboardView(AdminRequiredMixin, TemplateView):
    template_name = 'sales/dashboard.html'

    def get(self, request, *args, **kwargs):
        context = self.get_context_data(**kwargs)

        requested_table = request.GET.get('table')
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        if is_ajax and requested_table in DASHBOARD_TABLE_PARTIALS:
            html = render_to_string(
                DASHBOARD_TABLE_PARTIALS[requested_table], context, request=request
            )
            return JsonResponse({'ok': True, 'table': requested_table, 'html': html})

        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        now = timezone.now()
        period_start, period_end, period_label, is_custom_period = _resolve_report_period(self.request, now)

        orders = Order.objects.all()
        period_orders = orders.filter(created_at__gte=period_start, created_at__lt=period_end)
        all_sessions = CashRegisterSession.objects.all()
        period_sessions = all_sessions.filter(opened_at__gte=period_start, opened_at__lt=period_end)
        closed_sessions = all_sessions.filter(status='CLOSED')

        sales_agg = period_orders.aggregate(
            total_revenue=Sum('total_revenue'),
            total_cogs=Sum('total_cogs'),
            total_profit=Sum('total_profit')
        )

        revenue = sales_agg['total_revenue'] or Decimal('0.00')
        cogs = sales_agg['total_cogs'] or Decimal('0.00')
        profit = sales_agg['total_profit'] or (revenue - cogs)

        margin = round((profit / revenue * 100), 2) if revenue > Decimal('0.00') else Decimal('0.00')

        context['metrics'] = {
            'revenue': revenue,
            'cogs': cogs,
            'profit': profit,
            'margin': margin,
        }

        total_cash_sales_all_time = orders.filter(
            payment_method='CASH'
        ).aggregate(Sum('total_revenue'))['total_revenue__sum'] or Decimal('0.00')

        latest_closed_session_id_per_branch = (
            CashRegisterSession.objects.filter(
                branch=OuterRef('branch'), status='CLOSED'
            ).order_by('-closed_at').values('id')[:1]
        )
        latest_closed_sessions = closed_sessions.filter(
            id=Subquery(latest_closed_session_id_per_branch)
        )

        total_counted_cash = latest_closed_sessions.aggregate(
            Sum('closing_balance')
        )['closing_balance__sum'] or Decimal('0.00')

        total_opening_floats = closed_sessions.aggregate(
            Sum('opening_balance')
        )['opening_balance__sum'] or Decimal('0.00')

        all_time_closing_sum = closed_sessions.aggregate(
            Sum('closing_balance')
        )['closing_balance__sum'] or Decimal('0.00')

        total_discrepancy = closed_sessions.aggregate(
            Sum('discrepancy')
        )['discrepancy__sum'] or Decimal('0.00')

        active_session = all_sessions.filter(status='OPEN').select_related('branch').first()
        live_pool_balance = active_session.get_current_expected_cash() if active_session else Decimal('0.00')

        context['cumulative_cash'] = {
            'total_cash_revenue': total_cash_sales_all_time,
            'total_counted_cash': total_counted_cash,
            'total_discrepancy': total_discrepancy,
            'live_pool_balance': live_pool_balance,
            'total_vault_cash': all_time_closing_sum - total_opening_floats,
        }

        period_cash_txns = CashTransaction.objects.filter(
            created_at__gte=period_start, created_at__lt=period_end
        )

        cash_in = period_cash_txns.filter(
            transaction_type='CASH_IN'
        ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

        cash_out = period_cash_txns.filter(
            transaction_type='CASH_OUT'
        ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

        period_discrepancy = period_sessions.filter(
            status='CLOSED'
        ).aggregate(Sum('discrepancy'))['discrepancy__sum'] or Decimal('0.00')

        context['cash_metrics'] = {
            'active_sessions_count': all_sessions.filter(status='OPEN').count(),
            'cash_in': cash_in,
            'cash_out': cash_out,
            'discrepancies': period_discrepancy,
        }

        context['period'] = {
            'label': period_label,
            'is_custom': is_custom_period,
            'start_input': timezone.localtime(period_start).strftime('%Y-%m-%dT%H:%M'),
            'end_input': timezone.localtime(period_end).strftime('%Y-%m-%dT%H:%M'),
            'order_count': period_orders.count(),
        }

        sessions_qs = period_sessions.select_related('opened_by', 'branch').order_by('-opened_at')
        txns_qs = period_cash_txns.select_related('user').order_by('-created_at')
        orders_qs = period_orders.select_related('branch').order_by('-created_at')

        sessions_page = _paginate(self.request, sessions_qs, 'sessions_page', page_size=15)
        txns_page = _paginate(self.request, txns_qs, 'txns_page', page_size=15)
        orders_page = _paginate(self.request, orders_qs, 'orders_page', page_size=15)

        context['recent_sessions'] = sessions_page
        context['recent_cash_txns'] = txns_page
        context['recent_orders'] = orders_page

        context['sessions_prev_qs'] = (
            _page_link_qs(self.request, 'sessions_page', sessions_page.previous_page_number())
            if sessions_page.has_previous() else None
        )
        context['sessions_next_qs'] = (
            _page_link_qs(self.request, 'sessions_page', sessions_page.next_page_number())
            if sessions_page.has_next() else None
        )
        context['txns_prev_qs'] = (
            _page_link_qs(self.request, 'txns_page', txns_page.previous_page_number())
            if txns_page.has_previous() else None
        )
        context['txns_next_qs'] = (
            _page_link_qs(self.request, 'txns_page', txns_page.next_page_number())
            if txns_page.has_next() else None
        )
        context['orders_prev_qs'] = (
            _page_link_qs(self.request, 'orders_page', orders_page.previous_page_number())
            if orders_page.has_previous() else None
        )
        context['orders_next_qs'] = (
            _page_link_qs(self.request, 'orders_page', orders_page.next_page_number())
            if orders_page.has_next() else None
        )

        context['timestamp'] = now

        return context


class OrderDetailView(AdminRequiredMixin, DetailView):
    model = Order
    template_name = "sales/order_detail.html"
    context_object_name = "order"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['line_items'] = self.object.items.select_related('variant__product').all()

        total_items_count = sum(item.quantity for item in context['line_items'])
        context['total_items_count'] = total_items_count

        order_margin = 0.0
        if self.object.total_revenue > 0:
            order_margin = round((float(self.object.total_profit) / float(self.object.total_revenue)) * 100, 2)
        context['order_margin'] = order_margin

        return context