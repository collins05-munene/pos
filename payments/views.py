from decimal import Decimal, InvalidOperation
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST, require_GET
from django.contrib import messages

from sales.cart import POSCart
from sales.models import CashRegisterSession
from sales.exceptions import InsufficientStockError
from sales.utils import complete_pos_sale, resolve_user_branch


@require_GET
def payment_select(request):
    cart = POSCart(request)
    if cart.total_items == 0:
        messages.warning(request, 'Your cart is empty.')
        return redirect('pos_terminal')

    context = {
        'cart': cart,
        'total_amount': cart.get_total_price
    }
    return render(request, 'sales/payment_select.html', context)


@require_POST
def process_payment(request):
    cart = POSCart(request)
    if cart.total_items == 0:
        return redirect('pos_terminal')

    total_amount = cart.get_total_price

    branch = resolve_user_branch(request.user)
    session = CashRegisterSession.get_active_session(branch)
    if not session:
        messages.error(request, "Cannot process payment: No active cash register session.")
        return redirect('open_register')

    raw_received = request.POST.get('amount_received', '').strip()
    try:
        amount_received = Decimal(raw_received)
    except (InvalidOperation, ValueError, TypeError):
        messages.error(request, "Enter a valid amount received.")
        return redirect('payment_select')

    if amount_received < total_amount:
        messages.error(
            request,
            f"Amount received (KSH {amount_received}) is less than the total due (KSH {total_amount})."
        )
        return redirect('payment_select')

    try:
        order = complete_pos_sale(
            cart=cart,
            payment_method='CASH',
            cashier=request.user,
            cash_session=session,
            amount_received=amount_received
        )
    except InsufficientStockError as e:
        messages.error(request, str(e))
        return redirect('payment_select')
    except ValueError as e:
        messages.error(request, str(e))
        return redirect('payment_select')

    request.session['pos_cart'] = {}
    request.session.modified = True
    cart.save()

    messages.success(
        request,
        f"Sale Completed! Received KSH {order.amount_received}, Change Given: KSH {order.change_given}"
    )
    return redirect('pos_terminal')