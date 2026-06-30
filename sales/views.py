from decimal import Decimal
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Q
from products.models import ProductVariant, Category
from .cart import POSCart


@require_GET
def pos_terminal(request):
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
                Q(product__category_id=category_id) |
                Q(product__category__parent_id=category_id)
            )
            active_category = int(category_id)
        except (ValueError, TypeError):
            pass

    if q:
        products = products.filter(
            Q(sku__icontains=q) |
            Q(product__name__icontains=q)
        )
    else:
        products = products[:24]

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        html = render_to_string(
            'sales/product_list.html',
            {'products': products},
            request=request,
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
    """Render cart_contents partial and return as JSON payload."""
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


@require_POST
def cart_add(request):
    cart = POSCart(request)
    variant_id = request.POST.get('variant_id')
    sku = request.POST.get('sku')

    if sku:
        variant = ProductVariant.objects.filter(
            sku__iexact=sku.strip(), is_active=True
        ).first()
        if variant:
            cart.add(variant_id=variant.id, quantity=1)
    elif variant_id:
        cart.add(variant_id=variant_id, quantity=1)

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return _cart_json_response(request)

    return redirect(request.META.get('HTTP_REFERER', '/pos/'))


@require_POST
def cart_update(request):
    cart = POSCart(request)
    variant_id = request.POST.get('variant_id')
    quantity = request.POST.get('quantity', 1)

    if variant_id:
        cart.update_quantity(variant_id=variant_id, quantity=quantity)

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