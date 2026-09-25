from django.urls import path

from . import views

urlpatterns = [
    path('terminal/', views.PosTerminalView.as_view(), name='pos_terminal'),
    path('cart/add/', views.cart_add, name='cart_add'),
    path('cart/update/', views.cart_update, name='cart_update'),
    path('cart/remove/', views.cart_remove, name='cart_remove'),
    path('dashboard/', views.SalesDashboardView.as_view(), name='sales-dashboard'),
    path('order/<int:pk>/', views.OrderDetailView.as_view(), name='order-detail'),
    path('register/open/', views.OpenRegisterView.as_view(), name='open_register'),
    path('register/close/', views.CloseRegisterView.as_view(), name='close_register'),
    path('register/cash-transaction/', views.CashTransactionView.as_view(), name='cash_transaction'),
    path('cash-out/', views.RecordCashOutView.as_view(), name='record-cash-out'),
]