from django.urls import path

from .views import (
    StockLevelListView,
    StockAdjustmentCreateView,
    PurchaseOrderListView,
    PurchaseOrderCreateView,
    PurchaseOrderDetailView,
    ReceivePurchaseOrderView,
    PurchasePaymentCreateView,
    TransferListView,
    TransferCreateView,
    TransferDetailView,
    TransferDispatchView,
    TransferReceiveView,
    TransferCancelView,
)

urlpatterns = [
    path('stock/', StockLevelListView.as_view(), name='stock-level-list'),
    path('stock/adjust/', StockAdjustmentCreateView.as_view(), name='stock-adjust'),

    path('purchases/', PurchaseOrderListView.as_view(), name='purchase-order-list'),
    path('purchases/new/', PurchaseOrderCreateView.as_view(), name='purchase-order-create'),
    path('purchases/<int:pk>/', PurchaseOrderDetailView.as_view(), name='purchase-order-detail'),
    path('purchases/<int:pk>/receive/', ReceivePurchaseOrderView.as_view(), name='purchase-order-receive'),
    path('purchases/<int:pk>/pay/', PurchasePaymentCreateView.as_view(), name='purchase-order-pay'),

    path('transfers/', TransferListView.as_view(), name='transfer-list'),
    path('transfers/new/', TransferCreateView.as_view(), name='transfer-create'),
    path('transfers/<int:pk>/', TransferDetailView.as_view(), name='transfer-detail'),
    path('transfers/<int:pk>/dispatch/', TransferDispatchView.as_view(), name='transfer-dispatch'),
    path('transfers/<int:pk>/receive/', TransferReceiveView.as_view(), name='transfer-receive'),
    path('transfers/<int:pk>/cancel/', TransferCancelView.as_view(), name='transfer-cancel'),
]
