from django.urls import path

from .views import (
    StockLevelListView,
    StockAdjustmentCreateView,
    PurchaseOrderListView,
    PurchaseOrderCreateView,
    PurchaseOrderDetailView,
    ReceivePurchaseOrderView,
    PurchasePaymentCreateView,
)

urlpatterns = [
    # Stock levels & adjustments
    path('stock/', StockLevelListView.as_view(), name='stock-level-list'),
    path('stock/adjust/', StockAdjustmentCreateView.as_view(), name='stock-adjust'),

    # Purchases (stock-in / procurement)
    path('purchases/', PurchaseOrderListView.as_view(), name='purchase-order-list'),
    path('purchases/new/', PurchaseOrderCreateView.as_view(), name='purchase-order-create'),
    path('purchases/<int:pk>/', PurchaseOrderDetailView.as_view(), name='purchase-order-detail'),
    path('purchases/<int:pk>/receive/', ReceivePurchaseOrderView.as_view(), name='purchase-order-receive'),
    path('purchases/<int:pk>/pay/', PurchasePaymentCreateView.as_view(), name='purchase-order-pay'),
]