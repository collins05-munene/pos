from django.urls import path

from . import views

urlpatterns = [
    path("", views.DiscountListView.as_view(), name="discount-list"),
    path("new/", views.DiscountCreateView.as_view(), name="discount-create"),
    path("<int:pk>/edit/", views.DiscountUpdateView.as_view(), name="discount-update"),
    path("<int:pk>/toggle/", views.DiscountToggleView.as_view(), name="discount-toggle"),
    path("<int:pk>/delete/", views.DiscountDeleteView.as_view(), name="discount-delete"),
]
