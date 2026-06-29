from django.urls import path
from . import views

urlpatterns = [
    path('categories/', views.CategoryListView.as_view(), name='category-list'),
    path('categories/create/', views.CategoryCreateView.as_view(), name='category-create'),
    path('category/<int:pk>/update/', views.CategoryUpdateView.as_view(), name='category-update'),
    path('category/<int:pk>/delete/', views.CategoryDeleteView.as_view(), name='category-delete'),

    path('brands/', views.BrandListView.as_view(), name='brand-list'),
    path('brands/create/', views.BrandCreateView.as_view(), name='brand-create'),
    path('brand/<int:pk>/update/', views.BrandUpdateView.as_view(), name='brand-update'),
    path('brand/<int:pk>/delete/', views.BrandDeleteView.as_view(), name='brand-delete'),

    path('uoms/', views.UoMListView.as_view(), name='uom-list'),
    path('uoms/create/', views.UoMCreateView.as_view(), name='uom-create'),
    path('uoms/<int:pk>/update/', views.UoMUpdateView.as_view(), name='uom-update'),
    path('uoms/<int:pk>/delete/', views.UoMDeleteView.as_view(), name='uom-delete'),

    path('products/', views.ProductListView.as_view(),name='product-list'),
    path('products/create/',views.ProductCreateView.as_view(), name='product-create'),
    path('product/<int:pk>/edit/',views.ProductUpdateView.as_view(),name='product-update'),
    path('product/<int:pk>/delete/',views.ProductDeleteView.as_view(),name='product-delete'),
    path('product/<int:pk>/detail/', views.ProductDetailView.as_view(), name='product-detail'),
]