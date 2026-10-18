from django.urls import path

from . import helpdesk_views as v

app_name = "helpdesk"

urlpatterns = [
    path("", v.HelpdeskView.as_view(), name="page"),
    path("api/summary/", v.SummaryView.as_view(), name="api-summary"),
    path("api/tickets/", v.TicketListView.as_view(), name="api-tickets"),
    path("api/tickets/<int:number>/", v.TicketDetailView.as_view(), name="api-ticket"),
    path("api/tickets/<int:number>/reply/", v.TicketReplyView.as_view(), name="api-ticket-reply"),
    path("api/tickets/<int:number>/update/", v.TicketUpdateView.as_view(), name="api-ticket-update"),
    path("api/tickets/<int:number>/assign-me/", v.TicketAssignMeView.as_view(), name="api-ticket-assign"),
    path("api/articles/", v.ArticleListView.as_view(), name="api-articles"),
    path("api/articles/save/", v.ArticleSaveView.as_view(), name="api-article-save"),
    path("api/articles/<int:pk>/", v.ArticleDetailView.as_view(), name="api-article"),
    path("api/articles/<int:pk>/publish/", v.ArticlePublishView.as_view(), name="api-article-publish"),
    path("api/articles/<int:pk>/delete/", v.ArticleDeleteView.as_view(), name="api-article-delete"),
    path("api/features/", v.FeatureListView.as_view(), name="api-features"),
    path("api/features/<int:pk>/update/", v.FeatureUpdateView.as_view(), name="api-feature-update"),
]
