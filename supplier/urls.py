from django.urls import path

from . import views

urlpatterns = [
    path('suppliers/', views.SupplierListView.as_view(), name='supplier-list'),
    path('supplier/create/', views.SupplierCreateView.as_view(), name='supplier-create'),
    path('suppliers/<int:pk>/update/', views.SupplierUpdateView.as_view(), name='supplier-update'),
    path('suppliers/<int:pk>/delete/', views.SupplierDeleteView.as_view(), name='supplier-delete'),
    path('suppliers/<int:pk>/detail/', views.SupplierDetailView.as_view(), name='supplier-detail'),
    
]