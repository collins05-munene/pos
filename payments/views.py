import uuid
from decimal import Decimal, InvalidOperation
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST, require_GET
from django.contrib import messages

from sales.cart import POSCart
from sales.models import CashRegisterSession, Order
from sales.exceptions import InsufficientStockError
from sales.utils import complete_pos_sale, resolve_user_branch
from notifications.models import Notification
from notifications.utils import notify, MANAGEMENT

@require_GET
def payment_select(request):
    cart = POSCart(request)
    if cart.total_items == 0:
        messages.warning(request, 'Your cart is empty.')
        return redirect('pos_terminal')

    context = {
        'cart': cart,
        'total_amount': cart.get_total_price,
        # One id per checkout attempt. It travels with the sale wherever it ends up
        # (this request, or the offline queue after a dropped connection), so the
        # sale can never be recorded twice.
        'checkout_uuid': uuid.uuid4(),
    }
    return render(request, 'sales/payment_select.html', context)


def _wants_json(request):
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _parse_uuid(raw):
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


@require_POST
def process_payment(request):
    wants_json = _wants_json(request)
    client_uuid = _parse_uuid(request.POST.get('client_uuid'))

    def go(target, *args):
        url = reverse(target, args=args) if args else reverse(target)
        return url

    def fail(message, target='payment_select'):
        if wants_json:
            return JsonResponse({'ok': False, 'message': message, 'redirect': go(target)}, status=400)
        messages.error(request, message)
        return redirect(target)

    def done(order, note):
        url = f"{reverse('receipt-detail', args=[order.receipt.pk])}?new=1"
        if note:
            messages.success(request, note)
        if wants_json:
            return JsonResponse({'ok': True, 'redirect': url})
        return redirect(url)

    # Retry of a checkout that already went through (response lost, double tap):
    # answer with the sale that exists instead of selling twice.
    if client_uuid:
        existing = Order.objects.filter(client_uuid=client_uuid).select_related('receipt').first()
        if existing:
            request.session['pos_cart'] = {}
            request.session.modified = True
            return done(existing, "This sale was already recorded.")

    cart = POSCart(request)
    if cart.total_items == 0:
        if wants_json:
            return JsonResponse({'ok': False, 'message': 'Your cart is empty.',
                                 'redirect': go('pos_terminal')}, status=400)
        return redirect('pos_terminal')

    total_amount = cart.get_total_price

    branch = resolve_user_branch(request.user)
    session = CashRegisterSession.get_active_session(branch)
    if not session:
        return fail("Cannot process payment: No active cash register session.", 'open_register')

    raw_received = request.POST.get('amount_received', '').strip()
    try:
        amount_received = Decimal(raw_received)
    except (InvalidOperation, ValueError, TypeError):
        return fail("Enter a valid amount received.")

    if amount_received < total_amount:
        return fail(f"Amount received (KSH {amount_received}) is less than the total due (KSH {total_amount}).")

    try:
        order = complete_pos_sale(
            cart=cart,
            payment_method='CASH',
            cashier=request.user,
            cash_session=session,
            amount_received=amount_received,
            client_uuid=client_uuid,
        )
    except InsufficientStockError as e:
        notify(request.tenant, "Sale blocked: stock mismatch",
               f"{request.user.get_username()} could not complete a sale. {e}",
               level=Notification.Level.WARNING, roles=MANAGEMENT, dedupe_hours=6)
        return fail(str(e))

    except ValueError as e:
        return fail(str(e))

    request.session['pos_cart'] = {}
    request.session.modified = True
    cart.save()

    return done(order, f"Sale Completed! Received KSH {order.amount_received}, Change Given: KSH {order.change_given}")
