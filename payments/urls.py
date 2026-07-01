from django.urls import path

from . import views


urlpatterns = [
    path('pos/checkout/pay/', views.payment_select, name='payment_select'),
    path('pos/checkout/process/', views.process_payment, name='process_payment'),
    path('pos/checkout/mpesa/', views.mpesa_prompt, name='mpesa_prompt'),

    path('mpesa/callback/', views.MpesaCallbackView.as_view(), name='mpesa-callback'),
    path('api/mpesa/stk-push/', views.MpesaSTKPushView.as_view(), name='mpesa-stk-push'),
    
    path('mpesa/check-status/<str:checkout_request_id>/', views.CheckMpesaStatusView.as_view(), name='check-mpesa-status'),

    path('cart/clear/', views.ClearPOSCartView.as_view(), name='clear-pos-cart')
    
]