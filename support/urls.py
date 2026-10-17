from django.urls import path

from . import views

app_name = "support"

urlpatterns = [
    path("", views.SupportHomeView.as_view(), name="home"),
    path("report/", views.ReportIssueView.as_view(), name="report-issue"),
    path("contact/", views.ContactSupportView.as_view(), name="contact"),
    path("tickets/", views.TicketListView.as_view(), name="ticket-list"),
    path("tickets/<int:number>/", views.TicketDetailView.as_view(), name="ticket-detail"),
    path("features/", views.FeatureListView.as_view(), name="feature-list"),
    path("features/new/", views.FeatureCreateView.as_view(), name="feature-new"),
    path("features/<int:pk>/vote/", views.FeatureVoteView.as_view(), name="feature-vote"),
    path("help/", views.HelpCenterView.as_view(), name="help-center"),
    path("help/<slug:slug>/", views.HelpArticleView.as_view(), name="help-article"),
    path("suggest/", views.SuggestView.as_view(), name="suggest"),
    path("attachments/<int:pk>/", views.AttachmentDownloadView.as_view(), name="attachment"),
    # Your support team
    path("team/", views.StaffTicketListView.as_view(), name="staff-queue"),
    path("team/tickets/<int:number>/", views.StaffTicketDetailView.as_view(), name="staff-ticket"),
]
