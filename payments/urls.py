from django.urls import path

from . import views


urlpatterns = [
    path('pos/checkout/pay/', views.payment_select, name='payment_select'),
    path('pos/checkout/process/', views.process_payment, name='process_payment')    
]