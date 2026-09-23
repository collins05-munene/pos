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
from .models import Order, OrderItem
from .exceptions import InsufficientStockError
from users.mixins import AdminRequiredMixin, CashierRequiredMixin



logger = logging.getLogger(__name__)
class PosTerminalView(CashierRequiredMixin, View): 
    
    def get(self, request, *args, **kwargs):
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

        totals = monthly_orders.aggregate(
            revenue=Sum('total_revenue'),
            cogs=Sum('total_cogs'),
            profit=Sum('total_profit')
        )

        revenue = totals['revenue'] or 0.00
        cogs = totals['cogs'] or 0.00
        profit = totals['profit'] or 0.00

        profit_margin = 0.0
        if revenue > 0:
            profit_margin = round((float(profit) / float(revenue)) * 100, 2)
        
        context['metrics'] = {
            'revenue': revenue,
            'cogs': cogs,
            'profit': profit,
            'margin': profit_margin,
            'total_sales_count': monthly_orders.count()
        }
        
        payment_breakdown = monthly_orders.values('payment_method').annotate(
            method_revenue=Sum('total_revenue'),
            method_profit=Sum('total_profit')
        )
        context['payment_data'] = payment_breakdown

        context['recent_orders'] = orders.order_by('-created_at')[:10]

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