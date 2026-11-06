from django.urls import path

from . import views

app_name = "onboarding"
urlpatterns = [
    path("", views.GuideView.as_view(), name="guide"),
    path("step/<slug:step>/", views.StepSubmitView.as_view(), name="step"),
    path("skip/<slug:step>/", views.SkipStepView.as_view(), name="skip"),
    path("dismiss/", views.DismissView.as_view(), name="dismiss"),
    path("restore/", views.RestoreView.as_view(), name="restore"),
]
