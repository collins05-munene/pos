from django.urls import path
from . import branch_views as v

app_name = "branches"
urlpatterns = [
    path("", v.BranchListView.as_view(), name="branch-list"),
    path("switch/", v.BranchSwitchView.as_view(), name="branch-switch"),
    path("<int:pk>/<slug:action>/", v.BranchActionView.as_view(), name="branch-action"),
]
