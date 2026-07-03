from django.urls import path
from . import views

urlpatterns = [
    path('stock/', views.StockLevelListView.as_view(), name='stock-level-list'),
    path('stock/adjust/', views.StockAdjustmentCreateView.as_view(), name='stock-adjust'),
    path('purchase/order/', views.PurchaseOrderListView.as_view(), name='purchase-order-list'),
    path('purchase/order/new/', views.PurchaseOrderCreateView.as_view(), name='purchase-order-create'),
    path('purchase/order/<int:pk>/', views.PurchaseOrderDetailView.as_view(), name='purchase-order-detail'),
    path('purchase/order/<int:pk>/receive/', views.ReceivePurchaseOrderView.as_view(), name='purchase-order-receive')
]