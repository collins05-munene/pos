from decimal import Decimal
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Q
from django.views.generic import TemplateView, DetailView
from django.utils import timezone
from django.db.models import Sum, F, ExpressionWrapper, DecimalField
from django.contrib import messages
from decimal import Decimal
from django.views import View
import logging

from products.models import ProductVariant, Category
from .cart import POSCart
from .models import Order, OrderItem, CashRegisterSession, CashTransaction
from .exceptions import InsufficientStockError
from .forms import CashOutForm
from users.mixins import AdminRequiredMixin, CashierRequiredMixin
from inventory.models import Branch



logger = logging.getLogger(__name__)

class RecordCashOutView(CashierRequiredMixin, View):
    template_name = 'sales/record_cash_out.html'

    def get_active_session(self, request):
        """Fetch active open session for the current cashier or branch."""
        return CashRegisterSession.objects.filter(
            cashier=request.user, 
            status='OPEN'
        ).first()

    def get(self, request, *args, **kwargs):
        active_session = self.get_active_session(request)
        form = CashOutForm(session=active_session)

        return render(request, self.template_name, {
            'active_session': active_session,
            'form': form,
        })

    def post(self, request, *args, **kwargs):
        active_session = self.get_active_session(request)

        if not active_session:
            messages.error(request, "Cannot complete transaction: No open register session found.")
            return redirect('sales-dashboard')

        form = CashOutForm(request.POST, session=active_session)

        if form.is_valid():
            amount = form.cleaned_data['amount']
            reason = form.cleaned_data['reason']
            notes = form.cleaned_data['notes']

            reason_label = dict(form.fields['reason'].choices).get(reason, reason)
            full_reason = f"{reason_label} - {notes}" if notes else reason_label

            CashTransaction.objects.create(
                session=active_session,
                user=request.user,
                transaction_type='CASH_OUT',
                amount=amount,
                reason=full_reason
            )

            messages.success(
                request, 
                f"Successfully recorded Cash Out of KSH {amount:.2f}. New estimated drawer balance: KSH {active_session.expected_closing_balance:.2f}"
            )
            return redirect('sales-dashboard')

        for error in form.errors.values():
            messages.error(request, error[0])

        return render(request, self.template_name, {
            'active_session': active_session,
            'form': form,
        })

class OpenRegisterView(View):
    def get_user_branch(self, user):
        if hasattr(user, 'branch') and user.branch:
            return user.branch
        if hasattr(user, 'profile') and hasattr(user.profile, 'branch'):
            return user.profile.branch
        return Branch.objects.first()

    def get(self, request):
        branch = self.get_user_branch(request.user)
        last_session = CashRegisterSession.objects.filter(
            branch=branch, 
            status='CLOSED'
        ).order_by('-closed_at').first()

        carried_over_float = last_session.closing_balance if last_session and last_session.closing_balance else Decimal('0.00')

        return render(request, 'sales/open_register.html', {
            'branch': branch,
            'carried_over_float': carried_over_float
        })

    def post(self, request):
        branch = self.get_user_branch(request.user)
        last_session = CashRegisterSession.objects.filter(
            branch=branch, 
            status='CLOSED'
        ).order_by('-closed_at').first()

        carried_over_float = last_session.closing_balance if last_session and last_session.closing_balance else Decimal('0.00')

        raw_float = request.POST.get('opening_balance', '').strip()
        
        if raw_float:
            try:
                opening_float = Decimal(raw_float)
            except (ValueError, TypeError):
                opening_float = carried_over_float
        else:
            opening_float = carried_over_float

        CashRegisterSession.objects.create(
            cashier=request.user,
            branch=branch,
            opening_balance=opening_float,
            status='OPEN'
        )

        return redirect('pos_terminal')
    
class CloseRegisterView(CashierRequiredMixin, View):
    def get(self, request):
        session = CashRegisterSession.get_active_session(request.user)
        if not session:
            messages.warning(request, "No active cash register session found.")
            return redirect('open_register')

        expected_cash = session.get_current_expected_cash()
        context = {
            'session': session,
            'expected_cash': expected_cash,
        }
        return render(request, 'sales/close_register.html', context)

    def post(self, request):
        session = CashRegisterSession.get_active_session(request.user)
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
        session.closed_at = timezone.now()
        
        session.save()

        messages.success(request, f"Register closed. Discrepancy: KSH {discrepancy:.2f}")
        return redirect('pos_terminal')
        


class CashTransactionView(CashierRequiredMixin, View):
    """Handles Pay-In / Pay-Out during an active session"""
    def post(self, request):
        session = CashRegisterSession.get_active_session(request.user)
        if not session:
            messages.error(request, "No active session. Please open the register first.")
            return redirect('open_register')

        txn_type = request.POST.get('transaction_type')
        reason = request.POST.get('reason', '').strip()
        try:
            amount = Decimal(request.POST.get('amount', '0.00'))
        except (ValueError, TypeError):
            amount = Decimal('0.00')

        if amount <= 0 or txn_type not in ['CASH_IN', 'CASH_OUT']:
            messages.error(request, "Invalid transaction details.")
            return redirect('pos_terminal')

        CashTransaction.objects.create(
            session=session,
            user=request.user,
            transaction_type=txn_type,
            amount=amount,
            reason=reason
        )
        messages.success(request, f"{txn_type.replace('_', ' ')} of KSH {amount} recorded.")
        return redirect('pos_terminal')



class PosTerminalView(CashierRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        # Redirect to open register if cashier has no active session
        session = CashRegisterSession.get_active_session(request.user)
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
        
        from django.contrib import messages
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

class SalesDashboardView(AdminRequiredMixin, TemplateView):
    template_name = 'sales/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        today = timezone.now()
        start_of_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        orders = Order.objects.all()
        monthly_orders = orders.filter(created_at__gte=start_of_month)
        all_sessions = CashRegisterSession.objects.all()
        monthly_sessions = all_sessions.filter(opened_at__gte=start_of_month)
        closed_sessions = all_sessions.filter(status='CLOSED')

        sales_agg = monthly_orders.aggregate(
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

        total_opening_floats = closed_sessions.aggregate(
            Sum('opening_balance')
        )['opening_balance__sum'] or Decimal('0.00')

        total_counted_cash = closed_sessions.aggregate(
            Sum('closing_balance')
        )['closing_balance__sum'] or Decimal('0.00')

        total_discrepancy = closed_sessions.aggregate(
            Sum('discrepancy')
        )['discrepancy__sum'] or Decimal('0.00')

        active_session = all_sessions.filter(status='OPEN').first()
        active_drawer_float = active_session.opening_balance if active_session else Decimal('0.00')

        context['cumulative_cash'] = {
            'total_cash_revenue': total_cash_sales_all_time,
            'total_counted_cash': total_counted_cash,
            'total_discrepancy': total_discrepancy,
            'active_drawer_float': active_drawer_float,
            'total_vault_cash': total_counted_cash - total_opening_floats,
        }

        monthly_cash_txns = CashTransaction.objects.filter(created_at__gte=start_of_month)
        
        cash_in = monthly_cash_txns.filter(
            transaction_type='CASH_IN'
        ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')
        
        cash_out = monthly_cash_txns.filter(
            transaction_type='CASH_OUT'
        ).aggregate(Sum('amount'))['amount__sum'] or Decimal('0.00')

        monthly_discrepancy = monthly_sessions.filter(
            status='CLOSED'
        ).aggregate(Sum('discrepancy'))['discrepancy__sum'] or Decimal('0.00')

        context['cash_metrics'] = {
            'active_sessions_count': all_sessions.filter(status='OPEN').count(),
            'cash_in': cash_in,
            'cash_out': cash_out,
            'discrepancies': monthly_discrepancy,
        }

        context['recent_sessions'] = all_sessions.select_related('cashier', 'branch').order_by('-opened_at')[:15]
        context['recent_cash_txns'] = monthly_cash_txns.select_related('user').order_by('-created_at')[:10]
        context['recent_orders'] = orders.select_related('branch').order_by('-created_at')[:10]
        context['timestamp'] = today

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
            order_margin - round((float(self.object.total_profit) / float(self.object.total_revenue)) * 100, 2)
        context['order_margin'] = order_margin

        return context