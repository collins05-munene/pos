from django.shortcuts import render, render, redirect
import base64
import requests
import json
from datetime import datetime
from django.views import View
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.views.decorators.http import require_POST, require_GET
from django.contrib import messages

from sales.cart import POSCart
from sales.models import CashRegisterSession
from sales.exceptions import InsufficientStockError
from .models import MpesaTransaction
from sales.utils import complete_pos_sale

# Create your views here.
@require_GET
def payment_select(map_request):
    cart = POSCart(map_request)
    if cart.total_items == 0:
        messages.warning(map_request, 'Your cart is empty.')
        return redirect('pos_terminal')
    
    context = {
        'cart': cart,
        'total_amount': cart.get_total_price
    }
    return render(map_request, 'sales/payment_select.html', context)

@require_POST
def process_payment(map_request):
    cart = POSCart(map_request)
    if cart.total_items == 0:
        return redirect('pos_terminal')
    
    payment_method = map_request.POST.get('payment_method')
    total_amount = cart.get_total_price

    session = CashRegisterSession.get_active_session(map_request.user)
    if not session:
        messages.error(map_request, "Cannot process payment: No active cash register session.")
        return redirect('open_register')

    if payment_method == 'cash':
        complete_pos_sale(
            cart=cart,
            payment_method='CASH',
            cashier=map_request.user,
            cash_session=session  
        )
        map_request.session['pos_cart'] = {}
        map_request.session.modified = True
        cart.save()
        messages.success(map_request, f'Cash Sale Completed! Collected KSH {total_amount}')
        return redirect('pos_terminal')
        
    elif payment_method == 'mpesa':
        return redirect('mpesa_prompt')
    
    return redirect('payment_select')

@require_GET
def mpesa_prompt(map_request):
    cart = POSCart(map_request)
    if cart.total_items == 0:
        return redirect('pos_terminal')
    
    context = {
        'total_amount': cart.get_total_price
    }

    return render(map_request, 'sales/mpesa_prompt.html', context)

class MpesaSTKPushView(View):
    def get_access_token(self):
        api_url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
        response = requests.get(
            api_url,
            auth=(settings.MPESA_CONSUMER_KEY, settings.MPESA_CONSUMER_SECRET)
        )

        if response.status_code == 200:
            return response.json().get('access_token')
        raise Exception('Failed to Fetch Access Token')
    
   
    
    def post(self, request, *args, **kwargs):
        cart = POSCart(request)
        total_amount = cart.get_total_price

        if total_amount <= 0:
            return JsonResponse({
                'status': 'error' ,
                'message': 'Your cart amount is empty'
            }, status=400)
        
        amount_to_charge = int(total_amount)

        phone_number = request.POST.get('phone_number')

        if not phone_number and request.body:
            try:
                data = json.loads(request.body)
                phone_number = data.get('phone_number')
            except json.JSONDecodeError:
                phone_number = request.POST.get('phone_number')

        if not phone_number:
            return JsonResponse({
                'status': 'error',
                'message': 'Phone number is required'
            }, status=400)

        if phone_number.startswith('0'):
            phone_number = '254' + phone_number[1:]
        elif phone_number.startswith('+'):
            phone_number = phone_number.replace('+', '')

        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        password_str = f"{settings.MPESA_SHORTCODE}{settings.MPESA_PASSKEY}{timestamp}"
        password = base64.b64encode(password_str.encode('utf-8')).decode('utf-8')

        try:
            access_token = self.get_access_token()
        except Exception as e:
            return JsonResponse({
                'status': 'error',
                'message': str(e)
            }, status=500)
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "BusinessShortCode": settings.MPESA_SHORTCODE,
            "Password": password,
            "Timestamp": timestamp,
            "TransactionType": "CustomerPayBillOnline",
            "Amount": amount_to_charge,
            "PartyA": phone_number,
            "PartyB": settings.MPESA_SHORTCODE,
            "PhoneNumber": phone_number,
            "CallBackURL": settings.MPESA_CALLBACK_URL,
            "AccountReference": "POS_Order",
            "TransactionDesc": "POS Cart Checkout"
        }
        

        api_url = 'https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest'

        response = requests.post(api_url, json=payload, headers=headers)
        response_data = response.json()

        print('Safaricom Response Data:', response_data)
        if response.status_code == 200 and response_data.get("ResponseCode") == '0':
            MpesaTransaction.objects.create(
                checkout_request_id=response_data.get('CheckoutRequestID'),
                merchant_request_id=response_data.get('MerchantRequestID'),
                phone_number=phone_number,
                amount=amount_to_charge,
                status='PENDING'
            )
            return JsonResponse({
                'status': 'success',
                'message': 'STK Push initiated successfully. Waiting for client to enter pin.',
                'mechant_request_id': response_data.get('MerchantRequestID'),
                'checkout_request_id': response_data.get('CheckoutRequestID')
            })
        else:
            return JsonResponse({
                'status': 'error',
                'message': response_data.get('ResponseDescription', 'STK Push failed.')
            }, status=400)
        
@method_decorator(csrf_exempt, name='dispatch')
class MpesaCallbackView(View):
    def post(self, request, *args, **kwargs):
        callback_data = json.loads(request.body)

        stk_callback = callback_data.get('Body', {}).get('stkCallback', {})
        result_code = stk_callback.get('ResultCode')
        merchant_request_id = stk_callback.get('MerchantRequestID')
        checkout_request_id = stk_callback.get('CheckoutRequestID')

        try:
            transaction = MpesaTransaction.objects.get(checkout_request_id=checkout_request_id)
            if result_code == 0:
                transaction.status = 'SUCCESS'
                callback_metadata = stk_callback.get('CallbackMetadata', {}).get('Item', [])

                metadata = {item['Name']: item.get('Value') for item in callback_metadata}
                mpesa_receipt_number = metadata.get('MpesaReceiptNumber')
                amount_paid = metadata.get('Amount')
                phone_number = metadata.get('PhoneNumber')
            else:
                transaction.status = 'FAILED'
            transaction.result_description = stk_callback.get('ResultDesc')
            transaction.save()

        except MpesaTransaction.DoesNotExist:
            pass

        return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted Successfully"})
    

class CheckMpesaStatusView(View):
    def get(self, request, checkout_request_id, *args, **kwargs):
        try:
            transaction = MpesaTransaction.objects.get(checkout_request_id=checkout_request_id)
            return JsonResponse({
                'status': transaction.status,
                'message': transaction.result_description or 'Waiting for user interaction...'

            })
        except MpesaTransaction.DoesNotExist:
            return JsonResponse({
                'status': 'NOT_FOUND',
                'message': 'Transaction record missing.'
            }, status=400)
        
        
class ClearPOSCartView(View):
    def post(self, request, *args, **kwargs):
        checkout_request_id = request.POST.get('checkout_request_id')
        cart = POSCart(request)
        
        if cart.total_items == 0:
            return JsonResponse({'status': 'already_empty'}, status=400)

        if checkout_request_id:
            try:
                transaction_record = MpesaTransaction.objects.get(
                    checkout_request_id=checkout_request_id
                )
            except MpesaTransaction.DoesNotExist:
                return JsonResponse({
                    'status': 'error',
                    'message': 'No matching transaction found.'
                }, status=400)

            if transaction_record.status == 'PENDING':
                return JsonResponse({
                    'status': 'pending',
                    'message': 'Waiting for client to enter PIN...'
                }, status=200) 

            if transaction_record.status == 'FAILED':
                return JsonResponse({
                    'status': 'error',
                    'message': 'Payment was cancelled or failed.'
                }, status=400)

           
            if getattr(transaction_record, 'order', None):
                request.session['pos_cart'] = {}
                request.session.modified = True
                return JsonResponse({'status': 'already_completed'})

       
        try:
            order = complete_pos_sale(
                cart=cart,
                payment_method='MPESA',
                cashier=request.user
            )
        except InsufficientStockError as e:
            return JsonResponse({
                'status': 'error',
                'message': f'Payment received but sale could not be completed: {e}' 
            }, status=409)

        if checkout_request_id:
            transaction_record.order = order
            transaction_record.save(update_fields=['order'])
             
        
        request.session['pos_cart'] = {}
        request.session.modified = True
        return JsonResponse({'status': 'cleared'})

