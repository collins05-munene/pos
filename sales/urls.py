from django.urls import path

from . import views

urlpatterns = [
    path('terminal/', views.pos_terminal, name='pos_terminal'),
    path('cart/add/', views.cart_add, name='cart_add'),
    path('cart/update/', views.cart_update, name='cart_update'),
    path('cart/remove/', views.cart_remove, name='cart_remove'),
    path('dashboard/', views.SalesDashboardView.as_view(), name='sales-dashboard'),
]