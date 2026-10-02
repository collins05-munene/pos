from django.urls import path

from . import views

urlpatterns = [
    path("", views.BackupListView.as_view(), name="backup-list"),
    path("create/", views.BackupCreateView.as_view(), name="backup-create"),
    path("schedule/", views.BackupScheduleView.as_view(), name="backup-schedule"),
    path("export/", views.BackupExportView.as_view(), name="backup-export"),
    path("<int:pk>/download/", views.BackupDownloadView.as_view(), name="backup-download"),
    path("<int:pk>/delete/", views.BackupDeleteView.as_view(), name="backup-delete"),
]